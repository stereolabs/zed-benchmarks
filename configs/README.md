# Benchmark Configuration Files

This folder contains YAML configuration files that define benchmark suites for the ZED SDK.

## Structure

```
configs/
├── zedx/                           # ZED X (stereo) camera benchmarks
│   ├── 1zedx/                      # Single camera
│   │   ├── capture.yaml
│   │   ├── depth.yaml
│   │   ├── recording.yaml
│   │   ├── streaming.yaml
│   │   ├── object_detection.yaml
│   │   └── body_tracking.yaml
│   ├── 4zedx_mp/                   # 4 cameras, multi-process
│   │   └── ...
│   └── 4zedx_mt/                   # 4 cameras, multi-thread
│       └── ...
│
├── zedxonegs/                      # ZED X One GS (mono) camera benchmarks
│   ├── 1zedxonegs/                 # Single camera
│   ├── 4zedxonegs_mp/              # 4 cameras, multi-process
│   ├── 4zedxonegs_mt/              # 4 cameras, multi-thread
│   ├── 8zedxonegs_mp/              # 8 cameras, multi-process
│   └── 8zedxonegs_mt/              # 8 cameras, multi-thread
│
└── hybrid/                         # Mixed ZED X + ZED X One GS benchmarks
    ├── 4zedx4zedxonegs_mp/        # 4+4 cameras, multi-process
    └── 4zedx4zedxonegs_mt/        # 4+4 cameras, multi-thread
```

## Configuration Format

### Basic Structure

```yaml
name: "Benchmark Suite Name"
description: "Description of what this suite tests"
camera: ZEDX  # or ZEDXOneGS, ZED2i

defaults:
  warmup: 5          # Warmup duration in seconds
  duration: 30       # Benchmark duration in seconds
  resolution: HD1200 # Default resolution
  camera_fps: 60     # Default FPS
  num_stereo: 1      # Number of stereo cameras to use
  num_mono: 0        # Number of mono cameras to use

benchmarks:
  - id: unique_benchmark_id
    name: "Human-readable name"
    # Override any default parameter here
    resolution: HD1080
    camera_fps: 60
    depth_mode: NEURAL_LIGHT
```

### Available Parameters

| Parameter | Type | Description | Example Values |
|-----------|------|-------------|----------------|
| `id` | string | Unique identifier for the benchmark | `capture_hd1080_60` |
| `name` | string | Human-readable name | `"HD1080 @ 60 FPS"` |
| `resolution` | string | Camera resolution | `SVGA`, `HD1080`, `HD1200`, `HD2K` |
| `camera_fps` | int | Target frame rate | `15`, `30`, `60`, `120` |
| `depth_mode` | string | Depth estimation mode (stereo only) | `NEURAL_LIGHT`, `NEURAL`, `NEURAL_PLUS` |
| `od_model` | string | Object detection model (stereo only) | `MULTI_CLASS_BOX_FAST`, `MULTI_CLASS_BOX_MEDIUM`, `MULTI_CLASS_BOX_ACCURATE` |
| `bt_model` | string | Body tracking model (stereo only) | `HUMAN_BODY_FAST`, `HUMAN_BODY_MEDIUM`, `HUMAN_BODY_ACCURATE` |
| `compression_mode_recording` | string | Recording compression | `H264`, `H265`, `H265_LOSSLESS`, `LOSSLESS` |
| `compression_mode_streaming` | string | Streaming codec | `H264`, `H265` |
| `enable_positional_tracking` | bool | Enable 6DoF tracking | `true`, `false` |
| `svo_input` | string | Path to SVO file | `svo/zed2i_hd1080_30fps.svo2` |
| `warmup` | float | Warmup duration (seconds) | `5.0` |
| `duration` | float | Benchmark duration (seconds) | `30.0`, `-1` (until SVO ends) |
| `num_stereo` | int | Number of stereo cameras | `0`, `1`, `4` |
| `num_mono` | int | Number of mono cameras | `0`, `1`, `4`, `8` |
| `mode` | string | Execution mode | `multi_process`, `multi_thread` |

> **Note:** `depth_mode`, `od_model`, and `bt_model` only apply to stereo cameras.
> Mono cameras automatically skip these features and run capture only.

### Resolution Compatibility

#### ZED X / ZED X One GS
| Resolution | Available FPS |
|------------|--------------|
| SVGA | 15, 30, 60, 120 |
| HD1080 | 15, 30, 60 |
| HD1200 | 15, 30, 60 |

### Multi-Camera Benchmarks

Use `num_stereo` and `num_mono` to specify how many of each camera type to run:

```yaml
benchmarks:
  # 4 stereo cameras, multi-process (one process per camera)
  - id: 4cam_depth
    name: "4 ZED X - Neural Light Depth"
    num_stereo: 4
    num_mono: 0
    mode: multi_process
    depth_mode: NEURAL_LIGHT

  # Hybrid: 4 stereo + 4 mono, multi-thread (single process, one thread per camera)
  - id: hybrid_capture
    name: "4 ZED X + 4 ZED X One GS - Capture"
    num_stereo: 4
    num_mono: 4
    mode: multi_thread
```

### Including Other Configs

Config files can use `include` to combine multiple config files:

```yaml
name: "ZED X Full Suite"
description: "Complete benchmark suite for ZED X on ZED Link Quad"
camera: ZEDX

include:
  - configs/zedx/1zedx/capture.yaml
  - configs/zedx/1zedx/depth.yaml
  - configs/zedx/4zedx_mp/capture.yaml
  # ... etc
```

## Usage Examples

```bash
# Run all benchmarks in a config
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --lang python

# Run a specific benchmark by ID
python scripts/run_benchmarks.py --config configs/zedx/1zedx/depth.yaml --id 1zedx_depth_neural_hd1200_60

# List all benchmarks in a config
python scripts/run_benchmarks.py --config configs/zedx/1zedx/capture.yaml --list

# Dry run (show commands without executing)
python scripts/run_benchmarks.py --config configs/zedx/1zedx/capture.yaml --dry-run
```

## Creating Custom Configs

1. Copy an existing config file as a template
2. Modify the benchmarks list
3. Run with `--list` to verify
4. Run with `--dry-run` to preview commands
5. Execute the benchmarks

```bash
# Example: Create a custom config for your use case
cp configs/zedx/1zedx/depth.yaml configs/zedx/1zedx/my_custom.yaml
# Edit my_custom.yaml
python scripts/run_benchmarks.py --config configs/zedx/1zedx/my_custom.yaml --list
```
