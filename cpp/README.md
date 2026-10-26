# ZED SDK C++ Benchmarks

C++ benchmark implementations for measuring ZED SDK performance.

## Prerequisites

- ZED SDK >= 5.5.0
- CUDA 12.x
- CMake >= 3.10
- C++17 compatible compiler
- *(optional, for glass-to-glass latency)* GLFW + OpenGL + OpenCV (with the `objdetect` module)

## Building

```bash
cd cpp
mkdir -p build && cd build
cmake ..
make -j$(nproc)
```

This creates four executables in `build/`:
- `benchmark_single_cam`: single camera benchmark
- `benchmark_multi_cam`: multi-camera (multi-threaded, single process) benchmark
- `list_cameras`: list connected ZED cameras by serial number
- `reboot_cameras`: reboot connected cameras (USB MCU reset + GMSL `nvargus-daemon` restart) before a run

Glass-to-glass support is optional: if GLFW/OpenGL/OpenCV are not found at configure time, the
benchmarks still build with ZED + CUDA and `--glass_to_glass_latency` prints a notice at runtime
(see [Glass-to-glass latency](#glass-to-glass-latency)).

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
| `--svo_input <file>` | Input SVO file (omit for live camera). With `--glass_to_glass_latency`, replays embedded live timestamps (see below) | - |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--depth_mode <mode>` | Depth mode (NONE, NEURAL_LIGHT, NEURAL, NEURAL_PLUS) | NONE |
| `--od_model <model>` | Object detection model | - |
| `--od_custom_onnx_file <path>` | Custom ONNX file (requires `--od_model CUSTOM_YOLOLIKE_BOX_OBJECTS`) | - |
| `--od_custom_onnx_input_shape <WxH>` | Input resolution for the custom ONNX model | 512x512 |
| `--bt_model <model>` | Body tracking model | - |
| `--enable_positional_tracking` | Enable 6DoF tracking | OFF |
| `--compression_mode_recording <mode>` | Enable recording (H264, H265, H265_LOSSLESS, …) | - |
| `--encoding_preset <preset>` | Encoding preset: DEFAULT, ULTRAFAST, FAST, MEDIUM, SLOW | DEFAULT |
| `--encryption_key <key>` | Encrypt the recorded SVO (also used to decrypt `--svo_input`) | - |
| `--output_svo <file>` | Output SVO filename | benchmark_recording.svo |
| `--compression_mode_streaming <mode>` | Enable streaming (H264, H265) | - |
| `--streaming_port <port>` | Streaming port | 30000 |
| `--serial_number <sn>` | Camera serial number | auto-detect |
| `--camera_one` | Use CameraOne (mono) API; stereo-only features (depth, OD, BT, tracking) are disabled | OFF |
| `--no_monitor_imu` | Disable IMU monitoring | IMU ON |
| `--warmup <seconds>` | Warmup duration (not measured) | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--glass_to_glass_latency` | Measure glass-to-glass latency via an on-screen pattern (see below) | OFF |
| `--sync-dir <dir>` | Barrier-sync directory for multi-process coordination (set automatically by `run_multi_process.sh`) | - |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

### benchmark_multi_cam

| Option | Description | Default |
|--------|-------------|---------|
| `--num_stereo <n>` | Number of stereo cameras (omit or −1 = all detected) | all |
| `--num_mono <n>` | Number of mono cameras (omit or −1 = all detected) | all |
| `--svo_input <files>` | Input SVO file(s), comma-separated, one per camera (omit for live camera). Mono/stereo type is auto-detected from the recorded filename token | - |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--depth_mode <mode>` | Depth mode (stereo only) | NONE |
| `--od_model <model>` | Object detection model (stereo only) | - |
| `--od_custom_onnx_file <path>` | Custom ONNX file (stereo only) | - |
| `--od_custom_onnx_input_shape <WxH>` | Input resolution for the custom ONNX model | 512x512 |
| `--bt_model <model>` | Body tracking model (stereo only) | - |
| `--enable_positional_tracking` | Enable 6DoF tracking (stereo only) | OFF |
| `--compression_mode_recording <mode>` | Enable recording (one `benchmark_output_<stereo\|mono>_<sn>.svo2` per camera) | - |
| `--encoding_preset <preset>` | Encoding preset: DEFAULT, ULTRAFAST, FAST, MEDIUM, SLOW | DEFAULT |
| `--encryption_key <key>` | Encrypt recorded SVOs (also decrypts `--svo_input`) | - |
| `--compression_mode_streaming <mode>` | Enable streaming (ports auto-assigned from 30000) | - |
| `--no_monitor_imu` | Disable IMU monitoring | IMU ON |
| `--warmup <seconds>` | Warmup duration (not measured) | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--glass_to_glass_latency` | Measure glass-to-glass latency via an on-screen pattern (see below) | OFF |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

Runs all cameras in a **single process** with one thread per camera. Stereo and mono cameras can be
mixed; stereo-only features are skipped on mono cameras. Enabling OD or BT implicitly enables
positional tracking.

## Latency

**Glass-to-image** (`GlassToImageLatency_SN…`) is always measured: from the image timestamp to the
end of `read()`, plus the `retrieveImage()` call (`grab()` is not included). The report also carries
`GrabProcessing_SN…`, the `grab()` call alone (without `read()`), next to the other per-call timings
(`RetrieveMeasure`, `RetrieveObjects`, …). 

On SVO playback the image timestamp is the original capture time, so glass-to-image uses the host
time right before `read()` as its reference instead: it then measures how long the SDK takes to
deliver an SVO frame (`read()` + `retrieveImage()`).

## Glass-to-glass latency

`--glass_to_glass_latency` measures full end-to-end latency (display + optics + sensor + SDK) by
filming an on-screen AprilTag clock: a superset of glass-to-image latency. The full scheme
(timestamp encode/decode, setup for accurate numbers, `G2G_DEBUG`, troubleshooting) is documented
once in **[../docs/glass-to-glass.md](../docs/glass-to-glass.md)**: the C++ and Python
implementations are bit-for-bit identical.

Decode runs on a separate worker thread per camera, so it never inflates the grab loop; frames are
submitted into a single-slot mailbox and dropped (counted) if the worker is still busy.

**Build deps (optional):** GLFW + OpenGL + OpenCV (with the `objdetect`/aruco module, included in
`libopencv-dev`). If absent at configure time, G2G is compiled out and the flag prints a notice (the
rest of the benchmark is unaffected). On Ubuntu/Jetson:

```bash
sudo apt install libglfw3-dev libgl-dev libopencv-dev
```

```bash
./build/benchmark_single_cam --resolution HD1200 --camera_fps 60 --glass_to_glass_latency
```

## Offline latency replay

Glass-to-glass needs the full physical rig (a screen showing the AprilTag clock, filmed by the
camera). To avoid keeping the rig set up, you can **measure once and re-analyze offline**: record the
run to an SVO, then re-derive the exact same glass-to-glass stats later by replaying the SVO, no camera,
screen or rig required.

This works because recording embeds, **directly inside the SVO**, a correspondence map from each
frame's baked `IMAGE` timestamp to the live "now" sampled at capture time (SVOData key
`live_timestamp_correspondance`, ingested via `ingestDataIntoSVO`). At playback the SDK clock can
only report the playback wall-clock, so the live "now" is read back from this embedded map instead,
making the result independent of replay speed. No separate sidecar file is involved. For G2G, the
SVO is filmed facing the screen, so the AprilTag clock is present in the frames and the decoder can
recover the displayed timestamp offline.

**1. Live capture (records the SVO with embedded live timestamps):**

```bash
./build/benchmark_single_cam --resolution HD1200 --camera_fps 60 \
    --glass_to_glass_latency \
    --compression_mode_recording H265_LOSSLESS --output_svo run.svo
```

The correspondence map is embedded automatically whenever recording is enabled. Use a **lossless**
codec for bit-exact G2G replay: a lossy codec still works (the AprilTag decode is error-correcting)
but a few borderline frames may decode differently.

**2. Offline replay (no rig; reads the SVO as fast as possible):**

```bash
./build/benchmark_single_cam --svo_input run.svo \
    --glass_to_glass_latency --benchmark replay_report.json
```

Each grabbed SVO frame is matched to the embedded map by its `IMAGE` timestamp; unmatched frames
get no G2G sample. **G2G** re-runs the decoder on the SVO frames (deterministic: it decodes every
frame, where the live async decoder may have dropped some, so the replay sample set can be a superset
of the live one). The `GlassToGlassLatency_SN…` section reproduces the live report within float
tolerance. `GlassToImageLatency_SN…` does not use the map: on playback they
measure SVO processing time (see [Latency](#latency)). If the SVO has no
`live_timestamp_correspondance` channel (i.e. it was not recorded by this tool), a
`--glass_to_glass_latency` replay fails fast with a clear error; without that flag any SVO can be
replayed.

> ⚠️ **Playback results are close to live, not identical.** Two effects make them diverge:
> - **Compression.** With a lossy codec the replayed frames are not the exact live pixels, so
>   depth / AI / runtime stats shift slightly. Record with `H265_LOSSLESS` to minimize this.
> - **G2G sample count.** Live, the decoder runs on a background worker thread and only processes
>   the frames compute leaves time for (others are dropped); on playback **every** frame is decoded,
>   so the replay sample set is a superset of the live one and the G2G statistics can differ.

## Multi-Process Benchmarks

For multi-process benchmarks (one process per camera), use `run_multi_process.sh`. It compiles the
code, detects connected cameras, launches one `benchmark_single_cam` per camera (each pinned to a
specific serial), and coordinates them through a start barrier (`--sync-dir`):

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

# Run a suite and generate a report
python ../scripts/run_benchmarks.py --config ../configs/zedx/1zedx/depth.yaml --lang cpp \
    --output-dir ../benchmark_output/my_run -o ../results/jetson/my_run.md
```

## Output

The benchmarks use a self-contained internal profiler (`bench::Profiler`, header-only, in
`include/profiler.hpp`) and write a JSON performance report (one `Grab_SN<serial>` entry per camera,
plus per-operation timings such as `RetrieveImage`, `RetrieveMeasure`, `RetrieveObjects`,
`RetrieveBodies`, `GetPosition`). When the matching options are set, the report also includes
`IMU_SN<serial>`, `GlassToImageLatency_SN<serial>`, `GrabProcessing_SN<serial>` (see
[Latency](#latency)) and `GlassToGlassLatency_SN<serial>` sections. The
profiler measures CPU wall-clock per section (mean / median / stddev / min / max / p99 / count).

## Files

| File | Description |
|------|-------------|
| `src/benchmark_single_cam.cpp` | Single camera benchmark implementation |
| `src/benchmark_multi_cam.cpp` | Multi-threaded multi-camera benchmark |
| `src/list_cameras.cpp` | List connected ZED cameras (fallback for `run_multi_process.sh`) |
| `src/reboot_cameras.cpp` | Reboot connected cameras before a run |
| `src/glass_to_glass.cpp` | Glass-to-glass display + decoder (compiled only when G2G deps are present) |
| `include/glass_to_glass.hpp` | Glass-to-glass public interface |
| `include/getopt_compat.h` | `getopt_long` compatibility shim |
| `run_multi_process.sh` | Multi-process runner script |
| `run.sh` | Wrapper for the unified runner (`scripts/run_benchmarks.py`) |
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
# Camera not detected: check connection and ZED SDK installation
python ../scripts/list_zed_cameras.py
# or, without pyzed:
./build/list_cameras

# Cameras in a bad state: reboot them before retrying
./build/reboot_cameras

# Permission denied
sudo usermod -a -G video $USER
# Log out and back in
```
