#!/usr/bin/env python3
"""
ZED SDK Benchmark Runner

Unified script to run benchmarks from YAML configuration files.
Supports both Python and C++ benchmark implementations.

Usage:
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --lang python
    python scripts/run_benchmarks.py --config configs/zedx/capture.yaml --lang cpp
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --id depth_neural_hd1080_30
    python scripts/run_benchmarks.py --config configs/zed2i/full_suite.yaml --list
    python scripts/run_benchmarks.py --config configs/zed2i/capture.yaml --dry-run
"""

import argparse
import os
import sys
import subprocess
import time
import yaml
import datetime
from typing import Dict, List


def load_config(config_path: str) -> Dict:
    """Load a YAML configuration file."""
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Handle includes (for full_suite.yaml)
    if 'include' in config:
        all_benchmarks = []
        base_dir = os.path.dirname(config_path)
        root_dir = os.path.dirname(base_dir)  # Go up from configs/zedx to configs
        root_dir = os.path.dirname(root_dir)  # Go up from configs to repo root
        
        for include_path in config['include']:
            # Resolve path relative to repo root
            full_path = os.path.join(root_dir, include_path)
            if os.path.exists(full_path):
                included = load_config(full_path)
                included_defaults = included.get('defaults', {})
                if 'benchmarks' in included:
                    # Merge each included config's defaults into its benchmarks
                    for b in included['benchmarks']:
                        merged = included_defaults.copy()
                        merged.update(b)
                        all_benchmarks.append(merged)
        
        config['benchmarks'] = all_benchmarks
    
    return config


def merge_with_defaults(benchmark: Dict, defaults: Dict) -> Dict:
    """Merge benchmark config with defaults."""
    merged = defaults.copy()
    merged.update(benchmark)
    return merged


def build_command(benchmark: Dict, lang: str, repo_root: str, output_dir: str) -> List[str]:
    """Build the command to run a benchmark."""
    benchmark_id = benchmark.get('id', 'unnamed')
    json_output = os.path.join(output_dir, f"{benchmark_id}.json")
    mode = benchmark.get('mode', 'single')
    num_stereo = benchmark.get('num_stereo', 0)
    num_mono = benchmark.get('num_mono', 0)
    # Derive total camera count and whether this is a mono-only run
    num_cameras = num_stereo + num_mono if (num_stereo or num_mono) else 1
    camera_one = (num_mono > 0 and num_stereo == 0)

    if lang == 'python':
        if num_cameras > 1 and mode == 'multi_process':
            script = os.path.join(repo_root, 'python', 'run_multi_process.sh')
            args = [script, '--num_stereo', str(num_stereo), '--num_mono', str(num_mono)]
        elif num_cameras > 1 and mode == 'multi_thread':
            script = os.path.join(repo_root, 'python', 'benchmark_multi_cam.py')
            args = ['python3', script, '--num_stereo', str(num_stereo), '--num_mono', str(num_mono)]
        else:
            script = os.path.join(repo_root, 'python', 'benchmark_single_cam.py')
            args = ['python3', script]
    else:  # cpp
        build_dir = os.path.join(repo_root, 'cpp', 'build')
        if num_cameras > 1 and mode == 'multi_process':
            script = os.path.join(repo_root, 'cpp', 'run_multi_process.sh')
            args = [script, '--num_stereo', str(num_stereo), '--num_mono', str(num_mono)]
        elif num_cameras > 1 and mode == 'multi_thread':
            script = os.path.join(build_dir, 'benchmark_multi_cam')
            args = [script, '--num_stereo', str(num_stereo), '--num_mono', str(num_mono)]
        else:
            script = os.path.join(build_dir, 'benchmark_single_cam')
            args = [script]

    # Add common parameters
    param_mapping = {
        'resolution': '--resolution',
        'camera_fps': '--camera_fps',
        'depth_mode': '--depth_mode',
        'od_model': '--od_model',
        'od_custom_onnx_file': '--od_custom_onnx_file',
        'od_custom_onnx_input_shape': '--od_custom_onnx_input_shape',
        'bt_model': '--bt_model',
        'compression_mode_recording': '--compression_mode_recording',
        'encryption_key': '--encryption_key',
        'compression_mode_streaming': '--compression_mode_streaming',
        'warmup': '--warmup',
        'duration': '--duration',
        'svo_input': '--svo_input',
    }

    for key, flag in param_mapping.items():
        if key in benchmark and benchmark[key] is not None:
            args.extend([flag, str(benchmark[key])])

    # Boolean flags
    if benchmark.get('enable_positional_tracking'):
        args.append('--enable_positional_tracking')
    # --camera_one is only needed for single-cam executable calls.
    # Multi-process shell scripts and multi-thread executables derive camera
    # type from --num_stereo / --num_mono.
    is_multi_process = (num_cameras > 1 and mode == 'multi_process')
    is_multi_thread = (num_cameras > 1 and mode == 'multi_thread')
    if camera_one and not is_multi_process and not is_multi_thread:
        args.append('--camera_one')

    # For multi-process runs, set a per-benchmark log directory so successive
    # runs don't overwrite each other's process logs.
    if is_multi_process:
        log_dir = os.path.join(output_dir, 'logs', benchmark_id)
        args.extend(['--log-dir', log_dir])
        if 'stagger_start' in benchmark and benchmark['stagger_start'] is not None:
            args.extend(['--stagger-start', str(benchmark['stagger_start'])])

    # Timer mode
    if benchmark.get('timer'):
        args.append('--timer')

    # Output benchmark JSON
    args.extend(['--benchmark', json_output])

    return args


