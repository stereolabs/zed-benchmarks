# ZED SDK Python Benchmarks

Python benchmark implementations for measuring ZED SDK performance.

## Prerequisites

- ZED SDK >= 5.5.0
- Python >= 3.8
- pyzed (ZED Python API: installed with the ZED SDK)

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
| `--svo_input <file>` | Input SVO file (omit for live camera) | - |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--depth_mode <mode>` | Depth mode (NEURAL_LIGHT, NEURAL, NEURAL_PLUS) | None |
| `--od_model <model>` | Object detection model | - |
| `--od_custom_onnx_file <path>` | Custom ONNX file (requires `--od_model CUSTOM_YOLOLIKE_BOX_OBJECTS`) | - |
| `--od_custom_onnx_input_shape <WxH>` | Input resolution for the custom ONNX model | 512x512 |
| `--bt_model <model>` | Body tracking model | - |
| `--compression_mode_recording <mode>` | Enable recording (H264, H265, H265_LOSSLESS) | - |
| `--encoding_preset <preset>` | Encoding preset: DEFAULT, ULTRAFAST, FAST, MEDIUM, SLOW | DEFAULT |
| `--encryption_key <key>` | Encryption key/passphrase for SVO recording / decryption | - |
| `--output_svo <file>` | Output SVO filename | benchmark_recording.svo |
| `--compression_mode_streaming <mode>` | Enable streaming (H264, H265) | - |
| `--streaming_port <port>` | Streaming port | 30000 |
| `--serial_number <sn>` | Camera serial number | auto-detect |
| `--enable_positional_tracking` | Enable 6DoF tracking | OFF |
| `--camera_one` | Use CameraOne (mono) API | OFF |
| `--no_monitor_imu` | Disable IMU monitoring | IMU ON |
| `--warmup <seconds>` | Warmup duration | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--glass_to_glass_latency` | Measure glass-to-glass latency via an on-screen pattern (see below) | OFF |
| `--sync_dir <dir>` | Barrier-sync directory for multi-process coordination (set automatically by `run_multi_process.sh`) | - |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

### benchmark_multi_cam.py

| Option | Description | Default |
|--------|-------------|---------|
| `--num_stereo <n>` | Number of stereo cameras (−1 = all detected) | −1 |
| `--num_mono <n>` | Number of mono cameras (−1 = all detected) | −1 |
| `--svo_input <files>` | Input SVO file(s), comma-separated, one per camera (omit for live camera). Mono/stereo type is auto-detected from the recorded filename token | - |
| `--resolution <res>` | Camera resolution | HD1080 |
| `--camera_fps <fps>` | Target frame rate | 30 |
| `--depth_mode <mode>` | Depth mode (stereo only) | None |
| `--od_model <model>` | Object detection model (stereo only) | - |
| `--od_custom_onnx_file <path>` | Custom ONNX file (stereo only) | - |
| `--od_custom_onnx_input_shape <WxH>` | Input resolution for the custom ONNX model | 512x512 |
| `--bt_model <model>` | Body tracking model (stereo only) | - |
| `--compression_mode_recording <mode>` | Enable recording (one `benchmark_output_<stereo\|mono>_<sn>.svo2` per camera) | - |
| `--encoding_preset <preset>` | Encoding preset: DEFAULT, ULTRAFAST, FAST, MEDIUM, SLOW | DEFAULT |
| `--encryption_key <key>` | Encryption key/passphrase for SVO recording / decryption | - |
| `--compression_mode_streaming <mode>` | Enable streaming | - |
| `--enable_positional_tracking` | Enable 6DoF tracking (stereo only) | OFF |
| `--no_monitor_imu` | Disable IMU monitoring | IMU ON |
| `--warmup <seconds>` | Warmup duration | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--glass_to_glass_latency` | Measure glass-to-glass latency via an on-screen pattern (see below) | OFF |
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
| `--compression_mode_recording <mode>` | Enable recording | - |
| `--compression_mode_streaming <mode>` | Enable streaming | - |
| `--warmup <seconds>` | Warmup duration | 5.0 |
| `--duration <seconds>` | Benchmark duration | 60.0 |
| `--benchmark <file>` | Output JSON report file | benchmark_report.json |

## Latency

**Glass-to-image** (`GlassToImageLatency_SN…`) is always measured: from the image timestamp to the
end of `read()`, plus the `retrieve_image()` call (`grab()` is not included). The report also carries
`GrabProcessing_SN…` (stereo only), the `grab()` call alone (without `read()`), next to the other
per-call timings (`RetrieveMeasure`, `RetrieveObjects`, …).

On SVO playback the image timestamp is the original capture time, so glass-to-image uses the host
time right before `read()` as its reference instead: it then measures how long the SDK takes to
deliver an SVO frame (`read()` + `retrieve_image()`).

## Glass-to-glass latency

`--glass_to_glass_latency` measures full end-to-end latency (display + optics + sensor + SDK) by
filming an on-screen AprilTag clock: a superset of glass-to-image latency. The full scheme
(timestamp encode/decode, setup for accurate numbers, `G2G_DEBUG`, troubleshooting) is documented
once in **[../docs/glass-to-glass.md](../docs/glass-to-glass.md)**: the C++ and Python
implementations are bit-for-bit identical.

**Deps (optional):** `glfw`, `PyOpenGL`, `opencv-python` (the aruco module ships with it), `numpy`.
If any is missing, the flag warns and continues without G2G. Install with:

```bash
pip install -r requirements.txt
```

```bash
python3 benchmark_single_cam.py --resolution HD1200 --camera_fps 60 --glass_to_glass_latency
```

## Offline latency replay

Glass-to-glass needs the full physical rig (a screen showing the marker clock, filmed by the
camera). To avoid keeping the rig set up, you can **measure once and re-analyze offline**: while
recording, the benchmark automatically embeds a *live-timestamp correspondence* map into the SVO
(each frame's `IMAGE` timestamp → the live "now" sampled at capture). No extra flag is needed: it
is written whenever recording is enabled.

**1. Live capture (records the SVO with the embedded map):**

```bash
python3 benchmark_single_cam.py --resolution HD1200 --camera_fps 60 \
    --glass_to_glass_latency \
    --compression_mode_recording H265_LOSSLESS --output_svo run.svo
