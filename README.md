# 🚀 ZED SDK Benchmarks

Official performance benchmarks for the [ZED SDK](https://www.stereolabs.com/developers).

[![ZED SDK](https://img.shields.io/badge/ZED%20SDK-5.5-blue)](https://www.stereolabs.com/developers)
[![Platform](https://img.shields.io/badge/Platform-Linux-green)]()
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## 📖 Overview

**ZED-Benchmark** (available since **ZED SDK 5.5.0**) is the official tool for measuring ZED SDK performance on your own hardware. It answers the two questions that matter when building a real-time perception pipeline:

- **How fast does the SDK run?**: per-function CPU/GPU execution time (grab, image/measure retrieval, depth, AI inference, tracking…) and the real frame rate your application can actually sustain.
- **How responsive is the camera?**: the latency between a real-world event and the moment the corresponding data is available to your code.

Everything can be driven from the **C++ API** or the **Python wrapper** using the same options and the same config files, so you can compare the two directly and quantify the binding overhead. It targets two audiences: **developers** sizing a platform or tuning a pipeline, and **CI pipelines** tracking performance regressions across SDK releases.

### What it measures

| Category | Metrics |
|----------|---------|
| **Throughput** | Real application frame rate (`grab` frequency) and grab period: what your app achieves, *not* the theoretical `1000 / grab_time`. |
| **Function runtime** | Per-event wall-clock runtime (mean / median / stddev / max) for each SDK call (`grab`, `retrieveImage`, `retrieveMeasure`, `retrieveObjects`, `retrieveBodies`, `getPosition`…) via the benchmark suite's self-contained `Profiler`. |
| **Camera latency** | **Glass-to-image** (capture → image available in the SDK) and **glass-to-glass** (full end-to-end: display + optics + sensor + SDK), measured with an on-screen marker clock filmed by the camera. |
| **Hardware load** *(opt-in)* | CPU / GPU load (avg + peak) measured **by the SDK itself** while each camera is open. See [Measure Hardware Load](#measure-hardware-load). |

### What it can exercise

Capture, **depth** (Neural Light / Neural / Neural+), **object detection**, **body tracking**, **positional tracking**, **SVO recording** (H264 / H265 / lossless) and network **streaming**: individually or combined into one pipeline.

### How it scales

- **Single camera**, or **multi-camera** in either **multi-threaded** (one process, one thread per camera) or **multi-process** (one process per camera) mode.
- **Mixed stereo + mono** ("hybrid") setups.
- **Cameras:** ZED X and ZED X One GS ship with ready-made configs; other ZED cameras (e.g. ZED 2i) also work through the single-camera path. **Platforms:** x86 desktop with a discrete GPU, and NVIDIA Jetson: Jetson-aware, locking clocks for stable numbers and resetting the GMSL stack between runs.

### No camera? No problem

Runtime benchmarks can replay a recorded **SVO** file, and latency can be **measured once and re-analysed offline** from the SVO, no physical rig required.

---

> ⚠️ **First run note**: AI models (Neural depth, Object Detection, Body Tracking) are optimized for your GPU on first use. This one-time optimization takes several minutes and will cause higher CPU/GPU load. Run the benchmark once to trigger optimization, then re-run for accurate measurements.

---

## ⚡ Quick Start

### Prerequisites

- **ZED SDK** >= 5.5.0 ([Download](https://www.stereolabs.com/developers/release))
- **CUDA** 12.x
- **Python** >= 3.8 (for Python benchmarks)
- **CMake** >= 3.10 (for C++ benchmarks)

### With a ZED Camera

```bash
# Clone the repo
git clone https://github.com/stereolabs/zed-benchmarks.git
cd zed-benchmarks

# Install Python dependencies (config runner + optional glass-to-glass deps)
pip install -r python/requirements.txt

# Run ZED X capture benchmarks (C++)
python scripts/run_benchmarks.py --config configs/zedx/1zedx/capture.yaml --lang cpp

# Run ZED X depth benchmarks (Python)
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --lang python

# Run all ZED X depth benchmarks (C++)
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --lang cpp \
    --output-dir benchmark_output/my_run -o results/my_run.md
```

### Without a Camera (SVO)

Both implementations can replay a recorded **SVO** file instead of a live camera, and latency can be measured once and re-analysed offline. Pass an SVO directly to a benchmark:

```bash
# Replay a recorded SVO (no camera needed)
python python/benchmark_single_cam.py --svo_input run.svo --depth_mode NEURAL_LIGHT
```

See **Offline latency replay** in the [Python](python/README.md#offline-latency-replay) and [C++](cpp/README.md#offline-latency-replay) READMEs for measuring latency from an SVO. *(A ready-made sample SVO + config is not bundled yet.)*

> ⚠️ **Playback ≠ live.** Numbers from an SVO replay are close to, but not bit-identical to, a live run, for two reasons:
> - **Compressed images.** Unless recorded with a lossless codec, the replayed frames differ slightly from the live pixels, which shifts depth / AI / runtime stats. Use `H265_LOSSLESS` to minimize this.
> - **Glass-to-glass sample count.** Live, the G2G decoder runs asynchronously and only processes the frames compute leaves time for (the rest are dropped); on playback **every** frame is decoded, so the replayed sample set is a superset of the live one and the resulting statistics can differ.

---

## 📁 Repository Structure

```
zed-benchmarks/
├── configs/              # Benchmark configurations (YAML)
│   ├── zedx/             # ZED X specific benchmarks
│   ├── zedxonegs/        # ZED X One GS specific benchmarks
│   └── hybrid/           # Mixed ZED X + ZED X One GS benchmarks
├── results/              # Published benchmark results
├── scripts/              # Runner and parsing utilities
├── cpp/                  # C++ benchmark implementations
├── python/               # Python benchmark implementations
└── ros2/                 # ROS2 benchmarks (see ros2/README.md)
```

---

## 🎯 Available Benchmarks

### By Camera Model

| Camera | Supported Resolutions | Config Path |
|--------|----------------------|-------------|
| **ZED X** | SVGA (15/30/60/120), HD1080 & HD1200 (15/30/60) | `configs/zedx/` |
| **ZED X One GS** | SVGA (15/30/60/120), HD1080 & HD1200 (15/30/60) | `configs/zedxonegs/` |
| **Hybrid** | Mixed ZED X + ZED X One GS | `configs/hybrid/` |

### By Feature

| Config File | Description |
|-------------|-------------|
| `capture.yaml` | Raw image capture performance |
| `depth.yaml` | Depth estimation (Neural Light, Neural, Neural Plus) |
| `recording.yaml` | SVO recording (H264, H265, Lossless) |
| `streaming.yaml` | Network streaming performance |
| `object_detection.yaml` | Object detection models |
| `body_tracking.yaml` | Body/skeleton tracking models |


---

## 🔧 Usage

### Run All Benchmarks from a Config

```bash
# Python
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --lang python

# C++
python scripts/run_benchmarks.py --config configs/zedx/1zedx/capture.yaml --lang cpp
```

### Run a Specific Benchmark by ID

```bash
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --id 1zedx_depth_neural_hd1200_60
```

### Generate a Markdown Report

```bash
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --lang cpp \
    --output-dir benchmark_output/my_run \
    -o results/my_system.md
```

### List Available Benchmarks

```bash
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --list
```

### Dry Run (Preview Commands)

```bash
python scripts/run_benchmarks.py --config configs/zedx/1zedx/capture.yaml --dry-run
```

### Measure Glass-to-Glass Latency

Glass-to-image latency (plus `retrieveImage()`) is always measured, and the HTML report stacks it with the `grab()` and `retrieve*()` call times into one latency-per-frame bar; on SVO playback glass-to-image measures SVO processing time instead. Full **glass-to-glass** latency requires the physical marker-clock rig (a screen filmed by the camera: see [docs/glass-to-glass.md](docs/glass-to-glass.md)) and is opt-in: enable it per benchmark in the YAML (`glass_to_glass_latency: true`) or force it on all selected benchmarks from the command line:

```bash
python scripts/run_benchmarks.py --config configs/zedx/1zedx/capture.yaml --lang cpp \
    --glass-to-glass
```

### Measure Hardware Load

Add `--monitor-load` to have the **SDK itself** measure CPU / GPU load. The runner sets the `ZED_SDK_ENABLE_HW_LOAD_MEASUREMENT` environment variable for each benchmark; the SDK samples hardware load while the camera runs and, when the camera is closed, writes a per-camera report (`sl_hw_load_<serial>.json`, CPU / GPU load only) that the runner parses:

```bash
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --lang cpp \
    --monitor-load -o results/my_system.md
```

At the end the runner prints an avg/peak table and writes the aggregated metrics to `load_report.json` in the output directory (per benchmark, per camera serial). When a report is also generated (`-o`), a **Hardware Load** section is folded into the Markdown/HTML output.

The env var can also be used standalone with any SDK application: set it to `1` (reports written to the working directory) or to an output directory path.

#### How the load is measured

A background thread inside the SDK samples continuously (~10 ms tick) while the camera is open; avg and peak are computed over all samples between `open()` and `close()`:

- **CPU load** is **process-level**: the CPU time consumed by the SDK process relative to elapsed time, scaled by core count, same convention as `top`/`htop`, so it can exceed 100% on multi-core systems. Other processes are *not* counted.
- **GPU load** is the **system-wide temporal utilization** reported by the driver: the devfreq `load` node on Jetson, NVML `utilization.gpu` on desktop.

> ⚠️ **GPU load is an upper bound: it can read higher than the real load.** Temporal utilization is the fraction of time during which **at least one** kernel was executing, *not* the fraction of GPU compute capacity actually used. A single small kernel keeping the GPU busy back-to-back reads 100% even if it occupies only a fraction of the SMs. In addition:
> - It is **system-wide**: any other process using the GPU inflates the number.
> - On Jetson, if the GPU clock is downscaled by DVFS (e.g. `jetson_clocks` not active), the busy fraction rises even though the absolute amount of work is unchanged. The runner locks clocks on Jetson, but keep this in mind for standalone use.
>
> Practically: a reported GPU load of 80% does **not** mean only 20% of GPU compute is left, the real headroom for additional GPU work is usually larger than `100 − gpu_load` suggests. CPU load does not have this bias, as it measures actual CPU time consumed.

---

## 🏗️ Building C++ Benchmarks

```bash
cd cpp
mkdir build && cd build
cmake ..
make -j$(nproc)
```

Executables will be created in `cpp/build/`:
- `benchmark_single_cam`
- `benchmark_multi_cam`
- `list_cameras`
- `reboot_cameras`

---

## 📈 Understanding Results

Each benchmark outputs a JSON file with two categories of data:

| Category | Key Fields | Description |
|----------|-----------|-------------|
| **Grab Frequency** | `grab_call_frequency_hz`, `grab_call_median_period_ms` | Real application throughput: how many frames per second your pipeline processes. This is the **FPS** number to use. |
| **Per-Event Execution Stats** | `Grab_SN*`, `RetrieveImage`, … → `cpu_stats` / `gpu_stats` | How long each SDK call takes to execute. The grab execution time is **not** the frame rate: most of it is spent waiting for the next camera frame. |

> **FPS ≠ 1000 / grab_time.** A 60 FPS camera with a 15 ms grab execution time has ~1.7 ms of application overhead per frame. The correct FPS is 60, not 66.7.

> **Resolution note:** `retrieveImage` / `retrieveMeasure` are called with `Resolution(-1, -1)` to retrieve at the SDK's internal working resolution, which avoids an unnecessary resize to the camera resolution and is more efficient. The default `Resolution(0, 0)` would retrieve at the full camera resolution. When depth is enabled, the benchmarks retrieve the **XYZ point cloud** (`MEASURE::XYZ`) rather than the depth map.

See [results/README.md](results/README.md) for detailed interpretation guidelines.

---

## 📜 License

This project is licensed under the MIT License - see [LICENSE](LICENSE) for details.

---

## 🔗 Related Resources

- [ZED SDK Documentation](https://www.stereolabs.com/docs/)
- [ZED SDK Downloads](https://www.stereolabs.com/developers/release)
- [Stereolabs GitHub](https://github.com/stereolabs)
- [Stereolabs Community](https://community.stereolabs.com/)
