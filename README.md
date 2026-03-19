# 🚀 ZED SDK Benchmarks

Official performance benchmarks for the [ZED SDK](https://www.stereolabs.com/developers).

[![ZED SDK](https://img.shields.io/badge/ZED%20SDK-5.2-blue)](https://www.stereolabs.com/developers)
[![Platform](https://img.shields.io/badge/Platform-Linux-green)]()
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## 📊 Performance Highlights

Coming soon.

> ⚠️ **First run note**: AI models (Neural depth, Object Detection, Body Tracking) are optimized for your GPU on first use. This one-time optimization takes several minutes and will cause higher CPU/GPU load. Run the benchmark once to trigger optimization, then re-run for accurate measurements.

---

## ⚡ Quick Start

### Prerequisites

- **ZED SDK** >= 5.2 ([Download](https://www.stereolabs.com/developers/release))
- **CUDA** 12.x
- **Python** >= 3.8 (for Python benchmarks)
- **CMake** >= 3.10 (for C++ benchmarks)

### With a ZED Camera

```bash
# Clone the repo
git clone https://github.com/stereolabs/zed-benchmarks.git
cd zed-benchmarks

# Install Python dependencies (for the benchmark runner)
pip install -r requirements.txt

# Run ZED X capture benchmarks (C++)
python scripts/run_benchmarks.py --config configs/zedx/1zedx/capture.yaml --lang cpp

# Run ZED X depth benchmarks (Python)
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --lang python

# Run all ZED X depth benchmarks (C++)
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --lang cpp \
    --output-dir benchmark_output/my_run -o results/my_run.md
```

### Without a Camera (SVO Demo)

Coming soon.

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
| **ZED X** | SVGA@120, HD1080@60/30, HD1200@60/30 | `configs/zedx/` |
| **ZED X One GS** | SVGA@120, HD1080@60/30, HD1200@60/30 | `configs/zedxonegs/` |
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

---

## 📈 Understanding Results

Each benchmark outputs a JSON file with three categories of data:

| Category | Key Fields | Description |
|----------|-----------|-------------|
| **Grab Frequency** | `grab_call_frequency_hz`, `grab_call_median_period_ms` | Real application throughput — how many frames per second your pipeline processes. This is the **FPS** number to use. |
| **Per-Event Execution Stats** | `Grab_SN*`, `RetrieveImage`, … → `cpu_stats` / `gpu_stats` | How long each SDK call takes to execute. The grab execution time is **not** the frame rate — most of it is spent waiting for the next camera frame. |
| **Hardware Load** | `mean_cpu_load`, `mean_gpu_load`, `mean_*_mem_load` | CPU/GPU utilisation and memory consumption sampled during the run. |

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
