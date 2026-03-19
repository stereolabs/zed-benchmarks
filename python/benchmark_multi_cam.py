import argparse
import json
import os
import time
import signal
import sys
import threading
from typing import Optional
import pyzed.sl as sl

# Global flag to stop the loop
is_running = True

def signal_handler(sig, frame):
    global is_running
    print("\nBenchmark stopped by user.")
    is_running = False

signal.signal(signal.SIGINT, signal_handler)


def stereo_worker(camera_index, zed, depth_mode, enable_pt, od_model, bt_model, monitor_imu, imu_data):
    """Worker thread for a stereo Camera."""
    runtime_params = sl.RuntimeParameters()
    image = sl.Mat()
    depth = sl.Mat()
    objects = sl.Objects()
    bodies = sl.Bodies()
    pose = sl.Pose()
    all_sensors_data_in_current_frame = []

    print(f"[Cam {camera_index}] Started (stereo).")
    while is_running:
        if zed.grab(runtime_params) <= sl.ERROR_CODE.SUCCESS:
            # Use Resolution(-1,-1) for optimal working resolution
            zed.retrieve_image(image, sl.VIEW.LEFT, sl.MEM.CPU, sl.Resolution(-1, -1))
            if depth_mode != sl.DEPTH_MODE.NONE:
                zed.retrieve_measure(depth, sl.MEASURE.XYZ, sl.MEM.CPU, sl.Resolution(-1, -1))
            if enable_pt:
                zed.get_position(pose, sl.REFERENCE_FRAME.WORLD)
            if od_model is not None:
                zed.retrieve_objects(objects, sl.ObjectDetectionRuntimeParameters())
            if bt_model is not None:
                zed.retrieve_bodies(bodies, sl.BodyTrackingRuntimeParameters())
            if monitor_imu:
                zed.get_sensors_data_batch(all_sensors_data_in_current_frame)
                if all_sensors_data_in_current_frame is not None:
                    imu_data["count"] += len(all_sensors_data_in_current_frame)
                    imu_data["per_frame"].append(float(len(all_sensors_data_in_current_frame)))
                else:
                    imu_data["per_frame"].append(0.0)
        else:
            time.sleep(0.001)


def mono_worker(camera_index, zed, monitor_imu, imu_data):
    """Worker thread for a mono CameraOne."""
    image = sl.Mat()
    all_sensors_data_in_current_frame = []

    print(f"[Cam {camera_index}] Started (mono).")
    while is_running:
        if zed.grab() <= sl.ERROR_CODE.SUCCESS:
            # Use Resolution(-1,-1) for optimal working resolution
            zed.retrieve_image(image, sl.VIEW.LEFT, sl.MEM.CPU, sl.Resolution(-1, -1))
            if monitor_imu:
                zed.get_sensors_data_batch(all_sensors_data_in_current_frame)
                if all_sensors_data_in_current_frame is not None:
                    imu_data["count"] += len(all_sensors_data_in_current_frame)
                    imu_data["per_frame"].append(float(len(all_sensors_data_in_current_frame)))
                else:
                    imu_data["per_frame"].append(0.0)
        else:
            time.sleep(0.001)


class CameraHandle:
    """Holds either a stereo Camera or a mono CameraOne."""
    def __init__(self, is_mono: bool, serial_number: int):
        self.is_mono = is_mono
        self.serial_number = serial_number
        self.stereo: Optional[sl.Camera] = None
        self.mono: Optional[sl.CameraOne] = None

    def get_current_fps(self) -> float:
        if self.is_mono:
            assert self.mono is not None
            return self.mono.get_current_fps()
        else:
            assert self.stereo is not None
            return self.stereo.get_current_fps()