def list_benchmarks(config: Dict) -> None:
    """List all benchmarks in a configuration."""
    print(f"\n{'='*60}")
    print(f"Configuration: {config.get('name', 'Unknown')}")
    print(f"Description: {config.get('description', 'No description')}")
    print(f"Camera: {config.get('camera', 'Not specified')}")
    print(f"{'='*60}\n")
    
    defaults = config.get('defaults', {})
    benchmarks = config.get('benchmarks', [])
    
    print(f"{'ID':<40} {'Name':<40}")
    print(f"{'-'*40} {'-'*40}")
    
    for benchmark in benchmarks:
        merged = merge_with_defaults(benchmark, defaults)
        print(f"{merged.get('id', 'unnamed'):<40} {merged.get('name', 'Unnamed'):<40}")
    
    print(f"\nTotal: {len(benchmarks)} benchmarks")


def reboot_cameras(lang: str, repo_root: str, dry_run: bool = False) -> None:
    """Reboot cameras to reset nvargus-daemon between benchmarks.

    On Jetson platforms the GMSL camera stack (nvargus-daemon) can get
    into a bad state between successive camera open/close cycles.  This
    calls ``reboot_cameras`` (C++) or ``sl.Camera.reboot`` (Python) to
    put it back to a clean state.

    No-op on non-Jetson platforms or when the binary is not built.
    """
    if not os.path.isfile('/etc/nv_tegra_release'):
        return

    if dry_run:
        print("[DRY RUN] Would reboot cameras")
        return

    if lang == 'cpp':
        exe = os.path.join(repo_root, 'cpp', 'build', 'reboot_cameras')
        if not os.path.isfile(exe):
            return
        try:
            result = subprocess.run([exe], timeout=15, capture_output=True, text=True)
            if result.stdout:
                print(result.stdout.strip())
            if result.returncode != 0 and result.stderr:
                print(f"Warning: reboot_cameras: {result.stderr.strip()}")
        except Exception as e:
            print(f"Warning: Could not reboot cameras: {e}")
    else:
        try:
            import pyzed.sl as sl
            sl.Camera.reboot(sl.INPUT_TYPE.GMSL)
        except Exception:
            pass

    time.sleep(5)


def _is_jetson() -> bool:
    return os.path.isfile('/etc/nv_tegra_release')


