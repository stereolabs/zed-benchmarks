# ZED SDK C++ Benchmarks

C++ benchmark implementations for measuring ZED SDK performance.

## Prerequisites

- ZED SDK >= 5.2
- CUDA 12.x
- CMake >= 3.10
- C++17 compatible compiler

## Building

```bash
cd cpp
mkdir -p build && cd build
cmake ..
make -j$(nproc)
```

This creates three executables in `build/`:
- `benchmark_single_cam` — single camera benchmarks
- `benchmark_multi_cam` — multi-camera (multi-threaded) benchmarks
- `list_cameras` — list connected ZED cameras by serial number

## Quick Start

```bash
# Basic capture benchmark
./build/benchmark_single_cam --resolution HD1200 --camera_fps 60

# Depth benchmark
./build/benchmark_single_cam --resolution HD1200 --camera_fps 60 --depth_mode NEURAL_LIGHT

# Object detection
./build/benchmark_single_cam --resolution HD1200 --camera_fps 60 --depth_mode NEURAL_LIGHT --od_model MULTI_CLASS_BOX_FAST
```

## Command Line Options

### benchmark_single_cam

| Option | Description | Default |
|--------|-------------|---------|
| `--svo_input <file>` | Input SVO file (omit for live camera) | — |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--depth_mode <mode>` | Depth mode (NONE, NEURAL_LIGHT, NEURAL, NEURAL_PLUS) | NONE |
| `--od_model <model>` | Object detection model | — |
| `--bt_model <model>` | Body tracking model | — |
| `--compression_mode_recording <mode>` | Enable recording (H264, H265, H265_LOSSLESS) | — |
| `--output_svo <file>` | Output SVO filename | benchmark_recording.svo |
| `--compression_mode_streaming <mode>` | Enable streaming (H264, H265) | — |
| `--streaming_port <port>` | Streaming port | 30000 |
| `--serial_number <sn>` | Camera serial number | auto-detect |
| `--enable_positional_tracking` | Enable 6DoF tracking | OFF |
| `--camera_one` | Use CameraOne (mono) API | OFF |
| `--warmup <seconds>` | Warmup duration | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

### benchmark_multi_cam

| Option | Description | Default |
|--------|-------------|---------|
| `--num_stereo <n>` | Number of stereo cameras (−1 = all detected) | −1 |
| `--num_mono <n>` | Number of mono cameras (−1 = all detected) | −1 |
| `--svo_input <file>` | Input SVO file (omit for live camera) | — |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--depth_mode <mode>` | Depth mode (stereo only) | NONE |
| `--od_model <model>` | Object detection model (stereo only) | — |
| `--bt_model <model>` | Body tracking model (stereo only) | — |
| `--compression_mode_recording <mode>` | Enable recording | — |
| `--compression_mode_streaming <mode>` | Enable streaming | — |
| `--enable_positional_tracking` | Enable 6DoF tracking (stereo only) | OFF |
| `--warmup <seconds>` | Warmup duration | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

Runs all cameras in a **single process** with one thread per camera.

## Multi-Process Benchmarks

For multi-process benchmarks (one process per camera), use `run_multi_process.sh`:

```bash
# 4 stereo cameras, capture
./run_multi_process.sh --num_stereo 4 --resolution HD1200 --camera_fps 60

# 4 stereo cameras with depth
./run_multi_process.sh --num_stereo 4 --resolution HD1200 --camera_fps 60 --depth_mode NEURAL_LIGHT

# Hybrid: 4 stereo + 4 mono
./run_multi_process.sh --num_stereo 4 --num_mono 4 --resolution HD1200 --camera_fps 60
```

See `./run_multi_process.sh --help` for all options.

## Using with Config Runner

The recommended way to run benchmarks is through the unified runner:

```bash
# Run all depth benchmarks
python ../scripts/run_benchmarks.py --config ../configs/zedx/1zedx/depth.yaml --lang cpp

# Run a specific benchmark by ID
python ../scripts/run_benchmarks.py --config ../configs/zedx/1zedx/capture.yaml --id 1zedx_capture_hd1200_60 --lang cpp

# Run the full ZED X suite and generate a report
python ../scripts/run_benchmarks.py --config ../configs/zedx/full_suite_zedlinkquad_gmsl.yaml --lang cpp \
    --output-dir ../benchmark_output/my_run -o ../results/jetson/my_run.md
```

## Output

The benchmarks set `ZED_SDK_BENCHMARK_MODE` automatically. The SDK prints a performance report to `stdout` and writes a JSON file:

```
Performance Report (sorted by CPU time, some elements are run in parallel):
EVENT NAME        CPU MEAN (ms)  CPU MED (ms)  CPU STD (ms)   Count
-------------------------------------------------------------------
Open                   3562.037      3562.037         0.000       1
Grab_SN41758409          12.759        12.533         2.553    2101
RetrieveImage             4.427         4.284         0.691    1800

Mean grab frequency: 59.9 Hz (median period: 16.7ms)

Hardware Load:
RESOURCE             MEAN              MEDIAN            STDDEV            COUNT
---------------------------------------------------------------------------------
CPU Load             90.0 %            88.9 %            65.2 %            3562
GPU Load             35.6 %            36.0 %            16.9 %            3562
Unified Memory       188.8 MB (1%)     201.2 MB (1%)     39.7 MB (0%)      3562
```

## Files

| File | Description |
|------|-------------|
| `benchmark_single_cam.cpp` | Single camera benchmark implementation |
| `benchmark_multi_cam.cpp` | Multi-threaded multi-camera benchmark |
| `list_cameras.cpp` | List connected ZED cameras |
| `run_multi_process.sh` | Multi-process runner script |
| `run.sh` | Wrapper for the unified runner |
| `CMakeLists.txt` | CMake build configuration |

## Troubleshooting

### Build Errors

```bash
# Make sure ZED SDK is installed
export ZED_SDK_ROOT=/usr/local/zed

# Clean rebuild
rm -rf build && mkdir build && cd build && cmake .. && make -j$(nproc)
```

### Runtime Errors

```bash
# Camera not detected — check connection and ZED SDK installation
python ../scripts/list_zed_cameras.py

# Permission denied
sudo usermod -a -G video $USER
# Log out and back in
```
