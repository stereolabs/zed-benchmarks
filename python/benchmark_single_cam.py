import argparse
import json
import math
import os
import time
import signal
import sys
import pyzed.sl as sl

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
    """Compute mean, median, stddev from a list of floats."""
    if not samples:
        return {"mean": 0, "median": 0, "stddev": 0}
    n = len(samples)
    mean = sum(samples) / n
    s = sorted(samples)
    median = (s[n//2-1] + s[n//2]) / 2.0 if n % 2 == 0 else s[n//2]
    stddev = math.sqrt(sum((v - mean) ** 2 for v in samples) / n)
    return {"mean": round(mean, 3), "median": round(median, 3), "stddev": round(stddev, 3)}


def _write_timer_json(filename, grab_key, frames_count, total_seconds,
                     grab_ms, period_ms, ri_ms, rb_ms, ro_ms, gp_ms):
    """Write timer-mode JSON compatible with parse_results.py."""
    freq = frames_count / total_seconds if total_seconds > 0 else 0
    gs = _compute_stats(grab_ms)
    ps = _compute_stats(period_ms)
    data = {
        grab_key: {
            "grab_call_frequency_hz": round(freq, 3),
            "grab_call_mean_period_ms": ps["mean"],
            "grab_call_median_period_ms": ps["median"],
            "cpu_stats": gs,
        }
    }
    for name, samples in [("RetrieveImage", ri_ms), ("RetrieveBodies", rb_ms),
                           ("RetrieveObjects", ro_ms), ("GetPosition", gp_ms)]:
        if samples:
            data[name] = {"cpu_stats": _compute_stats(samples)}
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[Timer] Results written to {filename}")


def _append_imu_json(filename, imu_key, imu_sample_count, total_seconds, imu_per_frame):
    """Append IMU stats to an existing timer JSON file."""
    with open(filename, "r") as f:
        data = json.load(f)
    rate = imu_sample_count / total_seconds if total_seconds > 0 else 0.0
    avg_per_frame = sum(imu_per_frame) / len(imu_per_frame) if imu_per_frame else 0.0
    data[imu_key] = {
        "imu_sample_count": imu_sample_count,
        "imu_rate_hz": round(rate, 2),
        "imu_per_frame": round(avg_per_frame, 4),
    }
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[Timer] IMU data appended to {filename}")


def run_mono_benchmark(args, resolution, compression_mode_recording, compression_mode_streaming):
    """Run benchmark for a mono camera (CameraOne)."""
    global is_running

    zed = sl.CameraOne()
    init_params = sl.InitParametersOne()
    init_params.camera_resolution = resolution
    init_params.camera_fps = args.camera_fps
    if args.svo_input is not None:
        init_params.set_from_svo_file(args.svo_input)
        init_params.svo_real_time_mode = False
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
        rec_params.video_filename = args.output_svo
        err = zed.enable_recording(rec_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable recording: {err}")
            sys.exit(1)
        print(f"Recording enabled ({compression_mode_recording}) to {args.output_svo}")

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

    # Warmup
    if args.warmup > 0:
        print(f"\nStarting warmup loop for {args.warmup} seconds...")
        warmup_start_time = time.time()
        while is_running:
            if (time.time() - warmup_start_time) > args.warmup:
                print("Warmup finished.")
                break
            if zed.grab() > sl.ERROR_CODE.SUCCESS:
                time.sleep(0.001)

    if not args.timer:
        sl.reset_benchmark_timer()

    frames_count = 0
    start_time = time.time()
    last_grab_t = time.perf_counter()
    t_grab, t_period, t_ri = [], [], []
    imu_sample_count = 0
    imu_per_frame = []
    all_sensors_data_in_current_frame = []
    print(f"\nStarting benchmark loop for {args.duration} seconds. Press Ctrl+C to stop.")

    while is_running:
        if args.duration > 0 and (time.time() - start_time) > args.duration:
            print("\nBenchmark finished (duration reached).")
            break

        tg0 = time.perf_counter()
        if zed.grab() <= sl.ERROR_CODE.SUCCESS:
            tg1 = time.perf_counter()
            tri0 = time.perf_counter()
            # Use Resolution(-1,-1) to retrieve at the SDK's working resolution
            # instead of the camera resolution (default). This is more efficient.
            zed.retrieve_image(image, sl.VIEW.LEFT, sl.MEM.CPU, sl.Resolution(-1, -1))
            tri1 = time.perf_counter()
            if args.timer:
                t_grab.append((tg1 - tg0) * 1000)
                t_period.append((tg1 - last_grab_t) * 1000)
                last_grab_t = tg1
                t_ri.append((tri1 - tri0) * 1000)
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
            time.sleep(0.001)

    print(f"\nFinal FPS: {zed.get_current_fps():.2f}")

    if args.timer:
        total_s = time.time() - start_time
        sn = str(cam_info.serial_number)
        _write_timer_json(args.benchmark, f"Grab_SN{sn}", frames_count, total_s,
                          t_grab, t_period, t_ri, [], [], [])
        if args.monitor_imu and imu_sample_count > 0:
            _append_imu_json(args.benchmark, f"IMU_SN{sn}", imu_sample_count, total_s, imu_per_frame)

    if compression_mode_recording is not None:
        zed.disable_recording()
    if compression_mode_streaming is not None:
        zed.disable_streaming()
    zed.close()


def run_stereo_benchmark(args, resolution, depth_mode, od_model, bt_model,
                         compression_mode_recording, compression_mode_streaming,
                         od_custom_onnx_file=None, od_custom_onnx_input_shape=None):
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
        rec_params.video_filename = args.output_svo
        err = zed.enable_recording(rec_params)
        if err != sl.ERROR_CODE.SUCCESS:
            print(f"Failed to enable recording: {err}")
            sys.exit(1)
        print(f"Recording enabled ({compression_mode_recording}) to {args.output_svo}")

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

    # Runtime Data Containers
    runtime_params = sl.RuntimeParameters()
    image = sl.Mat()
    depth = sl.Mat()
    objects = sl.Objects()
    bodies = sl.Bodies()
    pose = sl.Pose()

    # Barrier sync: wait for all processes before starting
    sync_barrier_signal_and_wait(args.sync_dir)

    # Warmup
    if args.warmup > 0:
        print(f"\nStarting warmup loop for {args.warmup} seconds...")
        warmup_start_time = time.time()
        while is_running:
            if (time.time() - warmup_start_time) > args.warmup:
                print("Warmup finished.")
                break
            if zed.grab(runtime_params) > sl.ERROR_CODE.SUCCESS:
                time.sleep(0.001)

    if not args.timer:
        sl.reset_benchmark_timer()

    frames_count = 0
    start_time = time.time()
    last_grab_t = time.perf_counter()
    t_grab, t_period, t_ri, t_rb, t_ro, t_gp = [], [], [], [], [], []
    imu_sample_count = 0
    imu_per_frame = []
    all_sensors_data_in_current_frame = []
    print(f"\nStarting benchmark loop for {args.duration} seconds. Press Ctrl+C to stop.")

    while is_running:
        if args.duration > 0 and (time.time() - start_time) > args.duration:
            print("\nBenchmark finished (duration reached).")
            break

        tg0 = time.perf_counter()
        if zed.grab(runtime_params) <= sl.ERROR_CODE.SUCCESS:
            tg1 = time.perf_counter()
            tri0 = time.perf_counter()
            # Use Resolution(-1,-1) to retrieve at the SDK's working resolution
            # instead of the camera resolution (default). This is more efficient.
            zed.retrieve_image(image, sl.VIEW.LEFT, sl.MEM.CPU, sl.Resolution(-1, -1))
            tri1 = time.perf_counter()

            if init_params.depth_mode != sl.DEPTH_MODE.NONE:
                zed.retrieve_measure(depth, sl.MEASURE.XYZ, sl.MEM.CPU, sl.Resolution(-1, -1))

            if pt_enabled:
                tp0 = time.perf_counter()
                zed.get_position(pose, sl.REFERENCE_FRAME.WORLD)
                tp1 = time.perf_counter()
                if args.timer:
                    t_gp.append((tp1 - tp0) * 1000)

            if od_model is not None:
                to0 = time.perf_counter()
                zed.retrieve_objects(objects, sl.ObjectDetectionRuntimeParameters())
                to1 = time.perf_counter()
                if args.timer:
                    t_ro.append((to1 - to0) * 1000)

            if bt_model is not None:
                tb0 = time.perf_counter()
                zed.retrieve_bodies(bodies, sl.BodyTrackingRuntimeParameters())
                tb1 = time.perf_counter()
                if args.timer:
                    t_rb.append((tb1 - tb0) * 1000)

            if args.timer:
                t_grab.append((tg1 - tg0) * 1000)
                t_period.append((tg1 - last_grab_t) * 1000)
                last_grab_t = tg1
                t_ri.append((tri1 - tri0) * 1000)

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
            time.sleep(0.001)

    print(f"\nFinal FPS: {zed.get_current_fps():.2f}")

    if args.timer:
        total_s = time.time() - start_time
        sn = str(zed.get_camera_information().serial_number)
        _write_timer_json(args.benchmark, f"Grab_SN{sn}", frames_count, total_s,
                          t_grab, t_period, t_ri, t_rb, t_ro, t_gp)
        if args.monitor_imu and imu_sample_count > 0:
            _append_imu_json(args.benchmark, f"IMU_SN{sn}", imu_sample_count, total_s, imu_per_frame)

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
    parser.add_argument("--output_svo", type=str, default="benchmark_output.svo", help="Output filename for recording.")
    parser.add_argument("--compression_mode_streaming", type=str, default=None, help="Compression Mode for Streaming (e.g. H265). If set, activates streaming.")
    parser.add_argument("--streaming_port", type=int, default=30000, help="Streaming port (default: 30000)")
    parser.add_argument("--serial_number", type=int, default=None, help="Camera serial number (default: auto-detect)")
    parser.add_argument("--enable_positional_tracking", action="store_true", help="Activates positional tracking.")
    parser.add_argument("--camera_one", action="store_true", help="Use CameraOne (mono) API instead of Camera (stereo)")
    parser.add_argument("--warmup", type=float, default=5.0, help="Warmup duration in seconds (default: 5.0)")
    parser.add_argument("--duration", type=float, default=60.0, help="Duration of the benchmark in seconds. -1 for infinite.")
    parser.add_argument("--benchmark", type=str, default="benchmark_report.json", help="Benchmark report filename")
    parser.add_argument("--sync_dir", type=str, default=None, help="Barrier sync directory for multi-process coordination")
    parser.add_argument("--timer", action="store_true", help="Use manual timer instead of SDK benchmark mode")
    parser.add_argument("--no_monitor_imu", action="store_true", help="Disable IMU monitoring (default: ON)")
    args = parser.parse_args()
    args.monitor_imu = not args.no_monitor_imu

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

    compression_mode_streaming = None if args.compression_mode_streaming is None else sl.STREAMING_CODEC.from_string(args.compression_mode_streaming.upper())
    if compression_mode_streaming is not None and compression_mode_streaming == sl.STREAMING_CODEC.LAST:
        compression_mode_streaming = None

    if not args.timer:
        os.environ["ZED_SDK_BENCHMARK_MODE"] = args.benchmark

    # Parse custom ONNX input shape if provided
    od_custom_onnx_input_shape = None
    if args.od_custom_onnx_input_shape:
        parts = args.od_custom_onnx_input_shape.lower().split('x')
        if len(parts) == 2:
            od_custom_onnx_input_shape = sl.Resolution(int(parts[0]), int(parts[1]))

    if args.camera_one:
        print("[CameraOne] Mono camera detected. Stereo-only features (depth, OD, BT, tracking) are not available.")
        run_mono_benchmark(args, resolution, compression_mode_recording, compression_mode_streaming)
    else:
        run_stereo_benchmark(args, resolution, depth_mode, od_model, bt_model,
                             compression_mode_recording, compression_mode_streaming,
                             args.od_custom_onnx_file, od_custom_onnx_input_shape)


if __name__ == "__main__":
    main()
