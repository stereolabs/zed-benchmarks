import argparse
import json
import os
import time
import signal
import sys
import pyzed.sl as sl
import glass_to_glass as g2gmod
from profiler import Profiler

# Global flag to stop the loop
is_running = True

def signal_handler(sig, frame):
    global is_running
    print("\nBenchmark stopped by user.")
    is_running = False

signal.signal(signal.SIGINT, signal_handler)


def sync_barrier_signal_and_wait(sync_dir):
    """Barrier sync: signal ready and wait for start file."""
    if not sync_dir:
        return
    ready_file = os.path.join(sync_dir, f"ready_{os.getpid()}")
    with open(ready_file, "w"):
        pass
    print("[Sync] Camera ready. Waiting for all processes...")
    start_file = os.path.join(sync_dir, "start")
    while is_running:
        if os.path.exists(start_file):
            print("[Sync] All processes ready. Starting benchmark.")
            return
        time.sleep(0.1)


def _compute_stats(samples):
    """Return (mean, median, stddev) over `samples`; sorts the list in place. Empty -> (0,0,0)."""
    n = len(samples)
    if n == 0:
        return (0.0, 0.0, 0.0)
    samples.sort()
    mean = sum(samples) / n
    median = (samples[n // 2 - 1] + samples[n // 2]) / 2.0 if n % 2 == 0 else samples[n // 2]
    stddev = (sum((v - mean) ** 2 for v in samples) / n) ** 0.5
    return (mean, median, stddev)


# ── Live-timestamp correspondence (G2G reconstruction at SVO playback) ────────
# At SVO playback, get_timestamp(CURRENT) returns the playback wall-clock, while the filmed screen
# shows the live capture time, so a G2G latency computed from it is meaningless. G2I does not
# need this map: at playback it measures the SVO processing time instead. To fix G2G we persist, during live
# capture, a map from each frame's TIME_REFERENCE.IMAGE timestamp (baked into the frame, identical
# across record/playback) to the live "now" sampled right after read(). At playback we look the
# frame up by its IMAGE timestamp and replay the recorded "now". The map holds INTEGER MILLISECONDS
# for both key (IMAGE ts) and value (the "now"); the stored "now" is the raw CURRENT clock WITHOUT
# the g2g offset (the playback path re-measures and adds its own offset). Mirrors the C++ benchmark.
LIVE_TS_KEY = "live_timestamp_correspondence"


def _serialize_live_ts_map(m):
    return json.dumps({str(k): v for k, v in m.items()})  # key=ms (string), value=ms (number)


def _deserialize_live_ts_map(payload):
    try:
        j = json.loads(payload)
        return {int(k): int(v) for k, v in j.items()}
    except (ValueError, TypeError) as e:
        print(f"[LiveTS] Failed to parse live-timestamp correspondences: {e}")
        return None


def _ingest_live_ts_map(zed, m, anchor_ts_ns):
    """Serialize the whole correspondence map into one SVOData blob and ingest it (valid only while
    recording). Anchor the blob at a real in-range frame timestamp (the last grabbed IMAGE ts, in
    ns: the map keys are milliseconds, not valid frame ns) so retrieve_svo_data finds it."""
    if not m:
        print("[LiveTS] No live timestamps collected; nothing to ingest.")
        return False
    data = sl.SVOData()
    data.key = LIVE_TS_KEY
    data.set_string_content(_serialize_live_ts_map(m))
    ts = sl.Timestamp()
    ts.set_nanoseconds(int(anchor_ts_ns))
    data.timestamp_ns = ts
    err = zed.ingest_data_into_svo(data)
    if err != sl.ERROR_CODE.SUCCESS:
        print(f"[LiveTS] Failed to ingest live-timestamp correspondences: {err}")
        return False
    print(f"[LiveTS] Ingested {len(m)} live-timestamp correspondences into the SVO.")
    return True


def _load_live_ts_map(zed):
    """Confirm the key is present, then retrieve + deserialize the map. Returns the dict, or None
    (with a clear message) if the SVO lacks the live-timestamp data."""
    keys = zed.get_svo_data_keys()
    if LIVE_TS_KEY not in keys:
        print(f"[LiveTS] This SVO has no '{LIVE_TS_KEY}' channel: the original live capture "
              "timestamps were not recorded, so glass-to-glass latency cannot be "
              "reconstructed at playback. Re-record the SVO with recording enabled to embed live "
              "timestamps.")
        return None
    svo_data = {}
    err = zed.retrieve_svo_data(LIVE_TS_KEY, svo_data, sl.Timestamp(), sl.Timestamp())  # (0,0) = all
    if err != sl.ERROR_CODE.SUCCESS or not svo_data:
        print(f"[LiveTS] Failed to retrieve '{LIVE_TS_KEY}' from the SVO: {err}")
        return None
    payload = next(iter(svo_data.values())).get_content_as_string()  # single blob
    m = _deserialize_live_ts_map(payload)
    if not m:
        print(f"[LiveTS] '{LIVE_TS_KEY}' present but empty/corrupt after parse.")
        return None
    print(f"[LiveTS] Loaded {len(m)} live-timestamp correspondences from the SVO.")
    return m


def _write_profiler_report(out_file, dump_file, sn, frames_count, total_seconds,
                           compression_rec_str="", encrypted=False):
    """Reshape the profiler JSON dump (profile_stats) into the historical report
    schema (Grab_SN<sn>, RetrieveImage_SN<sn>, …) so the analysis scripts keep
    working unchanged. Frequency is computed from the loop frame count (full run);
    per-section durations come from the profiler."""
    try:
        with open(dump_file, "r") as f:
            dump = json.load(f)
    except (OSError, ValueError):
        dump = {}

    all_profiles = dump.get("profile_stats") or {}
    # Scoped profiles live under the default context; each section name is
    # "camera_sn_<sn>_<function>(...)", so build that key once per lookup below.
    profiles = all_profiles.get("default") or {}
    def prof_key(fn):
        return f"camera_sn_{sn}_{fn}"

    def cpu_stats(name):
        s = profiles.get(name, {})
        return {
            "mean": s.get("mean", 0.0),
            "median": s.get("median", 0.0),
            "stddev": s.get("stddev", 0.0),
            "min": s.get("min", 0.0),
            "max": s.get("max", 0.0),
            "p99": s.get("p99", 0.0),
            "nb_samples": s.get("count", 0),
        }

    def cpu_stats_sum(a, b):
        # read() and grab() are profiled as separate sections; the reported "Grab" cost is their
        # field-by-field sum (full capture+process time). Missing sections contribute 0, so the
        # mono path (grab() only, no read()) reports grab() alone.
        sa = profiles.get(a, {})
        sb = profiles.get(b, {})
        out = {k: sa.get(k, 0.0) + sb.get(k, 0.0) for k in ("mean", "median", "stddev", "min", "max", "p99")}
        out["nb_samples"] = sb.get("count", sa.get("count", 0))
        return out

    report = {}
    # Recording config (only when recording ran), mirroring the C++ report's "config" block.
    if compression_rec_str:
        report["config"] = {
            "recording_compression": compression_rec_str,
            "recording_encrypted": encrypted,
        }

    freq = frames_count / total_seconds if total_seconds > 0 else 0.0
    period = profiles.get(prof_key("grab_period"), {})
    report[f"Grab_SN{sn}"] = {
        "grab_call_frequency_hz": round(freq, 3),
        "grab_call_mean_period_ms": period.get("mean", 0.0),
        "grab_call_median_period_ms": period.get("median", 0.0),
        "cpu_stats": cpu_stats_sum(prof_key("read(...)"), prof_key("grab(...)")),
    }
    # Per-operation sections are profiled as "camera_sn_<sn>_<function>(...)"; the report key is
    # "<ReportName>_SN<sn>" so per-event timings stay serial-keyed and coherent across single- and
    # multi-camera reports.
    event_sections = (
        ("retrieveImage(...)",   "RetrieveImage"),
        ("retrieveMeasure(...)", "RetrieveMeasure"),
        ("retrieveObjects(...)", "RetrieveObjects"),
        ("retrieveBodies(...)",  "RetrieveBodies"),
        ("getPosition(...)",     "GetPosition"),
    )
    for prof_name, report_name in event_sections:
        if prof_key(prof_name) in profiles:
            report[f"{report_name}_SN{sn}"] = {"cpu_stats": cpu_stats(prof_key(prof_name))}
    # GrabProcessing is the grab() call alone (SDK processing, without the read() capture step), so
    # it can be stacked on top of glass-to-image without counting read() twice. Only emitted when
    # read() was profiled separately: on mono (no read() split) grab() is already in glass-to-image.
    if prof_key("read(...)") in profiles and prof_key("grab(...)") in profiles:
        report[f"GrabProcessing_SN{sn}"] = {"cpu_stats": cpu_stats(prof_key("grab(...)"))}

    # Latency sections, fed to the profiler with add_sample() (see the grab loop). Statistics are
    # computed over the WHOLE sample set: the G2G decoder only emits a sample after the per-column
    # agreement + CRC + plausibility checks pass, so mis-detections are rejected at decode time and
    # no statistical tail-trim is needed (matches the C++ benchmark).
    for prof_name, report_name in (("glass_to_image", "GlassToImageLatency"),
                                   ("glass_to_glass", "GlassToGlassLatency")):
        st = profiles.get(prof_key(prof_name))
        if not st:
            continue
        report[f"{report_name}_SN{sn}"] = {
            "sample_count": st.get("count", 0),
            "latency_ms": {k: round(st.get(k, 0.0), 3) for k in ("mean", "median", "stddev", "min", "max")},
        }

    with open(out_file, "w") as f:
        json.dump(report, f, indent=4)
    print(f"[Profiler] Results written to {out_file}")


def _append_imu_json(filename, imu_key, imu_sample_count, total_seconds, imu_per_frame):
    """Append IMU stats to an existing report JSON file. imu_per_frame is reported as the full
    {mean, median, stddev} distribution of the per-frame sample counts (matches the C++ report)."""
    if imu_sample_count == 0:
        return
    with open(filename, "r") as f:
        data = json.load(f)
    rate = imu_sample_count / total_seconds if total_seconds > 0 else 0.0
    mean, median, stddev = _compute_stats(list(imu_per_frame))
    data[imu_key] = {
        "imu_sample_count": imu_sample_count,
        "imu_rate_hz": round(rate, 3),
        "imu_per_frame": {"mean": round(mean, 3), "median": round(median, 3), "stddev": round(stddev, 3)},
    }
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)
    print(f"[Profiler] IMU data appended to {filename}")


# ── Glass-to-glass helper (shared by the mono and stereo runners) ────────────
def _g2g_setup(args, sn, latency_reserve):
    """Open the display (live capture only) and start the async decoder. Returns
    (async_decoder, sync_decoder, display). Live capture sets async_decoder (decode off the grab
    loop, may drop frames); SVO input sets sync_decoder instead (decode every frame inline for a
    repeatable result). The unused one is None."""
    if not getattr(args, "measure_glass_to_glass_latency", False):
        return (None, None, None)
    display = None
    if args.svo_input is None:
        display = g2gmod.Display()
        if not display.start():
            print("[G2G] Failed to open the display window; disabling G2G.")
            return (None, None, None)
    else:
        # On SVO playback the filmed screen is baked into the recorded frames; decode those rather
        # than driving a live screen, no display window is opened (decoder runs with display=None).
        print("[G2G] SVO playback: decoding the recorded screen; no display window opened.")
    print("[G2G] enabled")
    if args.svo_input is None:
        # Live capture: decode on a worker thread so it never inflates the grab loop period
        # (the single-slot mailbox drops frames the worker cannot keep up with).
        async_dec = g2gmod.AsyncDecoder()
        async_dec.start(display, sn, max(1, int(latency_reserve)))
        return (async_dec, None, display)
    # SVO playback: decode synchronously on EVERY frame so two replays of the same SVO produce the
    # exact same G2G samples (the async path drops a timing-dependent subset). Inflates the SVO loop.
    return (None, g2gmod.Decoder(), display)


def run_mono_benchmark(args, resolution, compression_mode_recording, compression_mode_streaming, encoding_preset=None):
    """Run benchmark for a mono camera (CameraOne)."""
    global is_running

    zed = sl.CameraOne()
    init_params = sl.InitParametersOne()
    init_params.camera_resolution = resolution
    init_params.camera_fps = args.camera_fps
    if args.svo_input is not None:
        init_params.set_from_svo_file(args.svo_input)
        init_params.svo_real_time_mode = False
        if args.encryption_key:
            init_params.svo_decryption_key = args.encryption_key
    elif args.serial_number is not None and args.serial_number > 0:
        init_params.set_from_serial_number(args.serial_number)

    print(f"Opening CameraOne with Resolution={resolution}, FPS={args.camera_fps}...")
    status = zed.open(init_params)
    if status != sl.ERROR_CODE.SUCCESS:
        print(f"Failed to open CameraOne: {status}")
        sys.exit(1)

    cam_info = zed.get_camera_information()
    print(f"Opened CameraOne ({cam_info.camera_model})")

    # Enable Recording
    if compression_mode_recording is not None:
        rec_params = sl.RecordingParameters()
        rec_params.compression_mode = compression_mode_recording
        if encoding_preset is not None:
            rec_params.encoding_preset = encoding_preset
        rec_params.video_filename = args.output_svo
        if args.encryption_key:
            rec_params.encryption_key = args.encryption_key
        err = zed.enable_recording(rec_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable recording: {err}")
            sys.exit(1)
        print(f"Recording enabled ({compression_mode_recording}{', encrypted' if args.encryption_key else ''}) to {args.output_svo}")

    # Enable Streaming
    if compression_mode_streaming is not None:
        stream_params = sl.StreamingParameters()
        stream_params.codec = compression_mode_streaming
        stream_params.port = args.streaming_port
        err = zed.enable_streaming(stream_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable streaming: {err}")
            sys.exit(1)
        print(f"Streaming enabled ({compression_mode_streaming}) on port {args.streaming_port}")

    # Barrier sync: wait for all processes before starting
    sync_barrier_signal_and_wait(args.sync_dir)

    image = sl.Mat()

    # Warmup. Skip on SVO playback: there is no hardware to warm up, and warmup would consume
    # frames from the file (svo_real_time_mode is off), shrinking the benchmark window.
    if args.warmup > 0 and args.svo_input is None:
        print(f"\nStarting warmup loop for {args.warmup} seconds...")
        warmup_start_time = time.time()
        while is_running:
            if (time.time() - warmup_start_time) > args.warmup:
                print("Warmup finished.")
                break
            if zed.grab() > sl.ERROR_CODE.SUCCESS:
                time.sleep(0.001)
    elif args.warmup > 0:
        print("\nSVO input detected: skipping warmup.")

    # Enable scoped profiling after warmup so warmup grabs are not measured.
    Profiler.enable()
    sn = str(cam_info.serial_number)
    # Profiled section names are "camera_sn_<sn>_<function>(...)" in the default context;
    # precompute them once so the grab loop never builds a string per call. (CameraOne has no
    # read() in the Python API, so there is no read(...) section here.)
    prof_grab = "camera_sn_" + sn + "_grab(...)"
    prof_period = "camera_sn_" + sn + "_grab_period"
    prof_img = "camera_sn_" + sn + "_retrieveImage(...)"
    # Latency sections, fed with add_sample() rather than start()/stop().
    prof_g2i = "camera_sn_" + sn + "_glass_to_image"
    prof_g2g = "camera_sn_" + sn + "_glass_to_glass"
    # Retrieve at the SDK's working resolution. Built once so the grab loop never
    # allocates inside the measured retrieve sections.
    working_res = sl.Resolution(-1, -1)

    # Live timestamp map: populated while recording live, then used to reconstruct the G2G
    # timestamps during SVO playback. Key = IMAGE timestamp, value = the raw after-grab "now"
    # (camera-clock domain, WITHOUT the g2g offset); both stored as integer milliseconds.
    # NOTE: CameraOne has no read() in the Python API, so the "now" is sampled right after grab()
    # only: the read()/grab() split the stereo path and the C++ benchmark use is unavailable here, so
    # mono's glass-to-image includes grab().
    record_live_ts = compression_mode_recording is not None
    playback_live_ts = (args.svo_input is not None) and args.measure_glass_to_glass_latency
    live_ts_map = {}
    last_image_ts_ns = 0
    if playback_live_ts:
        live_ts_map = _load_live_ts_map(zed)
        if live_ts_map is None:
            zed.close()
            sys.exit(1)

    reserve = max(1, int(max(0, args.duration) * args.camera_fps * 1.1))
    g2g_async, g2g_sync, g2g_display = _g2g_setup(args, sn, reserve)

    frames_count = 0
    start_time = time.time()
    period_open = False
    imu_sample_count = 0
    imu_per_frame = []
    all_sensors_data_in_current_frame = []
    print(f"\nStarting benchmark loop for {args.duration} seconds. Press Ctrl+C to stop.")

    while is_running:
        # SVO playback ignores --duration: process the whole file so two replays of the same SVO
        # cover the exact same frames (repeatability). Live capture stays time-bounded.
        if args.svo_input is None and args.duration > 0 and (time.time() - start_time) > args.duration:
            print("\nBenchmark finished (duration reached).")
            break

        before_grab_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
        Profiler.start(prof_grab)
        grab_status = zed.grab()
        Profiler.stop(prof_grab)
        # Sample the raw "now" reference right after grab() (CameraOne has no read() split). The G2G
        # offset is NOT folded in here, so the stored live-ts and G2I stay in the camera-clock domain.
        now_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
        if grab_status == sl.ERROR_CODE.SUCCESS:
            image_ts_ns = zed.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds()
            # Recording: store the raw camera-clock "now" keyed by the IMAGE timestamp (both ms).
            if record_live_ts:
                live_ts_map[image_ts_ns // 1_000_000] = now_ns // 1_000_000
                last_image_ts_ns = image_ts_ns

            # Inter-grab period (success-to-success), measured by the profiler.
            if period_open:
                Profiler.stop(prof_period)
            Profiler.start(prof_period)
            period_open = True

            # G2I reference. Live: the image (capture) timestamp. SVO playback: the image timestamp is the
            # original capture time, so use the host time right before grab() instead, which makes G2I the
            # latency of processing the SVO frame.
            capture_ref_ns = image_ts_ns if args.svo_input is None else before_grab_ns

            # Retrieve at the SDK's working resolution (see working_res) instead of the
            # camera resolution (default). This is more efficient.
            before_img_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
            Profiler.start(prof_img)
            zed.retrieve_image(image, sl.VIEW.LEFT, sl.MEM.CPU, working_res)
            Profiler.stop(prof_img)
            after_img_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()

            # Glass-to-image latency: reference -> end of grab() (CameraOne has no read() split, so
            # grab() is included), plus retrieve_image().
            Profiler.add_sample(prof_g2i, ((now_ns - capture_ref_ns) + (after_img_ns - before_img_ns)) / 1.0e6)
            # Glass-to-glass. The "now" (the camera CURRENT clock) and the decoded on-screen
            # time share the host realtime clock, so their difference is the latency directly.
            g2g_now_ns = now_ns
            # Playback: replace the playback wall-clock "now" with the live "now"
            # recorded at capture time, keyed by the frame's baked IMAGE timestamp (ms).
            if playback_live_ts:
                measured = live_ts_map.get(image_ts_ns // 1_000_000)
                if measured is None:
                    print(f"[LiveTS] No correspondence for IMAGE ts {image_ts_ns}; no G2G sample for this frame.")
                    g2g_now_ns = None
                else:
                    g2g_now_ns = measured * 1_000_000
            if g2g_now_ns is None:
                pass
            elif g2g_async is not None:
                # Live: hand the frame to the worker (deep copy); it decodes off the grab loop.
                g2g_async.submit(image, g2g_now_ns)
            elif g2g_sync is not None:
                # SVO: decode every frame here (no async drops) for a repeatable result.
                status, display_ns = g2g_sync.decode(image, g2g_now_ns)
                if status == g2gmod.Status.LOCKED:
                    g2g_lat_ms = (g2g_now_ns - display_ns) / 1.0e6
                    if g2g_lat_ms > 0.0:  # drop non-positive latency
                        Profiler.add_sample(prof_g2g, g2g_lat_ms)
            frames_count += 1
            if args.monitor_imu:
                zed.get_sensors_data_batch(all_sensors_data_in_current_frame)
                if all_sensors_data_in_current_frame is not None:
                    imu_sample_count += len(all_sensors_data_in_current_frame)
                    imu_per_frame.append(float(len(all_sensors_data_in_current_frame)))
                else:
                    imu_per_frame.append(0.0)
            if frames_count % args.camera_fps == 0:
                sys.stdout.write(f"\rFPS: {zed.get_current_fps():.2f} | Frames: {frames_count}")
                sys.stdout.flush()
        else:
            # End of SVO: stop here instead of spinning. This both terminates the otherwise
            # unbounded SVO run and keeps the reported frequency free of a post-EOF idle tail.
            if args.svo_input is not None and grab_status == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                print("\nBenchmark finished (end of SVO reached).")
                break
            time.sleep(0.001)
    Profiler.stop(prof_period)

    print(f"\nFinal FPS: {zed.get_current_fps():.2f}")

    total_s = time.time() - start_time

    # Stop + join the async decoder before the profiler dump, so every in-flight latency sample
    # is collected, then hand its latencies to the profiler.
    if g2g_async is not None:
        g2g_async.stop()
        for lat_ms in g2g_async.take_latencies():
            Profiler.add_sample(prof_g2g, lat_ms)

    # Dump profiler stats while the camera is still open (closing it resets the
    # profiler), then reshape into the historical report schema.
    raw_report = args.benchmark + ".profiler.json"
    Profiler.dump_stats(raw_report)

    # Persist the live-timestamp correspondence map into the SVO before recording is disabled
    # (ingest_data_into_svo is only valid while recording).
    if record_live_ts:
        _ingest_live_ts_map(zed, live_ts_map, last_image_ts_ns)

    if compression_mode_recording is not None:
        zed.disable_recording()
    if compression_mode_streaming is not None:
        zed.disable_streaming()
    zed.close()

    _write_profiler_report(args.benchmark, raw_report, sn, frames_count, total_s,
                           args.compression_mode_recording or "", bool(args.encryption_key))
    try:
        os.remove(raw_report)
    except OSError:
        pass
    if g2g_display is not None:
        g2g_display.stop()
    if args.monitor_imu and imu_sample_count > 0:
        _append_imu_json(args.benchmark, f"IMU_SN{sn}", imu_sample_count, total_s, imu_per_frame)


def run_stereo_benchmark(args, resolution, depth_mode, od_model, bt_model,
                         compression_mode_recording, compression_mode_streaming,
                         od_custom_onnx_file=None, od_custom_onnx_input_shape=None,
                         encoding_preset=None):
    """Run benchmark for a stereo camera (Camera)."""
    global is_running

    zed = sl.Camera()
    init_params = sl.InitParameters()
    init_params.camera_resolution = resolution
    init_params.camera_fps = args.camera_fps
    init_params.depth_mode = depth_mode
    if args.svo_input is not None:
        init_params.set_from_svo_file(args.svo_input)
        init_params.svo_real_time_mode = False
        if args.encryption_key:
            init_params.svo_decryption_key = args.encryption_key
    elif args.serial_number is not None and args.serial_number > 0:
        init_params.set_from_serial_number(args.serial_number)

    print(f"Opening Camera with Resolution={resolution}, FPS={args.camera_fps}, Depth={depth_mode}...")
    status = zed.open(init_params)
    if status != sl.ERROR_CODE.SUCCESS:
        print(f"Failed to open camera: {status}")
        sys.exit(1)

    print(f"Opened Camera ({zed.get_camera_information().camera_model})")

    # Enable Recording
    if compression_mode_recording is not None:
        rec_params = sl.RecordingParameters()
        rec_params.compression_mode = compression_mode_recording
        if encoding_preset is not None:
            rec_params.encoding_preset = encoding_preset
        rec_params.video_filename = args.output_svo
        if args.encryption_key:
            rec_params.encryption_key = args.encryption_key
        err = zed.enable_recording(rec_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable recording: {err}")
            sys.exit(1)
        print(f"Recording enabled ({compression_mode_recording}{', encrypted' if args.encryption_key else ''}) to {args.output_svo}")

    # Enable Streaming
    if compression_mode_streaming is not None:
        stream_params = sl.StreamingParameters()
        stream_params.codec = compression_mode_streaming
        stream_params.port = args.streaming_port
        err = zed.enable_streaming(stream_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable streaming: {err}")
            sys.exit(1)
        print(f"Streaming enabled ({compression_mode_streaming}) on port {args.streaming_port}")

    # Positional Tracking
    pt_enabled = False
    if args.enable_positional_tracking:
        pt_params = sl.PositionalTrackingParameters()
        pt_params.enable_area_memory = True
        err = zed.enable_positional_tracking(pt_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable Positional Tracking: {err}")
            sys.exit(1)
        print("Positional Tracking enabled (Area Memory)")
        pt_enabled = True

    # Object Detection
    if od_model is not None:
        if not pt_enabled:
            pt_params = sl.PositionalTrackingParameters()
            pt_params.enable_area_memory = True
            zed.enable_positional_tracking(pt_params)
            pt_enabled = True
        od_params = sl.ObjectDetectionParameters()
        od_params.detection_model = od_model
        od_params.enable_tracking = True
        if od_custom_onnx_file is not None:
            od_params.custom_onnx_file = od_custom_onnx_file
            if od_custom_onnx_input_shape is not None:
                od_params.custom_onnx_dynamic_input_shape = od_custom_onnx_input_shape
            print(f"Custom ONNX file: {od_custom_onnx_file}")
            print(f"Custom ONNX input shape: {od_params.custom_onnx_dynamic_input_shape.width}x{od_params.custom_onnx_dynamic_input_shape.height}")
        err = zed.enable_object_detection(od_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable Object Detection: {err}")
            sys.exit(1)
        print(f"Object Detection enabled ({od_model})")

    # Body Tracking
    if bt_model is not None:
        if not pt_enabled:
            pt_params = sl.PositionalTrackingParameters()
            pt_params.enable_area_memory = True
            zed.enable_positional_tracking(pt_params)
            pt_enabled = True
        bt_params = sl.BodyTrackingParameters()
        bt_params.detection_model = bt_model
        bt_params.enable_tracking = True
        err = zed.enable_body_tracking(bt_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable Body Tracking: {err}")
            sys.exit(1)
        print(f"Body Tracking enabled ({bt_model})")

    # Runtime Data Containers (allocated once and reused each frame).
    runtime_params = sl.RuntimeParameters()
    image = sl.Mat()
    depth = sl.Mat()
    objects = sl.Objects()
    bodies = sl.Bodies()
    pose = sl.Pose()
    od_runtime_params = sl.ObjectDetectionRuntimeParameters()
    bt_runtime_params = sl.BodyTrackingRuntimeParameters()

    # Barrier sync: wait for all processes before starting
    sync_barrier_signal_and_wait(args.sync_dir)

    # Warmup. Skip on SVO playback: there is no hardware to warm up, and warmup would consume
    # frames from the file (svo_real_time_mode is off), shrinking the benchmark window.
    if args.warmup > 0 and args.svo_input is None:
        print(f"\nStarting warmup loop for {args.warmup} seconds...")
        warmup_start_time = time.time()
        while is_running:
            if (time.time() - warmup_start_time) > args.warmup:
                print("Warmup finished.")
                break
            if zed.grab(runtime_params) > sl.ERROR_CODE.SUCCESS:
                time.sleep(0.001)
    elif args.warmup > 0:
        print("\nSVO input detected: skipping warmup.")

    # Enable scoped profiling after warmup so warmup grabs are not measured.
    Profiler.enable()
    sn = str(zed.get_camera_information().serial_number)
    # Profiled section names are "camera_sn_<sn>_<function>(...)" in the default context;
    # precompute them once so the grab loop never builds a string per call.
    prof_read = "camera_sn_" + sn + "_read(...)"
    prof_grab = "camera_sn_" + sn + "_grab(...)"
    prof_period = "camera_sn_" + sn + "_grab_period"
    prof_img = "camera_sn_" + sn + "_retrieveImage(...)"
    # Latency sections, fed with add_sample() rather than start()/stop().
    prof_g2i = "camera_sn_" + sn + "_glass_to_image"
    prof_g2g = "camera_sn_" + sn + "_glass_to_glass"
    prof_measure = "camera_sn_" + sn + "_retrieveMeasure(...)"
    prof_pos = "camera_sn_" + sn + "_getPosition(...)"
    # Retrieve at the SDK's working resolution. Built once so the grab loop never
    # allocates inside the measured retrieve sections.
    working_res = sl.Resolution(-1, -1)
    prof_obj = "camera_sn_" + sn + "_retrieveObjects(...)"
    prof_bodies = "camera_sn_" + sn + "_retrieveBodies(...)"

    # Live timestamp map: populated while recording live, then used to reconstruct the G2G
    # timestamps during SVO playback. Key = IMAGE timestamp, value = CURRENT-after-read "now"
    # (camera-clock domain, WITHOUT the g2g offset); both stored as integer milliseconds.
    record_live_ts = compression_mode_recording is not None
    playback_live_ts = (args.svo_input is not None) and args.measure_glass_to_glass_latency
    live_ts_map = {}
    last_image_ts_ns = 0
    if playback_live_ts:
        live_ts_map = _load_live_ts_map(zed)
        if live_ts_map is None:
            zed.close()
            sys.exit(1)
    reserve = max(1, int(max(0, args.duration) * args.camera_fps * 1.1))
    g2g_async, g2g_sync, g2g_display = _g2g_setup(args, sn, reserve)

    frames_count = 0
    start_time = time.time()
    period_open = False
    imu_sample_count = 0
    imu_per_frame = []
    all_sensors_data_in_current_frame = []
    print(f"\nStarting benchmark loop for {args.duration} seconds. Press Ctrl+C to stop.")

    while is_running:
        # SVO playback ignores --duration: process the whole file so two replays of the same SVO
        # cover the exact same frames (repeatability). Live capture stays time-bounded.
        if args.svo_input is None and args.duration > 0 and (time.time() - start_time) > args.duration:
            print("\nBenchmark finished (duration reached).")
            break

        # Split capture: read() pulls + rectifies the frame (the blocking step); grab() then runs
        # the SDK measurements (depth, tracking, …). Each is profiled separately (.read() / .grab())
        # and summed in the report into the full capture+process "Grab" cost.
        before_read_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
        Profiler.start(prof_read)
        read_status = zed.read()
        Profiler.stop(prof_read)

        # Live-timestamp correspondence: sample the raw CURRENT clock right after read(). The G2G
        # offset is NOT folded in here, so the stored live-ts and G2I stay in the camera-clock domain.
        after_read_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()

        Profiler.start(prof_grab)
        grab_status = zed.grab(runtime_params)
        Profiler.stop(prof_grab)

        if grab_status == sl.ERROR_CODE.SUCCESS:
            image_ts_ns = zed.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds()

            capture_ref_ns = image_ts_ns if args.svo_input is None else before_read_ns
            # Recording: store the raw camera-clock "now" keyed by the IMAGE timestamp (both ms).
            if record_live_ts:
                live_ts_map[image_ts_ns // 1_000_000] = after_read_ns // 1_000_000
                last_image_ts_ns = image_ts_ns

            # Inter-grab period (success-to-success), measured by the profiler.
            if period_open:
                Profiler.stop(prof_period)
            Profiler.start(prof_period)
            period_open = True

            # Retrieve at the SDK's working resolution (see working_res) instead of the
            # camera resolution (default). This is more efficient.
            before_img_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
            Profiler.start(prof_img)
            zed.retrieve_image(image, sl.VIEW.LEFT, sl.MEM.CPU, working_res)
            Profiler.stop(prof_img)
            after_img_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()

            Profiler.add_sample(prof_g2i, ((after_read_ns - capture_ref_ns) + (after_img_ns - before_img_ns)) / 1.0e6)

            # Glass-to-glass. The after-read "now" (the camera CURRENT clock) and the decoded on-screen
            # time share the host realtime clock, so their difference is the latency directly.
            g2g_now_ns = after_read_ns
            if playback_live_ts:
                measured = live_ts_map.get(image_ts_ns // 1_000_000)
                if measured is None:
                    print(f"[LiveTS] No correspondence for IMAGE ts {image_ts_ns}; no G2G sample for this frame.")
                    g2g_now_ns = None
                else:
                    g2g_now_ns = measured * 1_000_000
            if g2g_now_ns is None:
                pass
            elif g2g_async is not None:
                # Live: hand the frame to the worker (deep copy); it decodes off the grab loop.
                g2g_async.submit(image, g2g_now_ns)
            elif g2g_sync is not None:
                # SVO: decode every frame here (no async drops) for a repeatable result.
                status, display_ns = g2g_sync.decode(image, g2g_now_ns)
                if status == g2gmod.Status.LOCKED:
                    g2g_lat_ms = (g2g_now_ns - display_ns) / 1.0e6
                    if g2g_lat_ms > 0.0:  # drop non-positive latency
                        Profiler.add_sample(prof_g2g, g2g_lat_ms)

            if init_params.depth_mode != sl.DEPTH_MODE.NONE:
                Profiler.start(prof_measure)
                zed.retrieve_measure(depth, sl.MEASURE.XYZ, sl.MEM.CPU, working_res)
                Profiler.stop(prof_measure)

            if pt_enabled:
                Profiler.start(prof_pos)
                zed.get_position(pose, sl.REFERENCE_FRAME.WORLD)
                Profiler.stop(prof_pos)

            if od_model is not None:
                Profiler.start(prof_obj)
                zed.retrieve_objects(objects, od_runtime_params)
                Profiler.stop(prof_obj)

            if bt_model is not None:
                Profiler.start(prof_bodies)
                zed.retrieve_bodies(bodies, bt_runtime_params)
                Profiler.stop(prof_bodies)

            frames_count += 1
            if args.monitor_imu:
                zed.get_sensors_data_batch(all_sensors_data_in_current_frame)
                if all_sensors_data_in_current_frame is not None:
                    imu_sample_count += len(all_sensors_data_in_current_frame)
                    imu_per_frame.append(float(len(all_sensors_data_in_current_frame)))
                else:
                    imu_per_frame.append(0.0)
            if frames_count % args.camera_fps == 0:
                sys.stdout.write(f"\rFPS: {zed.get_current_fps():.2f} | Frames: {frames_count}")
                sys.stdout.flush()
        else:
            # End of SVO: stop here instead of spinning. This both terminates the otherwise
            # unbounded SVO run and keeps the reported frequency free of a post-EOF idle tail.
            if args.svo_input is not None and grab_status == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                print("\nBenchmark finished (end of SVO reached).")
                break
            time.sleep(0.001)
    Profiler.stop(prof_period)

    print(f"\nFinal FPS: {zed.get_current_fps():.2f}")

    total_s = time.time() - start_time

    # Stop + join the async decoder before the profiler dump, so every in-flight latency sample
    # is collected, then hand its latencies to the profiler.
    if g2g_async is not None:
        g2g_async.stop()
        for lat_ms in g2g_async.take_latencies():
            Profiler.add_sample(prof_g2g, lat_ms)

    # Dump profiler stats while the camera is still open (closing it resets the
    # profiler), then reshape into the historical report schema.
    raw_report = args.benchmark + ".profiler.json"
    Profiler.dump_stats(raw_report)

    # Persist the live-timestamp correspondence map into the SVO before recording is disabled
    # (ingest_data_into_svo is only valid while recording).
    if record_live_ts:
        _ingest_live_ts_map(zed, live_ts_map, last_image_ts_ns)

    if od_model is not None:
        zed.disable_object_detection()
    if bt_model is not None:
        zed.disable_body_tracking()
    if pt_enabled:
        zed.disable_positional_tracking()
    if compression_mode_recording is not None:
        zed.disable_recording()
    if compression_mode_streaming is not None:
        zed.disable_streaming()
    zed.close()

    _write_profiler_report(args.benchmark, raw_report, sn, frames_count, total_s,
                           args.compression_mode_recording or "", bool(args.encryption_key))
    try:
        os.remove(raw_report)
    except OSError:
        pass
    if g2g_display is not None:
        g2g_display.stop()
    if args.monitor_imu and imu_sample_count > 0:
        _append_imu_json(args.benchmark, f"IMU_SN{sn}", imu_sample_count, total_s, imu_per_frame)


def main():
    # Arguments
    parser = argparse.ArgumentParser(description="ZED Single Camera Benchmark")
    parser.add_argument("--svo_input", type=str, default=None, help="Path to SVO file (default: live)")
    parser.add_argument("--camera_fps", type=int, default=30, help="Camera FPS (e.g. 15, 30, 60)")
    parser.add_argument("--resolution", type=str, default="HD1080", help="Resolution (e.g. HD1200)")
    parser.add_argument("--depth_mode", type=str, default=None, help="Depth Mode (e.g. NEURAL_LIGHT). If set, activates depth.")
    parser.add_argument("--od_model", type=str, default=None, help="Object Detection Model (e.g. MULTI_CLASS_BOX_FAST). If set, activates OD.")
    parser.add_argument("--od_custom_onnx_file", type=str, default=None, help="Path to custom ONNX file (requires od_model=CUSTOM_YOLOLIKE_BOX_OBJECTS).")
    parser.add_argument("--od_custom_onnx_input_shape", type=str, default=None, help="Input resolution for custom ONNX as WxH (e.g. 640x640, default: 512x512).")
    parser.add_argument("--bt_model", type=str, default=None, help="Body Tracking Model (e.g. HUMAN_BODY_FAST). If set, activates BT.")
    parser.add_argument("--compression_mode_recording", type=str, default=None, help="Compression Mode for Recording (e.g. H265). If set, activates recording.")
    parser.add_argument("--encoding_preset", type=str, default=None, help="Encoding preset: DEFAULT, ULTRAFAST, FAST, MEDIUM, SLOW (default: DEFAULT)")
    parser.add_argument("--encryption_key", type=str, default="", help="Encryption key/passphrase for SVO recording / decryption (default: empty)")
    parser.add_argument("--output_svo", type=str, default="benchmark_recording.svo", help="Output filename for recording.")
    parser.add_argument("--compression_mode_streaming", type=str, default=None, help="Compression Mode for Streaming (e.g. H265). If set, activates streaming.")
    parser.add_argument("--streaming_port", type=int, default=30000, help="Streaming port (default: 30000)")
    parser.add_argument("--serial_number", type=int, default=None, help="Camera serial number (default: auto-detect)")
    parser.add_argument("--enable_positional_tracking", action="store_true", help="Activates positional tracking.")
    parser.add_argument("--camera_one", action="store_true", help="Use CameraOne (mono) API instead of Camera (stereo)")
    parser.add_argument("--warmup", type=float, default=5.0, help="Warmup duration in seconds (default: 5.0)")
    parser.add_argument("--duration", type=float, default=60.0, help="Duration of the benchmark in seconds. -1 for infinite.")
    parser.add_argument("--benchmark", type=str, default="benchmark_report.json", help="Benchmark report filename")
    parser.add_argument("--sync_dir", type=str, default=None, help="Barrier sync directory for multi-process coordination")
    parser.add_argument("--no_monitor_imu", action="store_true", help="Disable IMU monitoring (default: ON)")
    parser.add_argument("--glass_to_glass_latency", action="store_true", help="Measure glass-to-glass latency via an on-screen pattern (default: OFF)")
    args = parser.parse_args()
    if args.camera_fps < 1:
        parser.error("--camera_fps must be >= 1")
    args.monitor_imu = not args.no_monitor_imu
    args.measure_glass_to_glass_latency = args.glass_to_glass_latency
    if args.measure_glass_to_glass_latency and not g2gmod.G2G_AVAILABLE:
        print("[G2G] --glass_to_glass_latency requested but glfw / PyOpenGL / OpenCV are missing.\n"
              "      Install them (see python/requirements.txt). Continuing without G2G.")
        args.measure_glass_to_glass_latency = False

    # Parse enums
    resolution = sl.RESOLUTION.from_string(args.resolution.upper())
    if resolution == sl.RESOLUTION.LAST:
        print(f"Invalid resolution: {args.resolution}. Using HD1080.")
        resolution = sl.RESOLUTION.HD1080

    depth_mode = sl.DEPTH_MODE.NONE if args.depth_mode is None else sl.DEPTH_MODE.from_string(args.depth_mode.upper())
    if depth_mode == sl.DEPTH_MODE.LAST:
        print(f"Invalid depth mode: {args.depth_mode}. Using NONE.")
        depth_mode = sl.DEPTH_MODE.NONE

    od_model = None if args.od_model is None else sl.OBJECT_DETECTION_MODEL.from_string(args.od_model.upper())
    if od_model is not None and od_model == sl.OBJECT_DETECTION_MODEL.LAST:
        od_model = None

    bt_model = None if args.bt_model is None else sl.BODY_TRACKING_MODEL.from_string(args.bt_model.upper())
    if bt_model is not None and bt_model == sl.BODY_TRACKING_MODEL.LAST:
        bt_model = None

    compression_mode_recording = None if args.compression_mode_recording is None else sl.SVO_COMPRESSION_MODE.from_string(args.compression_mode_recording.upper())
    if compression_mode_recording is not None and compression_mode_recording == sl.SVO_COMPRESSION_MODE.LAST:
        compression_mode_recording = None

    encoding_preset = None if args.encoding_preset is None else sl.SVO_ENCODING_PRESET.from_string(args.encoding_preset.upper())
    if encoding_preset is not None and encoding_preset == sl.SVO_ENCODING_PRESET.LAST:
        encoding_preset = None

    compression_mode_streaming = None if args.compression_mode_streaming is None else sl.STREAMING_CODEC.from_string(args.compression_mode_streaming.upper())
    if compression_mode_streaming is not None and compression_mode_streaming == sl.STREAMING_CODEC.LAST:
        compression_mode_streaming = None

    # Parse custom ONNX input shape if provided
    od_custom_onnx_input_shape = None
    if args.od_custom_onnx_input_shape:
        parts = args.od_custom_onnx_input_shape.lower().split('x')
        if len(parts) == 2:
            od_custom_onnx_input_shape = sl.Resolution(int(parts[0]), int(parts[1]))

    if args.camera_one:
        print("[CameraOne] Mono camera detected. Stereo-only features (depth, OD, BT, tracking) are not available.")
        run_mono_benchmark(args, resolution, compression_mode_recording, compression_mode_streaming, encoding_preset)
    else:
        run_stereo_benchmark(args, resolution, depth_mode, od_model, bt_model,
                             compression_mode_recording, compression_mode_streaming,
                             args.od_custom_onnx_file, od_custom_onnx_input_shape, encoding_preset)


if __name__ == "__main__":
    main()