def jetson_clocks_enable() -> bool:
    """Run ``jetson_clocks`` to lock CPU/GPU/EMC to max frequency.

    Saves the current state so it can be restored later with
    ``jetson_clocks --restore``.
    Returns True if clocks were successfully set.
    """
    if not _is_jetson():
        return False
    try:
        subprocess.run(['sudo', 'jetson_clocks', '--store'], timeout=10,
                       capture_output=True)
        r = subprocess.run(['sudo', 'jetson_clocks'], timeout=10,
                           capture_output=True, text=True)
        if r.returncode == 0:
            print('Jetson clocks locked to max frequency')
            return True
        print(f'Warning: jetson_clocks failed: {r.stderr.strip()}')
    except FileNotFoundError:
        print('Warning: jetson_clocks not found')
    except Exception as e:
        print(f'Warning: jetson_clocks error: {e}')
    return False


def jetson_clocks_restore() -> None:
    """Restore clocks to their pre-benchmark state."""
    if not _is_jetson():
        return
    try:
        r = subprocess.run(['sudo', 'jetson_clocks', '--restore'], timeout=10,
                           capture_output=True, text=True)
        if r.returncode == 0:
            print('Jetson clocks restored')
        else:
            print(f'Warning: jetson_clocks --restore failed: {r.stderr.strip()}')
    except Exception as e:
        print(f'Warning: Could not restore jetson_clocks: {e}')