```

**2. Offline replay (no rig; reads the SVO as fast as possible):**

```bash
python3 benchmark_single_cam.py --svo_input run.svo \
    --glass_to_glass_latency --benchmark replay_report.json
```

On playback the benchmark loads the embedded map and replays each frame's recorded live "now"
(keyed by its baked `IMAGE` timestamp), so the `GlassToGlassLatency_SN…` section reproduces the live
numbers without a camera or screen. G2G re-runs the decoder on the recorded frames. Use a
**lossless** codec for bit-exact replay (a lossy codec still works: the marker decode is
error-correcting). `GlassToImageLatency_SN…` does not use the map: on
playback it measures SVO processing time (see [Latency](#latency)). If the SVO was
recorded without the map (no recording at capture time), a `--glass_to_glass_latency` replay prints
a clear error. Use `--encryption_key` to record/replay an encrypted SVO.

> ⚠️ **Playback results are close to live, not identical.** Two effects make them diverge:
> - **Compression.** With a lossy codec the replayed frames are not the exact live pixels, so
>   depth / AI / runtime stats shift slightly. Record with `H265_LOSSLESS` to minimize this.
> - **G2G sample count.** Live, the decoder runs on a background worker and only processes the
>   frames compute leaves time for (others are dropped); on playback **every** frame is decoded, so
>   the replay sample set is a superset of the live one and the G2G statistics can differ.

> **CameraOne (mono) note:** the Python `CameraOne` API has no `read()`, so `grab()` does the
> frame fetch: mono's `GlassToImageLatency` runs to the end of `grab()` (plus `retrieve_image()`, and
> on SVO playback from the host time right before `grab()`), and mono reports no `GrabProcessing`.

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

# Replay a recorded SVO instead of a live camera (no camera needed); see "Offline latency replay" above
python3 benchmark_single_cam.py --svo_input run.svo
```

## Output

The benchmarks use a self-contained internal profiler (`Profiler`, in `profiler.py`) and write a JSON performance report (one `Grab_SN<serial>` entry per camera, plus per-operation timings such as `RetrieveImage`, `RetrieveMeasure`, `RetrieveObjects`, `RetrieveBodies`, `GetPosition`). When the matching options are set, the report also includes `IMU_SN<serial>`, `GlassToImageLatency_SN<serial>`, `GrabProcessing_SN<serial>` (stereo only; see [Latency](#latency)) and `GlassToGlassLatency_SN<serial>` sections. The profiler measures CPU wall-clock per section (mean / median / stddev / min / max / p99 / count).

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
