import argparse
import json
import os
import time
import signal
import sys
import threading
from collections import Counter
from typing import Optional
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


def _split_svo_paths(csv):
    """Split a comma-separated --svo_input value into individual paths (one per camera),
    trimming whitespace and dropping empty entries."""
    if not csv:
        return []
    return [p.strip() for p in csv.split(",") if p.strip()]


def _svo_path_is_mono(path):
    """Recover whether an SVO was recorded from a mono camera. There is no way to ask the SDK
    without opening (a mono SVO opens only via CameraOne, a stereo one only via Camera), so we read
    the type token this tool writes into the recorded filename
    (benchmark_output_<stereo|mono>_<SN>.svo2). Defaults to stereo when the token is absent."""
    return "_mono" in os.path.basename(path).lower()


def _compute_stats(samples):
    """Return (mean, median, stddev) over `samples`; sorts the list in place. Empty -> (0,0,0).
    Matches benchmark_single_cam.py._compute_stats so both reports use the same population stats."""
    n = len(samples)
    if n == 0:
        return (0.0, 0.0, 0.0)
    samples.sort()
    mean = sum(samples) / n
    median = (samples[n // 2 - 1] + samples[n // 2]) / 2.0 if n % 2 == 0 else samples[n // 2]
    stddev = (sum((v - mean) ** 2 for v in samples) / n) ** 0.5
    return (mean, median, stddev)


# ── Live-timestamp correspondence (G2G reconstruction at SVO playback) ────────
# See benchmark_single_cam.py for the rationale. The map holds INTEGER MILLISECONDS for both key
# (IMAGE ts) and value (the after-read "now", without the g2g offset). Mirrors the C++ benchmark.
LIVE_TS_KEY = "live_timestamp_correspondence"


def _serialize_live_ts_map(m):
    return json.dumps({str(k): v for k, v in m.items()})


def _deserialize_live_ts_map(payload):
    try:
        j = json.loads(payload)
        return {int(k): int(v) for k, v in j.items()}
    except (ValueError, TypeError) as e:
        print(f"[LiveTS] Failed to parse live-timestamp correspondences: {e}")
        return None


def _ingest_live_ts_map(zed, m, anchor_ts_ns):
    """Serialize the correspondence map into one SVOData blob and ingest it (recording only),
    anchored at a real in-range frame timestamp so retrieve_svo_data finds it."""
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
    """Retrieve + deserialize the map. Returns the dict, or None (with a clear message) if the
    SVO lacks the live-timestamp data."""
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
    payload = next(iter(svo_data.values())).get_content_as_string()
    m = _deserialize_live_ts_map(payload)
    if not m:
        print(f"[LiveTS] '{LIVE_TS_KEY}' present but empty/corrupt after parse.")
        return None
    print(f"[LiveTS] Loaded {len(m)} live-timestamp correspondences from the SVO.")
    return m


def stereo_worker(camera_index, zed, sn, depth_mode, enable_pt, od_model, bt_model, cfg, state, g2g_display):
    """Worker thread for a stereo Camera. cfg holds the shared run flags; state holds this camera's
    per-thread accumulators (counter / imu / live_ts_map / last_image_ts)."""
    runtime_params = sl.RuntimeParameters()
    image = sl.Mat()
    depth = sl.Mat()
    objects = sl.Objects()
    bodies = sl.Bodies()
    pose = sl.Pose()
    od_runtime_params = sl.ObjectDetectionRuntimeParameters()
    bt_runtime_params = sl.BodyTrackingRuntimeParameters()
    all_sensors_data_in_current_frame = []
    period_open = False

    record_live_ts = cfg["record_live_ts"]
    playback_live_ts = cfg["playback_live_ts"]
    monitor_imu = cfg["monitor_imu"]
    live_ts_map = state["live_ts_map"]
    imu_data = state["imu"]
    counter = state["counter"]
    g2g_async = None
    g2g_sync = None
    if cfg["g2g_active"]:
        if cfg["svo"]:
            # SVO playback: decode synchronously on EVERY frame so replays are repeatable
            # (the async path drops a timing-dependent subset). Inflates the grab loop.
            g2g_sync = g2gmod.Decoder()
        else:
            # Live capture: decode on a worker thread so it never inflates this camera's grab loop.
            g2g_async = g2gmod.AsyncDecoder()
            g2g_async.start(g2g_display, sn, cfg["reserve"], camera_index)

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
    prof_obj = "camera_sn_" + sn + "_retrieveObjects(...)"
    prof_bodies = "camera_sn_" + sn + "_retrieveBodies(...)"
    # Retrieve at the SDK's working resolution. Built once so the grab loop never
    # allocates inside the measured retrieve sections.
    working_res = sl.Resolution(-1, -1)

    print(f"[Cam {camera_index}] Started (stereo).")
    while is_running:
        # Split capture: read() pulls + rectifies the frame (the blocking step); grab() then runs
        # the SDK measurements. Each is profiled separately (.read() / .grab()) and summed in the
        # report into the full capture+process "Grab" cost.
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

            capture_ref_ns = before_read_ns if cfg["svo"] else image_ts_ns
            # Recording: store the raw camera-clock "now" keyed by the IMAGE timestamp.
            if record_live_ts:
                live_ts_map[image_ts_ns // 1_000_000] = after_read_ns // 1_000_000
                state["last_image_ts"] = image_ts_ns

            # Inter-grab period (success-to-success), measured by the profiler.
            if period_open:
                Profiler.stop(prof_period)
            Profiler.start(prof_period)
            period_open = True

            # Retrieve at the SDK's working resolution (see working_res).
            before_img_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
            Profiler.start(prof_img)
            zed.retrieve_image(image, sl.VIEW.LEFT, sl.MEM.CPU, working_res)
            Profiler.stop(prof_img)
            after_img_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()

            Profiler.add_sample(prof_g2i, ((after_read_ns - capture_ref_ns) + (after_img_ns - before_img_ns)) / 1.0e6)
            # Glass-to-glass: submit a deep copy to the async decoder worker (off the grab loop).
            # The after-read "now" (the camera CURRENT clock) and the decoded on-screen time share the
            # host realtime clock, so their difference is the latency directly.
            g2g_now_ns = after_read_ns
            # Playback: replace the playback wall-clock "now" with the recorded live "now".
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
                g2g_async.submit(image, g2g_now_ns)
            elif g2g_sync is not None:
                # SVO: decode every frame here (no async drops) for a repeatable result.
                status, display_ns = g2g_sync.decode(image, g2g_now_ns)
                if status == g2gmod.Status.LOCKED:
                    g2g_lat_ms = (g2g_now_ns - display_ns) / 1.0e6
                    if g2g_lat_ms > 0.0:  # drop non-positive latency
                        Profiler.add_sample(prof_g2g, g2g_lat_ms)
            if depth_mode != sl.DEPTH_MODE.NONE:
                Profiler.start(prof_measure)
                zed.retrieve_measure(depth, sl.MEASURE.XYZ, sl.MEM.CPU, working_res)
                Profiler.stop(prof_measure)
            if enable_pt:
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
            counter["frames"] += 1
            if monitor_imu:
                zed.get_sensors_data_batch(all_sensors_data_in_current_frame)
                if all_sensors_data_in_current_frame is not None:
                    imu_data["count"] += len(all_sensors_data_in_current_frame)
                    imu_data["per_frame"].append(float(len(all_sensors_data_in_current_frame)))
                else:
                    imu_data["per_frame"].append(0.0)
        else:
            # End of SVO: this camera is done. Stop so the run can end once every camera has
            # reached EOF (the whole file is processed -> repeatable replay).
            if cfg["svo"] and grab_status == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                break
            time.sleep(0.001)
    Profiler.stop(prof_period)
    # Stamp this camera's end-of-capture time before the (potentially slow) async-decoder teardown,
    # so its frequency/IMU rate is divided by its own window, not the whole-run window.
    state["end_time"] = time.time()

    # Stop + join this camera's async decoder before the thread exits, so every in-flight latency
    # sample is collected, then hand its latencies to the profiler.
    if g2g_async is not None:
        g2g_async.stop()
        for lat_ms in g2g_async.take_latencies():
            Profiler.add_sample(prof_g2g, lat_ms)


def mono_worker(camera_index, zed, sn, cfg, state, g2g_display):
    """Worker thread for a mono CameraOne. CameraOne has no read() in the Python API, so the "now"
    is sampled right after grab() only (no read()/grab() split): mono's glass-to-image includes
    grab()."""
    image = sl.Mat()
    all_sensors_data_in_current_frame = []
    period_open = False

    record_live_ts = cfg["record_live_ts"]
    playback_live_ts = cfg["playback_live_ts"]
    monitor_imu = cfg["monitor_imu"]
    live_ts_map = state["live_ts_map"]
    imu_data = state["imu"]
    counter = state["counter"]

    g2g_async = None
    g2g_sync = None
    if cfg["g2g_active"]:
        if cfg["svo"]:
            # SVO playback: decode synchronously on EVERY frame so replays are repeatable
            # (the async path drops a timing-dependent subset). Inflates the grab loop.
            g2g_sync = g2gmod.Decoder()
        else:
            g2g_async = g2gmod.AsyncDecoder()
            g2g_async.start(g2g_display, sn, cfg["reserve"], camera_index)

    # Profiled section names are "camera_sn_<sn>_<function>(...)" in the default context;
    # precompute once. (CameraOne has no read() in the Python API, so there is no read(...) section.)
    prof_grab = "camera_sn_" + sn + "_grab(...)"
    prof_period = "camera_sn_" + sn + "_grab_period"
    prof_img = "camera_sn_" + sn + "_retrieveImage(...)"
    # Latency sections, fed with add_sample() rather than start()/stop().
    prof_g2i = "camera_sn_" + sn + "_glass_to_image"
    prof_g2g = "camera_sn_" + sn + "_glass_to_glass"
    # Retrieve at the SDK's working resolution. Built once so the grab loop never
    # allocates inside the measured retrieve sections.
    working_res = sl.Resolution(-1, -1)

    print(f"[Cam {camera_index}] Started (mono).")
    while is_running:
        # Host time right before grab(): replaces the image timestamp as the G2I reference on SVO
        # playback (see below).
        before_grab_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
        Profiler.start(prof_grab)
        grab_status = zed.grab()
        # Don't fold a failed grab (end-of-SVO / no frame) into the grab-timing stats.
        Profiler.stop(prof_grab)    
        # Sample the raw "now" reference right after grab() (CameraOne has no read() split). The G2G
        # offset is NOT folded in here, so the stored live-ts and G2I stay in the camera-clock domain.
        now_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
        if grab_status == sl.ERROR_CODE.SUCCESS:
            image_ts_ns = zed.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds()
            if record_live_ts:
                live_ts_map[image_ts_ns // 1_000_000] = now_ns // 1_000_000
                state["last_image_ts"] = image_ts_ns

            # Inter-grab period (success-to-success), measured by the profiler.
            if period_open:
                Profiler.stop(prof_period)
            Profiler.start(prof_period)
            period_open = True

            capture_ref_ns = before_grab_ns if cfg["svo"] else image_ts_ns

            # Retrieve at the SDK's working resolution (see working_res).
            before_img_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()
            Profiler.start(prof_img)
            zed.retrieve_image(image, sl.VIEW.LEFT, sl.MEM.CPU, working_res)
            Profiler.stop(prof_img)
            after_img_ns = zed.get_timestamp(sl.TIME_REFERENCE.CURRENT).get_nanoseconds()

            Profiler.add_sample(prof_g2i, ((now_ns - capture_ref_ns) + (after_img_ns - before_img_ns)) / 1.0e6)

            # The "now" (the camera CURRENT clock) and the decoded on-screen time share the
            # host realtime clock, so their difference is the latency directly.
            g2g_now_ns = now_ns
            # Playback: replace the playback wall-clock "now" with the recorded live "now".
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
                g2g_async.submit(image, g2g_now_ns)
            elif g2g_sync is not None:
                # SVO: decode every frame here (no async drops) for a repeatable result.
                status, display_ns = g2g_sync.decode(image, g2g_now_ns)
                if status == g2gmod.Status.LOCKED:
                    g2g_lat_ms = (g2g_now_ns - display_ns) / 1.0e6
                    if g2g_lat_ms > 0.0:  # drop non-positive latency
                        Profiler.add_sample(prof_g2g, g2g_lat_ms)
            counter["frames"] += 1
            if monitor_imu:
                zed.get_sensors_data_batch(all_sensors_data_in_current_frame)
                if all_sensors_data_in_current_frame is not None:
                    imu_data["count"] += len(all_sensors_data_in_current_frame)
                    imu_data["per_frame"].append(float(len(all_sensors_data_in_current_frame)))
                else:
                    imu_data["per_frame"].append(0.0)
        else:
            # End of SVO: this camera is done. Stop so the run can end once every camera has
            # reached EOF (the whole file is processed -> repeatable replay).
            if cfg["svo"] and grab_status == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                break
            time.sleep(0.001)
    Profiler.stop(prof_period)
    # Stamp this camera's end-of-capture time before the (potentially slow) async-decoder teardown,
    # so its frequency/IMU rate is divided by its own window, not the whole-run window.
    state["end_time"] = time.time()

    if g2g_async is not None:
        g2g_async.stop()
        for lat_ms in g2g_async.take_latencies():
            Profiler.add_sample(prof_g2g, lat_ms)


class CameraHandle:
    """Holds either a stereo Camera or a mono CameraOne."""
    def __init__(self, is_mono: bool, serial_number: int):
        self.is_mono = is_mono
        self.serial_number = serial_number
        self.stereo: Optional[sl.Camera] = None
        self.mono: Optional[sl.CameraOne] = None
        self.recording_enabled = False  # True only if enable_recording() actually succeeded

    def get_current_fps(self) -> float:
        if self.is_mono:
            assert self.mono is not None
            return self.mono.get_current_fps()
        else:
            assert self.stereo is not None
            return self.stereo.get_current_fps()


def _write_profiler_report(out_file, dump_file, serials, frames_counts, per_cam_seconds):
    """Reshape the profiler JSON dump (per-camera profile_stats) into the
    historical multi-camera report schema (one Grab_SN<sn> per camera) so the
    analysis scripts keep working unchanged. Each camera's grab frequency is its
    own frame count divided by its own capture window (per_cam_seconds[i]), so
    SVO files of different length stay accurate."""
    try:
        with open(dump_file, "r") as f:
            dump = json.load(f)
    except (OSError, ValueError):
        dump = {}

    # All cameras profile into the default context; each section name is
    # "camera_sn_<sn>_<function>(...)", so a single context holds every camera's metrics.
    profiles = (dump.get("profile_stats") or {}).get("default") or {}
    report = {}

    for i, sn in enumerate(serials):
        def prof_key(fn, sn=sn):
            return f"camera_sn_{sn}_{fn}"
        # read() and grab() are profiled separately; the reported "Grab" cost is their field-by-field
        # sum (full capture+process time). Missing sections contribute 0, so a mono camera (grab()
        # only, no read()) reports grab() alone.
        read_s = profiles.get(prof_key("read(...)"), {})
        grab_s = profiles.get(prof_key("grab(...)"), {})
        period = profiles.get(prof_key("grab_period"), {})
        cam_s = per_cam_seconds[i]
        freq = frames_counts[i] / cam_s if cam_s > 0 else 0.0
        report[f"Grab_SN{sn}"] = {
            "grab_call_frequency_hz": round(freq, 3),
            "grab_call_mean_period_ms": period.get("mean", 0.0),
            "grab_call_median_period_ms": period.get("median", 0.0),
            "cpu_stats": {
                "mean": read_s.get("mean", 0.0) + grab_s.get("mean", 0.0),
                "median": read_s.get("median", 0.0) + grab_s.get("median", 0.0),
                "stddev": read_s.get("stddev", 0.0) + grab_s.get("stddev", 0.0),
                "min": read_s.get("min", 0.0) + grab_s.get("min", 0.0),
                "max": read_s.get("max", 0.0) + grab_s.get("max", 0.0),
                "p99": read_s.get("p99", 0.0) + grab_s.get("p99", 0.0),
                "nb_samples": grab_s.get("count", read_s.get("count", 0)),
            },
        }

        # Per-operation sections (only those actually profiled for this camera appear). The profiled
        # section is "camera_sn_<sn>_<function>(...)"; the report key is "<ReportName>_SN<sn>", so each
        # camera's per-event timings stay serial-keyed and coherent with the single-camera report
        # schema. A mono camera only profiles retrieveImage, so only that key appears for it.
        for prof_name, report_name in (
            ("retrieveImage(...)",   "RetrieveImage"),
            ("retrieveMeasure(...)", "RetrieveMeasure"),
            ("retrieveObjects(...)", "RetrieveObjects"),
            ("retrieveBodies(...)",  "RetrieveBodies"),
            ("getPosition(...)",     "GetPosition"),
        ):
            s = profiles.get(prof_key(prof_name))
            if s is not None:
                report[f"{report_name}_SN{sn}"] = {
                    "cpu_stats": {
                        "mean": s.get("mean", 0.0),
                        "median": s.get("median", 0.0),
                        "stddev": s.get("stddev", 0.0),
                        "min": s.get("min", 0.0),
                        "max": s.get("max", 0.0),
                        "p99": s.get("p99", 0.0),
                        "nb_samples": s.get("count", 0),
                    }
                }
        # GrabProcessing is the grab() call alone (SDK processing, without the read() capture step),
        # so it can be stacked on top of glass-to-image without counting read() twice. Only emitted
        # when read() was profiled separately: on mono (no read() split) grab() is already in G2I.
        if read_s and grab_s:
            report[f"GrabProcessing_SN{sn}"] = {
                "cpu_stats": {
                    "mean": grab_s.get("mean", 0.0),
                    "median": grab_s.get("median", 0.0),
                    "stddev": grab_s.get("stddev", 0.0),
                    "min": grab_s.get("min", 0.0),
                    "max": grab_s.get("max", 0.0),
                    "p99": grab_s.get("p99", 0.0),
                    "nb_samples": grab_s.get("count", 0),
                }
            }

        # Latency sections, fed to the profiler with add_sample() (see the workers). Statistics are
        # computed over the WHOLE sample set: the G2G decoder only emits a sample after the per-column
        # agreement + CRC + plausibility checks pass, so mis-detections are rejected at decode time
        # and no statistical tail-trim is needed (matches the C++ benchmark).
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


def main():
    global is_running
    parser = argparse.ArgumentParser(description="ZED Multi-Camera Benchmark")
    parser.add_argument("--num_stereo", type=int, default=-1, help="Number of stereo (ZED X) cameras (-1 for all detected, default: -1)")
    parser.add_argument("--num_mono", type=int, default=-1, help="Number of mono (ZED X One) cameras (-1 for all detected, default: -1)")
    parser.add_argument("--svo_input", type=str, default=None, help="SVO file(s), comma-separated, one per camera (default: live)")
    parser.add_argument("--camera_fps", type=int, default=30, help="Camera FPS (e.g. 15, 30, 60)")
    parser.add_argument("--resolution", type=str, default="HD1080", help="Resolution (e.g. HD1200)")
    parser.add_argument("--depth_mode", type=str, default=None, help="Depth Mode (e.g. NEURAL_LIGHT)")
    parser.add_argument("--od_model", type=str, default=None, help="Object Detection Model (e.g. MULTI_CLASS_BOX_FAST)")
    parser.add_argument("--od_custom_onnx_file", type=str, default=None, help="Path to custom ONNX file (requires od_model=CUSTOM_YOLOLIKE_BOX_OBJECTS).")
    parser.add_argument("--od_custom_onnx_input_shape", type=str, default=None, help="Input resolution for custom ONNX as WxH (e.g. 640x640, default: 512x512).")
    parser.add_argument("--bt_model", type=str, default=None, help="Body Tracking Model (e.g. HUMAN_BODY_FAST)")
    parser.add_argument("--compression_mode_recording", type=str, default=None, help="Compression Mode for Recording (e.g. H265)")
    parser.add_argument("--encoding_preset", type=str, default=None, help="Encoding preset: DEFAULT, ULTRAFAST, FAST, MEDIUM, SLOW (default: DEFAULT)")
    parser.add_argument("--encryption_key", type=str, default="", help="Encryption key/passphrase for SVO recording / decryption (default: empty)")
    parser.add_argument("--compression_mode_streaming", type=str, default=None, help="Compression Mode for Streaming (e.g. H265)")
    parser.add_argument("--enable_positional_tracking", action="store_true", help="Enable Positional Tracking (stereo only).")
    parser.add_argument("--warmup", type=float, default=5.0, help="Warmup duration in seconds (default: 5.0)")
    parser.add_argument("--duration", type=float, default=60.0, help="Duration of the benchmark in seconds. -1 for infinite.")
    parser.add_argument("--benchmark", type=str, default="benchmark_report.json", help="Benchmark report filename")
    parser.add_argument("--no_monitor_imu", action="store_true", help="Disable IMU monitoring (default: ON)")
    parser.add_argument("--glass_to_glass_latency", action="store_true", help="Measure glass-to-glass latency via an on-screen pattern (default: OFF)")
    args = parser.parse_args()
    monitor_imu = not args.no_monitor_imu
    measure_glass_to_glass_latency = args.glass_to_glass_latency
    if measure_glass_to_glass_latency and not g2gmod.G2G_AVAILABLE:
        print("[G2G] --glass_to_glass_latency requested but glfw / PyOpenGL / OpenCV are missing.\n"
              "      Install them (see python/requirements.txt). Continuing without G2G.")
        measure_glass_to_glass_latency = False

    # Parse enums
    resolution = sl.RESOLUTION.from_string(args.resolution.upper())
    if resolution is None or resolution == sl.RESOLUTION.LAST:
        resolution = sl.RESOLUTION.HD1080

    depth_mode = sl.DEPTH_MODE.NONE if args.depth_mode is None else sl.DEPTH_MODE.from_string(args.depth_mode.upper())
    if depth_mode is None or depth_mode == sl.DEPTH_MODE.LAST:
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

    enable_pt = args.enable_positional_tracking or od_model is not None or bt_model is not None

    # Parse custom ONNX input shape if provided
    od_custom_onnx_input_shape = None
    if args.od_custom_onnx_input_shape:
        parts = args.od_custom_onnx_input_shape.lower().split('x')
        if len(parts) == 2:
            od_custom_onnx_input_shape = sl.Resolution(int(parts[0]), int(parts[1]))

    # Comma-separated --svo_input → one SVO path per camera (empty = live capture).
    svo_inputs = _split_svo_paths(args.svo_input)
    svo_mode = len(svo_inputs) > 0

    # Discover cameras
    all_devices = []
    num_stereo = args.num_stereo
    num_mono = args.num_mono

    if not svo_mode:
        stereo_devs = sl.Camera.get_device_list()
        mono_devs = sl.CameraOne.get_device_list()

        avail_stereo = len(stereo_devs)
        avail_mono = len(mono_devs)

        # -1 = use all detected stereo cameras
        if num_stereo < 0:
            num_stereo = avail_stereo
        num_stereo = min(num_stereo, avail_stereo)
        if num_mono < 0:
            num_mono = avail_mono
        num_mono = min(num_mono, avail_mono)

        # Build device list: stereo first, then mono
        for s in range(num_stereo):
            all_devices.append({"serial": stereo_devs[s].serial_number, "is_mono": False, "svo_path": None})
        for m in range(num_mono):
            all_devices.append({"serial": mono_devs[m].serial_number, "is_mono": True, "svo_path": None})

        n_cams = num_stereo + num_mono

        if n_cams == 0:
            print("No ZED cameras detected (or requested 0).")
            return

        print(f"Detected {avail_stereo} stereo + {avail_mono} mono camera(s). "
              f"Using {num_stereo} stereo + {num_mono} mono ({n_cams} total).")
    else:
        # SVO playback: one camera per input file. The mono/stereo type is recovered from the
        # filename token written at record time (see _svo_path_is_mono); --num_stereo/--num_mono
        # are ignored here since the file set defines the topology.
        for path in svo_inputs:
            all_devices.append({"serial": 0, "is_mono": _svo_path_is_mono(path), "svo_path": path})
        n_cams = len(all_devices)
        num_mono = sum(1 for d in all_devices if d["is_mono"])
        num_stereo = n_cams - num_mono
        print(f"SVO playback: {n_cams} file(s) ({num_stereo} stereo + {num_mono} mono).")

    # Open all cameras
    cameras = []
    for i in range(n_cams):
        dev = all_devices[i]
        handle = CameraHandle(dev["is_mono"], dev["serial"])

        if dev["is_mono"]:
            # --- CameraOne ---
            handle.mono = sl.CameraOne()
            init_one = sl.InitParametersOne()
            init_one.camera_resolution = resolution
            init_one.camera_fps = args.camera_fps
            if dev["svo_path"] is not None:
                init_one.set_from_svo_file(dev["svo_path"])
                init_one.svo_real_time_mode = False
                if args.encryption_key:
                    init_one.svo_decryption_key = args.encryption_key
            else:
                init_one.set_from_serial_number(dev["serial"])

            print(f"[Cam {i}] Opening CameraOne (SN: {dev['serial']})...")
            status = handle.mono.open(init_one)
            if status != sl.ERROR_CODE.SUCCESS:
                print(f"[Cam {i}] Failed to open CameraOne: {status}")
                for j in range(i):
                    if cameras[j].mono: cameras[j].mono.close()
                    if cameras[j].stereo: cameras[j].stereo.close()
                sys.exit(1)
            info = handle.mono.get_camera_information()
            # Recover the real serial from the opened camera/SVO header. On SVO playback the device
            # list carries serial 0, so without this every camera collapses onto the same "_SN0"
            # report keys and profiler sections, merging their metrics.
            handle.serial_number = info.serial_number
            print(f"[Cam {i}] Opened CameraOne (SN: {handle.serial_number}, {info.camera_model})")

            # Recording (mono)
            if compression_mode_recording is not None:
                rec_params = sl.RecordingParameters()
                rec_params.compression_mode = compression_mode_recording
                if encoding_preset is not None:
                    rec_params.encoding_preset = encoding_preset
                # Type token (mono/stereo) lets --svo_input recover the camera class at playback.
                filename = f"benchmark_output_mono_{dev['serial']}.svo2"
                rec_params.video_filename = filename
                if args.encryption_key:
                    rec_params.encryption_key = args.encryption_key
                err = handle.mono.enable_recording(rec_params)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable recording: {err}")
                else:
                    handle.recording_enabled = True
                    print(f"[Cam {i}] Recording enabled{' (encrypted)' if args.encryption_key else ''} to {filename}")

            # Streaming (mono)
            if compression_mode_streaming is not None:
                sp = sl.StreamingParameters()
                sp.codec = compression_mode_streaming
                sp.port = 30000 + i * 2
                err = handle.mono.enable_streaming(sp)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable streaming: {err}")
                else:
                    print(f"[Cam {i}] Streaming enabled ({compression_mode_streaming})")

            # Stereo-only features not available
            if depth_mode != sl.DEPTH_MODE.NONE:
                print(f"[Cam {i}] [SKIP] Depth - mono camera")
            if od_model is not None:
                print(f"[Cam {i}] [SKIP] Object Detection - mono camera")
            if bt_model is not None:
                print(f"[Cam {i}] [SKIP] Body Tracking - mono camera")
            if enable_pt:
                print(f"[Cam {i}] [SKIP] Positional Tracking - mono camera")

        else:
            # --- stereo Camera ---
            handle.stereo = sl.Camera()
            init_params = sl.InitParameters()
            init_params.camera_resolution = resolution
            init_params.camera_fps = args.camera_fps
            init_params.depth_mode = depth_mode
            if dev["svo_path"] is not None:
                init_params.set_from_svo_file(dev["svo_path"])
                init_params.svo_real_time_mode = False
                if args.encryption_key:
                    init_params.svo_decryption_key = args.encryption_key
            else:
                init_params.set_from_serial_number(dev["serial"])

            print(f"[Cam {i}] Opening Camera (SN: {dev['serial']})...")
            status = handle.stereo.open(init_params)
            if status != sl.ERROR_CODE.SUCCESS:
                print(f"[Cam {i}] Failed to open Camera: {status}")
                for j in range(i):
                    if cameras[j].mono: cameras[j].mono.close()
                    if cameras[j].stereo: cameras[j].stereo.close()
                sys.exit(1)
            info = handle.stereo.get_camera_information()
            # Recover the real serial from the opened camera/SVO header. On SVO playback the device
            # list carries serial 0, so without this every camera collapses onto the same "_SN0"
            # report keys and profiler sections, merging their metrics.
            handle.serial_number = info.serial_number
            print(f"[Cam {i}] Opened Camera (SN: {handle.serial_number}, {info.camera_model})")

            # Recording (stereo)
            if compression_mode_recording is not None:
                rec_params = sl.RecordingParameters()
                rec_params.compression_mode = compression_mode_recording
                if encoding_preset is not None:
                    rec_params.encoding_preset = encoding_preset
                # Type token (mono/stereo) lets --svo_input recover the camera class at playback.
                filename = f"benchmark_output_stereo_{dev['serial']}.svo2"
                rec_params.video_filename = filename
                if args.encryption_key:
                    rec_params.encryption_key = args.encryption_key
                err = handle.stereo.enable_recording(rec_params)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable recording: {err}")
                else:
                    handle.recording_enabled = True
                    print(f"[Cam {i}] Recording enabled{' (encrypted)' if args.encryption_key else ''} to {filename}")

            # Streaming (stereo)
            if compression_mode_streaming is not None:
                sp = sl.StreamingParameters()
                sp.codec = compression_mode_streaming
                sp.port = 30000 + i * 2
                err = handle.stereo.enable_streaming(sp)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable streaming: {err}")
                else:
                    print(f"[Cam {i}] Streaming enabled ({compression_mode_streaming})")

            # Positional Tracking (stereo)
            pt_ok = False
            if enable_pt:
                pt_params = sl.PositionalTrackingParameters()
                pt_params.enable_area_memory = True
                err = handle.stereo.enable_positional_tracking(pt_params)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable PT: {err}")
                else:
                    pt_ok = True

            # Object Detection (stereo)
            if od_model is not None:
                if not pt_ok:
                    pt_params = sl.PositionalTrackingParameters()
                    pt_params.enable_area_memory = True
                    handle.stereo.enable_positional_tracking(pt_params)
                    pt_ok = True
                od_params = sl.ObjectDetectionParameters()
                od_params.detection_model = od_model
                od_params.enable_tracking = True
                if args.od_custom_onnx_file is not None:
                    od_params.custom_onnx_file = args.od_custom_onnx_file
                    if od_custom_onnx_input_shape is not None:
                        od_params.custom_onnx_dynamic_input_shape = od_custom_onnx_input_shape
                    print(f"[Cam {i}] Custom ONNX: {args.od_custom_onnx_file} ({od_params.custom_onnx_dynamic_input_shape.width}x{od_params.custom_onnx_dynamic_input_shape.height})")
                err = handle.stereo.enable_object_detection(od_params)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable OD: {err}")

            # Body Tracking (stereo)
            if bt_model is not None:
                if not pt_ok:
                    pt_params = sl.PositionalTrackingParameters()
                    pt_params.enable_area_memory = True
                    handle.stereo.enable_positional_tracking(pt_params)
                    pt_ok = True
                bt_params = sl.BodyTrackingParameters()
                bt_params.detection_model = bt_model
                bt_params.enable_tracking = True
                err = handle.stereo.enable_body_tracking(bt_params)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable BT: {err}")

        cameras.append(handle)

    # Report keys and profiler sections are disambiguated only by serial. If two cameras share one
    # (e.g. the same SVO passed twice, or two SVOs recorded from the same camera) their metrics
    # would silently merge into a single "_SN<sn>" block: warn rather than emit a malformed report.
    sn_counts = Counter(cam.serial_number for cam in cameras)
    for sn, count in sn_counts.items():
        if count > 1:
            print(f"[Warning] Serial {sn} is shared by {count} cameras; their metrics will be "
                  f"merged under a single \"_SN{sn}\" key in the report.")

    # Live-timestamp correspondence (per camera): record the map while recording; reconstruct
    # G2G from it at SVO playback. Load the recorded map per camera now, missing data means
    # G2G is unreconstructible, so fail fast before the loop.
    record_live_ts = compression_mode_recording is not None
    playback_live_ts = svo_mode and measure_glass_to_glass_latency

    # Recording was requested but failed to enable on one or more cameras (only logged per-camera
    # above). The recorded SVO set is then incomplete and those cameras' live-timestamp map can't be
    # ingested, so their G2G replay is unavailable: warn loudly rather than letting the run look
    # fully successful with silently missing output.
    if record_live_ts:
        rec_failed = sum(1 for c in cameras if not c.recording_enabled)
        if rec_failed:
            print(f"[Warning] Recording failed on {rec_failed} of {n_cams} camera(s); the recorded SVO "
                  f"set is incomplete and those cameras' glass-to-glass replay will be "
                  f"unavailable.")

    live_ts_maps = [{} for _ in range(n_cams)]
    if playback_live_ts:
        for i in range(n_cams):
            cam = cameras[i].mono if cameras[i].is_mono else cameras[i].stereo
            m = _load_live_ts_map(cam)
            if m is None:
                for c in cameras:
                    if c.mono: c.mono.close()
                    if c.stereo: c.stereo.close()
                sys.exit(1)
            live_ts_maps[i] = m

    # Warmup. Skip on SVO playback: there is no hardware to warm up, and warmup would consume
    # frames from the file (svo_real_time_mode is off), shrinking the benchmark window.
    if args.warmup > 0 and not svo_mode:
        print(f"\nStarting warmup loop for {args.warmup} seconds...")
        warmup_threads = []
        for i in range(n_cams):
            def warmup_worker(cam_idx, cam_handle):
                print(f"[Cam {cam_idx}] Warmup Started.")
                warmup_start = time.time()
                while is_running:
                    if (time.time() - warmup_start) > args.warmup:
                        print(f"[Cam {cam_idx}] Warmup Finished.")
                        break
                    if cam_handle.is_mono:
                        err = cam_handle.mono.grab()
                    else:
                        err = cam_handle.stereo.grab()
                    if err > sl.ERROR_CODE.SUCCESS:
                        time.sleep(0.001)

            t = threading.Thread(target=warmup_worker, args=(i, cameras[i]))
            warmup_threads.append(t)
            t.start()
        for t in warmup_threads:
            t.join()
    elif args.warmup > 0:
        print("\nSVO input detected: skipping warmup.")

    # Enable scoped profiling after warmup so warmup grabs are not measured.
    Profiler.enable()

    # Glass-to-glass: one shared display for the process; each worker decodes the same on-screen
    # digit-marker timestamp on its own async decoder thread.
    g2g_display = None
    g2g_active = False
    if measure_glass_to_glass_latency:
        # On SVO playback the filmed screen is baked into the recorded frames; decode those rather
        # than driving a live screen, no display window is opened. The window is only for live capture.
        g2g_ok = True
        if not svo_mode:
            g2g_display = g2gmod.Display()
            # One on-screen readout row per camera, in worker-index order, labeled by serial.
            g2g_display.set_cameras([str(cameras[i].serial_number) for i in range(n_cams)])
            if not g2g_display.start():
                print("[G2G] Failed to open the display window; disabling G2G.")
                g2g_display = None
                g2g_ok = False
        else:
            print("[G2G] SVO playback: decoding the recorded screen; no display window opened.")
        if g2g_ok:
            print("[G2G] enabled")
            g2g_active = True

    # Main benchmark threads. cfg holds the shared run flags; each camera gets a per-thread state
    # dict for its accumulators (counter / imu / live_ts_map / last_image_ts).
    threads = []
    cfg = {
        "measure_glass_to_glass_latency": measure_glass_to_glass_latency,
        "record_live_ts": record_live_ts,
        "playback_live_ts": playback_live_ts,
        "g2g_active": g2g_active,
        "monitor_imu": monitor_imu,
        "reserve": max(1, int(max(0, args.duration) * args.camera_fps * 1.1)),
        "svo": svo_mode,
    }
    states = [{
        "imu": {"count": 0, "per_frame": []},
        "counter": {"frames": 0},
        "live_ts_map": live_ts_maps[i],
        "last_image_ts": 0,
        # Stamped by each worker the instant its grab loop ends. In SVO playback shorter files
        # finish first, so frequency/IMU rate must be divided by each camera's own window (matches
        # the C++ benchmark's per-camera cam_end_times). None until the worker stamps it.
        "end_time": None,
    } for i in range(n_cams)]
    start_time = time.time()
    for i in range(n_cams):
        sn = str(cameras[i].serial_number)
        if cameras[i].is_mono:
            t = threading.Thread(target=mono_worker, args=(i, cameras[i].mono, sn, cfg, states[i], g2g_display))
        else:
            t = threading.Thread(target=stereo_worker,
                                 args=(i, cameras[i].stereo, sn, depth_mode, enable_pt, od_model, bt_model, cfg, states[i], g2g_display))
        threads.append(t)
        t.start()

    # Display FPS from the worker-owned frame counters (delta over the sample interval) rather than
    # calling get_current_fps() on the camera from this thread: the workers are concurrently grabbing
    # on those same camera objects and pyzed is not documented thread-safe for concurrent access.
    last_counts = [states[i]["counter"]["frames"] for i in range(n_cams)]
    last_sample = time.time()
    try:
        while is_running:
            time.sleep(1)
            now = time.time()
            dt = now - last_sample
            last_sample = now
            for i in range(n_cams):
                tag = "mono" if cameras[i].is_mono else "stereo"
                cur = states[i]["counter"]["frames"]
                fps = (cur - last_counts[i]) / dt if dt > 0 else 0.0
                last_counts[i] = cur
                print(f"[Cam {i} ({tag})] FPS: {fps:.2f}", end="")
                if i < n_cams - 1:
                    print(" | ", end="")
            print("\r", end="")
            sys.stdout.flush()

            elapsed = now - start_time
            # SVO playback: ignore --duration and let the run end when all camera threads have
            # reached end-of-file (the is_alive() check below) so each file is processed in full
            # and a replay covers the exact same frames (repeatability).
            if not svo_mode and args.duration > 0 and elapsed > args.duration:
                print("\nBenchmark finished (duration reached).")
                is_running = False
                break
            if not any(t.is_alive() for t in threads):
                if svo_mode:
                    print("\nBenchmark finished (all SVOs reached end of file).")
                break
    except KeyboardInterrupt:
        is_running = False

    print("\nStopping...")
    for t in threads:
        t.join()

    if g2g_display is not None:
        g2g_display.stop()

    run_end = time.time()
    # Per-camera elapsed: in SVO playback cameras finish at different times, so each camera's
    # frequency/IMU rate is divided by its own window (a worker that never stamped end_time:
    # e.g. it crashed: falls back to the whole-run end). Mirrors C++ per_cam_seconds.
    per_cam_seconds = [
        (states[i]["end_time"] if states[i]["end_time"] is not None else run_end) - start_time
        for i in range(n_cams)
    ]

    # Dump profiler stats while the cameras are still open (closing the last one
    # resets the profiler), then reshape into the historical report schema.
    raw_report = args.benchmark + ".profiler.json"
    Profiler.dump_stats(raw_report)

    for i, cam in enumerate(cameras):
        # Persist the live-timestamp correspondence map into the SVO before recording is disabled
        # (ingest_data_into_svo is only valid while recording). Gate on the per-camera flag: a camera
        # whose enable_recording() failed is not recording, so ingesting would just error out.
        if record_live_ts and cam.recording_enabled:
            target = cam.mono if cam.is_mono else cam.stereo
            _ingest_live_ts_map(target, states[i]["live_ts_map"], states[i]["last_image_ts"])
        if cam.is_mono:
            if cam.recording_enabled:
                cam.mono.disable_recording()
            if compression_mode_streaming is not None:
                cam.mono.disable_streaming()
            cam.mono.close()
        else:
            if od_model is not None:
                cam.stereo.disable_object_detection()
            if bt_model is not None:
                cam.stereo.disable_body_tracking()
            if enable_pt:
                cam.stereo.disable_positional_tracking()
            if cam.recording_enabled:
                cam.stereo.disable_recording()
            if compression_mode_streaming is not None:
                cam.stereo.disable_streaming()
            cam.stereo.close()

    print("Benchmark Finished.")

    # Reshape the profiler dump into the historical report schema.
    serials = [str(cam.serial_number) for cam in cameras]
    frames_counts = [states[i]["counter"]["frames"] for i in range(n_cams)]
    _write_profiler_report(args.benchmark, raw_report, serials, frames_counts, per_cam_seconds)
    try:
        os.remove(raw_report)
    except OSError:
        pass

    # Append IMU data to the benchmark report. imu_per_frame is emitted as the full
    # {mean, median, stddev} distribution (3 decimals) to match the single-camera report schema
    # (benchmark_single_cam.py _append_imu_json) so the same key has the same shape across tools.
    if monitor_imu:
        try:
            with open(args.benchmark, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        for i in range(n_cams):
            imu = states[i]["imu"]
            if imu["count"] == 0:
                continue
            sn = str(cameras[i].serial_number)
            cam_s = per_cam_seconds[i]
            rate = imu["count"] / cam_s if cam_s > 0 else 0.0
            ipf_mean, ipf_median, ipf_stddev = _compute_stats(list(imu["per_frame"]))
            data[f"IMU_SN{sn}"] = {
                "imu_sample_count": imu["count"],
                "imu_rate_hz": round(rate, 3),
                "imu_per_frame": {"mean": round(ipf_mean, 3), "median": round(ipf_median, 3), "stddev": round(ipf_stddev, 3)},
            }
        with open(args.benchmark, "w") as f:
            json.dump(data, f, indent=4)
        print(f"[IMU] Data appended to {args.benchmark}")


if __name__ == "__main__":
    main()