def run_benchmark(benchmark: Dict, lang: str, repo_root: str, output_dir: str, dry_run: bool = False) -> bool:
    """Run a single benchmark."""
    benchmark_id = benchmark.get('id', 'unnamed')
    benchmark_name = benchmark.get('name', 'Unnamed')

    print(f"\n{'='*60}")
    print(f"Running: {benchmark_name}")
    print(f"ID: {benchmark_id}")
    print(f"{'='*60}")

    # Reset cameras before each benchmark (Jetson: restarts nvargus-daemon)
    reboot_cameras(lang, repo_root, dry_run)

    cmd = build_command(benchmark, lang, repo_root, output_dir)
    cmd_str = ' '.join(cmd)

    print(f"Command: {cmd_str}\n")

    if dry_run:
        print("[DRY RUN] Skipping execution")
        return True

    try:
        result = subprocess.run(cmd, cwd=repo_root)
        return result.returncode == 0
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Make sure the benchmark executables are built (for C++) or pyzed is installed (for Python)")
        return False
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='ZED SDK Benchmark Runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run all depth benchmarks in Python
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --lang python

    # Run all capture benchmarks in C++
    python scripts/run_benchmarks.py --config configs/zedx/capture.yaml --lang cpp

    # Run a specific benchmark by ID
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --id depth_neural_hd1080_30

    # List all benchmarks in a config
    python scripts/run_benchmarks.py --config configs/zed2i/full_suite.yaml --list

    # Preview commands without running (dry run)
    python scripts/run_benchmarks.py --config configs/zed2i/capture.yaml --dry-run

    # Generate markdown report
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --lang python --output results.md
        """
    )
    
    parser.add_argument('--config', '-c', required=True,
                        help='Path to YAML configuration file')
    parser.add_argument('--lang', '-l', choices=['python', 'cpp'], default='python',
                        help='Language implementation to use (default: python)')
    parser.add_argument('--id', '-i',
                        help='Run only the benchmark with this ID')
    parser.add_argument('--list', action='store_true',
                        help='List all benchmarks in the configuration')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print commands without executing')
    parser.add_argument('--output', '-o',
                        help='Output file for markdown report')
    parser.add_argument('--output-dir',
                        help='Directory for JSON output files (default: ./benchmark_output)')
    parser.add_argument('--no-jetson-clocks', action='store_true',
                        help='Do not lock Jetson clocks to max frequency during benchmarks')
    
    args = parser.parse_args()
    
    # Determine repo root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    
    # Resolve config path
    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(repo_root, config_path)
    
    if not os.path.exists(config_path):
        print(f"Error: Configuration file not found: {config_path}")
        sys.exit(1)
    
    # Load configuration
    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"Error loading configuration: {e}")
        sys.exit(1)
    
    # List mode
    if args.list:
        list_benchmarks(config)
        sys.exit(0)
    
    # Prepare output directory
    output_dir = args.output_dir
    if not output_dir:
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        output_dir = os.path.join(repo_root, 'benchmark_output', timestamp)
    if not args.dry_run:
        os.makedirs(output_dir, exist_ok=True)
    
    # Get benchmarks to run
    defaults = config.get('defaults', {})
    benchmarks = config.get('benchmarks', [])
    
    if not benchmarks:
        print("Error: No benchmarks found in configuration")
        sys.exit(1)
    
    # Filter by ID if specified
    if args.id:
        benchmarks = [b for b in benchmarks if b.get('id') == args.id]
        if not benchmarks:
            print(f"Error: Benchmark with ID '{args.id}' not found")
            print("Use --list to see available benchmarks")
            sys.exit(1)
    
    # Merge with defaults
    benchmarks = [merge_with_defaults(b, defaults) for b in benchmarks]
    
    print(f"\nZED SDK Benchmark Runner")
    print(f"{'='*60}")
    print(f"Configuration: {config.get('name', 'Unknown')}")
    print(f"Language: {args.lang}")
    print(f"Benchmarks to run: {len(benchmarks)}")
    print(f"Output directory: {output_dir}")
    if args.dry_run:
        print("Mode: DRY RUN (no execution)")
    print(f"{'='*60}")
    
    # Check C++ build if using cpp
    if args.lang == 'cpp' and not args.dry_run:
        cpp_single = os.path.join(repo_root, 'cpp', 'build', 'benchmark_single_cam')
        cpp_multi = os.path.join(repo_root, 'cpp', 'build', 'benchmark_multi_cam')
        needs_multi = any(b.get('mode') == 'multi_thread' for b in benchmarks)
        missing = []
        if not os.path.exists(cpp_single):
            missing.append('benchmark_single_cam')
        if needs_multi and not os.path.exists(cpp_multi):
            missing.append('benchmark_multi_cam')
        if missing:
            print(f"\nWarning: C++ benchmarks not built: {', '.join(missing)}")
            print("Build with:")
            print("  cd cpp && mkdir -p build && cd build && cmake .. && make -j$(nproc)")
            print()
            response = input("Continue anyway? [y/N] ")
            if response.lower() != 'y':
                sys.exit(1)
    
    # Lock Jetson clocks for consistent results
    clocks_locked = False
    if not args.no_jetson_clocks and not args.dry_run:
        clocks_locked = jetson_clocks_enable()

    # Run benchmarks
    results = []
    for step, benchmark in enumerate(benchmarks, 1):
        print(f"\n[{step}/{len(benchmarks)}]", end='')
        success = run_benchmark(benchmark, args.lang, repo_root, output_dir, args.dry_run)
        results.append({
            'id': benchmark.get('id'),
            'name': benchmark.get('name'),
            'success': success
        })
    
    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    
    successful = sum(1 for r in results if r['success'])
    failed = len(results) - successful
    
    print(f"Total: {len(results)}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")
    
    if failed > 0:
        print("\nFailed benchmarks:")
        for r in results:
            if not r['success']:
                print(f"  - {r['id']}: {r['name']}")
    
    print(f"\nOutput files: {output_dir}")
    
    # Generate markdown report if requested
    if args.output and not args.dry_run:
        try:
            from parse_results import generate_markdown_report
            generate_markdown_report(output_dir, args.output, config)
            print(f"Markdown report: {args.output}")
        except ImportError:
            print("Note: parse_results.py not found, skipping markdown generation")
        except Exception as e:
            print(f"Error generating markdown report: {e}")
    
    # Restore Jetson clocks
    if clocks_locked:
        jetson_clocks_restore()

    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
