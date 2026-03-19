import argparse
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

def main():
    # Arguments
    parser = argparse.ArgumentParser(description="ZED Multi-Camera Sensors API Benchmark")
    parser.add_argument("--num_cameras", type=int, default=0, help="Number of cameras to use. 0 for all available.")
    parser.add_argument("--camera_fps", type=int, default=30, help="Camera FPS (e.g. 15, 30, 60)")
    parser.add_argument("--resolution", type=str, default="HD1080", help="Resolution (e.g. HD1200)")
    parser.add_argument("--depth_mode", type=str, default=None, help="Depth Mode (e.g. NEURAL_LIGHT). If set, activates depth.")
    parser.add_argument("--compression_mode_recording", type=str, default=None, help="Compression Mode for Recording (e.g. H265). If set, activates recording.")
    parser.add_argument("--compression_mode_streaming", type=str, default=None, help="Compression Mode for Streaming (e.g. H265). If set, activates streaming.")
    parser.add_argument("--warmup", type=float, default=5.0, help="Warmup duration in seconds (default: 5.0)")
    parser.add_argument("--duration", type=float, default=60.0, help="Duration of the benchmark in seconds. -1 for infinite.")
    parser.add_argument("--benchmark", type=str, default="benchmark_report.json", help="Benchmark report filename (default: benchmark_report.json)")
    args = parser.parse_args()

    # Parse arguments
    resolution = sl.RESOLUTION.from_string(args.resolution.upper())
    if resolution is None or resolution == sl.RESOLUTION.LAST:
        print(f"Invalid resolution: {args.resolution}. Using HD1080.")
        resolution = sl.RESOLUTION.HD1080
    depth_mode = sl.DEPTH_MODE.NONE if args.depth_mode is None else sl.DEPTH_MODE.from_string(args.depth_mode.upper())
    if depth_mode is None or depth_mode == sl.DEPTH_MODE.LAST:
        print(f"Invalid depth mode: {args.depth_mode}. Using NONE.")
        depth_mode = sl.DEPTH_MODE.NONE

    compression_mode_recording = None if args.compression_mode_recording is None else sl.SVO_COMPRESSION_MODE.from_string(args.compression_mode_recording.upper())
    if compression_mode_recording is not None and compression_mode_recording == sl.SVO_COMPRESSION_MODE.LAST:
        print(f"Invalid recording compression mode: {args.compression_mode_recording}. Disabling recording.")
        compression_mode_recording = None

    compression_mode_streaming = None if args.compression_mode_streaming is None else sl.STREAMING_CODEC.from_string(args.compression_mode_streaming.upper())
    if compression_mode_streaming is not None and compression_mode_streaming == sl.STREAMING_CODEC.LAST:
        print(f"Invalid streaming compression mode: {args.compression_mode_streaming}. Disabling streaming.")
        compression_mode_streaming = None
    
    # Enable benchmarking mode in the ZED SDK
    os.environ["ZED_SDK_BENCHMARK_MODE"] = args.benchmark

    # Detect cameras
    devices = sl.Camera.get_device_list()
    if len(devices) == 0:
        print("No ZED cameras detected.")
        sys.exit(1)
    
    if args.num_cameras > 0:
        devices = devices[:args.num_cameras]

    print(f"Benchmarking with {len(devices)} cameras.")

    # Init Sensors instance
    sensors = sl.Sensors()
    init_sensors_params = sl.InitSensorsParameters()
    init_sensors_params.verbose = 0

    status = sensors.init(init_sensors_params)
    if status != sl.SENSORS_ERROR_CODE.SUCCESS:
        print(f"Failed to initialize Sensors: {status}")
        sys.exit(1)

    # Add cameras
    camera_identifiers = []
    for i, dev in enumerate(devices):
        init_params = sl.InitParameters()
        init_params.set_from_serial_number(dev.serial_number)
        init_params.camera_resolution = resolution
        init_params.camera_fps = args.camera_fps
        init_params.depth_mode = depth_mode

        cam_id = sl.SensorDeviceIdentifier()
        err = sensors.add(init_params, cam_id)
        if err != sl.SENSORS_ERROR_CODE.SUCCESS:
            print(f"Failed to add camera {dev.serial_number}: {err}")
        else:
            print(f"Added camera {dev.serial_number} (ID: {cam_id.get_id()})")
            camera_identifiers.append(cam_id)

    if not camera_identifiers:
        print("No cameras could be added.")
        sensors.close()
        sys.exit(1)

    # Enable Recording
    if compression_mode_recording is not None:
        video_filenames = {}
        for cam_id in camera_identifiers:
            filename = f"camera_{cam_id.get_serial_number()}.svo"
            video_filenames[cam_id] = filename
        rec_params = sl.RecordingSensorsParameters(
            video_filenames=video_filenames,
            compression_mode=compression_mode_recording
        )
        err = sensors.enable_recording(rec_params)
        if err != sl.SENSORS_ERROR_CODE.SUCCESS:
            print(f"Failed to enable recording: {err}")
        else:
            print(f"Recording enabled for all cameras.")

    # Enable Streaming
    if compression_mode_streaming is not None:
        stream_params = sl.StreamingSensorsParameters()
        stream_params.codec = compression_mode_streaming
        err = sensors.enable_streaming(stream_params)
        if err != sl.SENSORS_ERROR_CODE.SUCCESS:
            print(f"Failed to enable streaming: {err}")
        else:
            print(f"Streaming enabled ({compression_mode_streaming})")

    # Runtime Data Containers per camera
    camera_mats = {}
    camera_depths = {}
    for cam_id in camera_identifiers:
        camera_depths[cam_id] = sl.Mat()

    # Warmup
    if args.warmup > 0:
        print(f"\nStarting warmup loop for {args.warmup} seconds...")
        warmup_start_time = time.time()
        while is_running:
            if (time.time() - warmup_start_time) > args.warmup:
                print("Warmup finished.")
                break
            sensors.read()
            sensors.process()

    # Reset Benchmark Timer to avoid counting warmup
    sl.reset_benchmark_timer()

    # Stats
    frames_count = 0
    start_time = time.time()
    print(f"\nStarting benchmark loop for {args.duration} seconds. Press Ctrl+C to stop.")

    while is_running:
        if args.duration > 0 and (time.time() - start_time) > args.duration:
            print("\nBenchmark finished (duration reached).")
            break

        if sensors.read() <= sl.SENSORS_ERROR_CODE.SUCCESS:
            if sensors.process() <= sl.SENSORS_ERROR_CODE.SUCCESS:
                # 1. Retrieve Images (batched for all cameras)
                _, camera_mats = sensors.retrieve_image_batched(sl.VIEW.LEFT, sl.MEM.CPU, None, camera_identifiers)

                # 2. Retrieve Depth per camera (if enabled)
                if depth_mode != sl.DEPTH_MODE.NONE:
                    for cam_id in camera_identifiers:
                        sensors.retrieve_measure(camera_depths[cam_id], sl.MEASURE.XYZ, sl.MEM.CPU, sl.Resolution(-1, -1), cam_id)

                frames_count += 1
                if frames_count % args.camera_fps == 0:
                    elapsed = time.time() - start_time
                    fps = frames_count / elapsed
                    sys.stdout.write(f"\rAvg FPS: {fps:.2f} | Total Frames: {frames_count}")
                    sys.stdout.flush()
        else:
            time.sleep(0.001)

    elapsed = time.time() - start_time
    if elapsed > 0 and frames_count > 0:
        print(f"\nFinal Avg FPS: {frames_count / elapsed:.2f}")
    else:
        print("\nNo frames captured.")

    # Close
    sensors.close()

if __name__ == "__main__":
    main()