def main():
    global is_running
    parser = argparse.ArgumentParser(description="ZED Multi-Camera Benchmark")
    parser.add_argument("--num_stereo", type=int, default=-1, help="Number of stereo (ZED X) cameras (-1 for all detected, default: -1)")
    parser.add_argument("--num_mono", type=int, default=-1, help="Number of mono (ZED X One) cameras (-1 for all detected, default: -1)")
    parser.add_argument("--svo_input", type=str, default=None, help="Path to SVO file (default: live)")
    parser.add_argument("--camera_fps", type=int, default=30, help="Camera FPS (e.g. 15, 30, 60)")
    parser.add_argument("--resolution", type=str, default="HD1080", help="Resolution (e.g. HD1200)")
    parser.add_argument("--depth_mode", type=str, default=None, help="Depth Mode (e.g. NEURAL_LIGHT)")
    parser.add_argument("--od_model", type=str, default=None, help="Object Detection Model (e.g. MULTI_CLASS_BOX_FAST)")
    parser.add_argument("--od_custom_onnx_file", type=str, default=None, help="Path to custom ONNX file (requires od_model=CUSTOM_YOLOLIKE_BOX_OBJECTS).")
    parser.add_argument("--od_custom_onnx_input_shape", type=str, default=None, help="Input resolution for custom ONNX as WxH (e.g. 640x640, default: 512x512).")
    parser.add_argument("--bt_model", type=str, default=None, help="Body Tracking Model (e.g. HUMAN_BODY_FAST)")
    parser.add_argument("--compression_mode_recording", type=str, default=None, help="Compression Mode for Recording (e.g. H265)")
    parser.add_argument("--compression_mode_streaming", type=str, default=None, help="Compression Mode for Streaming (e.g. H265)")
    parser.add_argument("--enable_positional_tracking", action="store_true", help="Enable Positional Tracking (stereo only).")
    parser.add_argument("--warmup", type=float, default=5.0, help="Warmup duration in seconds (default: 5.0)")
    parser.add_argument("--duration", type=float, default=60.0, help="Duration of the benchmark in seconds. -1 for infinite.")
    parser.add_argument("--benchmark", type=str, default="benchmark_report.json", help="Benchmark report filename")
    parser.add_argument("--no_monitor_imu", action="store_true", help="Disable IMU monitoring (default: ON)")
    args = parser.parse_args()
    monitor_imu = not args.no_monitor_imu

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

    os.environ["ZED_SDK_BENCHMARK_MODE"] = args.benchmark

    # Discover cameras
    all_devices = []
    num_stereo = args.num_stereo
    num_mono = args.num_mono

    if args.svo_input is None:
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
            all_devices.append({"serial": stereo_devs[s].serial_number, "is_mono": False})
        for m in range(num_mono):
            all_devices.append({"serial": mono_devs[m].serial_number, "is_mono": True})

        n_cams = num_stereo + num_mono

        if n_cams == 0:
            print("No ZED cameras detected (or requested 0).")
            return

        print(f"Detected {avail_stereo} stereo + {avail_mono} mono camera(s). "
              f"Using {num_stereo} stereo + {num_mono} mono ({n_cams} total).")
    else:
        all_devices.append({"serial": 0, "is_mono": False})
        n_cams = 1

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
            if args.svo_input is not None:
                init_one.set_from_svo_file(args.svo_input)
                init_one.svo_real_time_mode = False
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
            print(f"[Cam {i}] Opened CameraOne ({handle.mono.get_camera_information().camera_model})")

            # Recording (mono)
            if compression_mode_recording is not None:
                rec_params = sl.RecordingParameters()
                rec_params.compression_mode = compression_mode_recording
                filename = f"benchmark_output_{dev['serial']}.svo"
                rec_params.video_filename = filename
                err = handle.mono.enable_recording(rec_params)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable recording: {err}")
                else:
                    print(f"[Cam {i}] Recording enabled to {filename}")

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
            if args.svo_input is not None:
                init_params.set_from_svo_file(args.svo_input)
                init_params.svo_real_time_mode = False
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
            print(f"[Cam {i}] Opened Camera ({handle.stereo.get_camera_information().camera_model})")

            # Recording (stereo)
            if compression_mode_recording is not None:
                rec_params = sl.RecordingParameters()
                rec_params.compression_mode = compression_mode_recording
                filename = f"benchmark_output_{dev['serial']}.svo"
                rec_params.video_filename = filename
                err = handle.stereo.enable_recording(rec_params)
                if err != sl.ERROR_CODE.SUCCESS:
                    print(f"[Cam {i}] Failed to enable recording: {err}")
                else:
                    print(f"[Cam {i}] Recording enabled to {filename}")

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

    # Warmup
    if args.warmup > 0:
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

    sl.reset_benchmark_timer()

    # Main benchmark threads
    threads = []
    imu_data_all = [{"count": 0, "per_frame": []} for _ in range(n_cams)]
    start_time = time.time()
    for i in range(n_cams):
        if cameras[i].is_mono:
            t = threading.Thread(target=mono_worker, args=(i, cameras[i].mono, monitor_imu, imu_data_all[i]))
        else:
            t = threading.Thread(target=stereo_worker, args=(i, cameras[i].stereo, depth_mode, enable_pt, od_model, bt_model, monitor_imu, imu_data_all[i]))
        threads.append(t)
        t.start()

    try:
        while is_running:
            for i in range(n_cams):
                tag = "mono" if cameras[i].is_mono else "stereo"
                print(f"[Cam {i} ({tag})] FPS: {cameras[i].get_current_fps():.2f}", end="")
                if i < n_cams - 1:
                    print(" | ", end="")
            print("\r", end="")
            sys.stdout.flush()

            time.sleep(1)
            elapsed = time.time() - start_time
            if args.duration > 0 and elapsed > args.duration:
                print("\nBenchmark finished (duration reached).")
                is_running = False
                break
            if not any(t.is_alive() for t in threads):
                break
    except KeyboardInterrupt:
        is_running = False

    print("\nStopping...")
    for t in threads:
        t.join()

    for i, cam in enumerate(cameras):
        if cam.is_mono:
            if compression_mode_recording is not None:
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
            if compression_mode_recording is not None:
                cam.stereo.disable_recording()
            if compression_mode_streaming is not None:
                cam.stereo.disable_streaming()
            cam.stereo.close()

    print("Benchmark Finished.")

    # Append IMU data to SDK benchmark JSON
    if monitor_imu:
        total_s = time.time() - start_time
        try:
            with open(args.benchmark, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        for i in range(n_cams):
            if imu_data_all[i]["count"] == 0:
                continue
            sn = str(cameras[i].serial_number)
            rate = imu_data_all[i]["count"] / total_s if total_s > 0 else 0.0
            pf = imu_data_all[i]["per_frame"]
            avg_pf = sum(pf) / len(pf) if pf else 0.0
            data[f"IMU_SN{sn}"] = {
                "imu_sample_count": imu_data_all[i]["count"],
                "imu_rate_hz": round(rate, 2),
                "imu_per_frame": round(avg_pf, 4),
            }
        with open(args.benchmark, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[IMU] Data appended to {args.benchmark}")

if __name__ == "__main__":
    main()
