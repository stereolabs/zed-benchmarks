# ZED SDK Python Benchmarks

Python benchmark implementations for measuring ZED SDK performance.

## Prerequisites

- ZED SDK >= 5.2
- Python >= 3.8
- pyzed (ZED Python API — installed with the ZED SDK)

## Verify Installation

```bash
python3 -c "import pyzed.sl as sl; print('ZED SDK', sl.Camera().get_sdk_version())"
```

## Quick Start

```bash
# Basic capture benchmark
python3 benchmark_single_cam.py --resolution HD1200 --camera_fps 60

# Depth benchmark
python3 benchmark_single_cam.py --resolution HD1200 --camera_fps 60 --depth_mode NEURAL_LIGHT

# Object detection
python3 benchmark_single_cam.py --resolution HD1200 --camera_fps 60 --depth_mode NEURAL_LIGHT --od_model MULTI_CLASS_BOX_FAST
```

## Command Line Options

### benchmark_single_cam.py

| Option | Description | Default |
|--------|-------------|---------|
| `--svo_input <file>` | Input SVO file (omit for live camera) | — |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--depth_mode <mode>` | Depth mode (NEURAL_LIGHT, NEURAL, NEURAL_PLUS) | None |
| `--od_model <model>` | Object detection model | — |
| `--bt_model <model>` | Body tracking model | — |
| `--compression_mode_recording <mode>` | Enable recording (H264, H265, H265_LOSSLESS) | — |
| `--output_svo <file>` | Output SVO filename | benchmark_output.svo |
| `--compression_mode_streaming <mode>` | Enable streaming (H264, H265) | — |
| `--streaming_port <port>` | Streaming port | 30000 |
| `--serial_number <sn>` | Camera serial number | auto-detect |
| `--enable_positional_tracking` | Enable 6DoF tracking | OFF |
| `--camera_one` | Use CameraOne (mono) API | OFF |
| `--warmup <seconds>` | Warmup duration | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

### benchmark_multi_cam.py

| Option | Description | Default |
|--------|-------------|---------|
| `--num_stereo <n>` | Number of stereo cameras (−1 = all detected) | −1 |
| `--num_mono <n>` | Number of mono cameras (−1 = all detected) | −1 |
| `--svo_input <file>` | Input SVO file (omit for live camera) | — |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--depth_mode <mode>` | Depth mode (stereo only) | None |
| `--od_model <model>` | Object detection model (stereo only) | — |
| `--bt_model <model>` | Body tracking model (stereo only) | — |
| `--compression_mode_recording <mode>` | Enable recording | — |
| `--compression_mode_streaming <mode>` | Enable streaming | — |
| `--enable_positional_tracking` | Enable 6DoF tracking (stereo only) | OFF |
| `--warmup <seconds>` | Warmup duration | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

Runs all cameras in a **single process** with one thread per camera.

### benchmark_sensors.py

Benchmarks the [Sensors](https://www.stereolabs.com/docs/api/python/classpyzed_1_1sl_1_1Sensors.html) API for multi-camera capture.

| Option | Description | Default |
|--------|-------------|---------|
| `--num_cameras <n>` | Number of cameras (0 = all) | 0 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--depth_mode <mode>` | Depth mode | None |
| `--compression_mode_recording <mode>` | Enable recording | — |
| `--compression_mode_streaming <mode>` | Enable streaming | — |
| `--warmup <seconds>` | Warmup duration | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

## Multi-Process Benchmarks

For multi-process benchmarks (one process per camera), use `run_multi_process.sh`:

```bash
# 4 stereo cameras, capture
./run_multi_process.sh --num_stereo 4 --resolution HD1200 --camera_fps 60

# 4 stereo cameras with depth
./run_multi_process.sh --num_stereo 4 --resolution HD1200 --camera_fps 60 --depth_mode NEURAL_LIGHT
```

See `./run_multi_process.sh --help` for all options.

## Using with Config Runner

The recommended way to run benchmarks is through the unified runner:

```bash
# Run all depth benchmarks
python3 ../scripts/run_benchmarks.py --config ../configs/zedx/1zedx/depth.yaml --lang python

# Run a specific benchmark by ID
python3 ../scripts/run_benchmarks.py --config ../configs/zedx/1zedx/capture.yaml --id 1zedx_capture_hd1200_60 --lang python

# Run with SVO (no camera needed)
python3 ../scripts/run_benchmarks.py --config ../configs/svo/zed2i_demo.yaml --lang python
```

## Output

The benchmarks set `ZED_SDK_BENCHMARK_MODE` automatically. The SDK prints a performance report to `stdout` and writes a JSON file:

```
Performance Report (sorted by CPU time):
EVENT NAME        CPU MEAN (ms)  CPU MED (ms)  CPU STD (ms)   Count
-------------------------------------------------------------------
Open                   3562.037      3562.037         0.000       1
Grab_SN41758409          12.759        12.533         2.553    2101
RetrieveImage             4.427         4.284         0.691    1800

Mean grab frequency: 59.9 Hz (median period: 16.7ms)
```

## Files

| File | Description |
|------|-------------|
| `benchmark_single_cam.py` | Single camera benchmark |
| `benchmark_multi_cam.py` | Multi-threaded multi-camera benchmark |
| `benchmark_sensors.py` | Sensors API multi-camera benchmark |
| `run_multi_process.sh` | Multi-process runner script |
| `run.sh` | Wrapper for the unified runner |

## Troubleshooting

### pyzed not found

```bash
# Reinstall from ZED SDK
python3 /usr/local/zed/get_python_api.py
```

### Camera not detected

```bash
python3 ../scripts/list_zed_cameras.py

# Check permissions
sudo usermod -a -G video $USER
# Log out and back in
```
