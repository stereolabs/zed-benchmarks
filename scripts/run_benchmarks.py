#!/usr/bin/env python3
"""
ZED SDK Benchmark Runner

Unified script to run benchmarks from YAML configuration files.
Supports both Python and C++ benchmark implementations.

Usage:
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --lang python
    python scripts/run_benchmarks.py --config configs/zedx/capture.yaml --lang cpp
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --id depth_neural_hd1080_30
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --id depth_neural_hd1080_30 depth_ultra_hd1080_30
    python scripts/run_benchmarks.py --config configs/zed2i/full_suite.yaml --list
    python scripts/run_benchmarks.py --config configs/zed2i/capture.yaml --dry-run
"""

import argparse
import json
import os
import sys
import shutil
import subprocess
import time
import yaml
import datetime
import glob as globmod
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
            if not os.path.exists(full_path):
                print(f"Warning: included config not found, skipping: {include_path}")
                continue
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


def reset_terminal():
    """Reset terminal to sane state (fixes corrupted onlcr after sudo timeout)."""
    try:
        subprocess.run(['stty', 'sane'], stdin=open('/dev/tty'),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except Exception:
        pass


def build_cpp(repo_root: str, targets: List[str]) -> bool:
    """Build (or rebuild) C++ benchmark targets if sources are newer than binaries."""
    build_dir = os.path.join(repo_root, 'cpp', 'build')
    src_dir = os.path.join(repo_root, 'cpp')
    sources = globmod.glob(os.path.join(src_dir, '*.cpp')) + \
              globmod.glob(os.path.join(src_dir, '*.h')) + \
              [os.path.join(src_dir, 'CMakeLists.txt')]

    newest_src = max((os.path.getmtime(f) for f in sources if os.path.isfile(f)), default=0)

    stale = []
    for t in targets:
        exe = os.path.join(build_dir, t)
        if not os.path.isfile(exe) or os.path.getmtime(exe) < newest_src:
            stale.append(t)

    if not stale:
        return True

    print(f"Building C++ targets: {', '.join(stale)} ...")
    os.makedirs(build_dir, exist_ok=True)
    try:
        subprocess.run(['cmake', '..'], cwd=build_dir, check=True)
        subprocess.run(['make', '-j' + str(os.cpu_count() or 4)] + stale,
                       cwd=build_dir, check=True)
        print("Build OK")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")
        return False


def merge_with_defaults(benchmark: Dict, defaults: Dict) -> Dict:
    """Merge benchmark config with defaults."""
    merged = defaults.copy()
    merged.update(benchmark)
    return merged


def _resolve_mode(benchmark: Dict) -> str:
    """Resolve the effective run mode.

    Rules:
    - 1 camera (or unspecified counts) → always 'single'.
    - Multiple cameras → 'multi_thread' by default, or whatever was set.
    """
    num_stereo = benchmark.get('num_stereo', 0)
    num_mono = benchmark.get('num_mono', 0)
    num_cameras = num_stereo + num_mono if (num_stereo or num_mono) else 1
    if num_cameras <= 1:
        return 'single'
    return benchmark.get('mode') or 'multi_thread'


def validate_benchmarks(benchmarks: List[Dict]) -> None:
    """Warn or error on config combinations that would otherwise silently fall back."""
    valid_multi = ('multi_thread', 'multi_process')
    for b in benchmarks:
        bid = b.get('id', 'unnamed')
        for key in ('num_stereo', 'num_mono'):
            val = b.get(key, 0)
            # bool is a subclass of int in Python; reject it explicitly.
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                print(f"Error: benchmark '{bid}' has {key}={val!r}, "
                      f"must be a non-negative integer.")
                sys.exit(1)
        num_stereo = b.get('num_stereo', 0)
        num_mono = b.get('num_mono', 0)
        num_cameras = num_stereo + num_mono if (num_stereo or num_mono) else 1
        configured = b.get('mode')
        if num_cameras <= 1:
            if configured and configured != 'single':
                print(f"Warning: benchmark '{bid}' sets mode='{configured}' but has "
                      f"<= 1 camera, running single-cam.")
        elif configured is not None and configured not in valid_multi:
            print(f"Error: benchmark '{bid}' has mode='{configured}' with "
                  f"{num_cameras} cameras: expected one of {valid_multi}.")
            sys.exit(1)


def build_command(benchmark: Dict, lang: str, repo_root: str, output_dir: str) -> List[str]:
    """Build the command to run a benchmark."""
    benchmark_id = benchmark.get('id', 'unnamed')
    json_output = os.path.join(output_dir, f"{benchmark_id}.json")
    mode = _resolve_mode(benchmark)
    num_stereo = benchmark.get('num_stereo', 0)
    num_mono = benchmark.get('num_mono', 0)
    camera_one = (num_mono > 0 and num_stereo == 0)

    if lang == 'python':
        if mode == 'multi_process':
            script = os.path.join(repo_root, 'python', 'run_multi_process.sh')
            args = [script, '--num_stereo', str(num_stereo), '--num_mono', str(num_mono)]
        elif mode == 'multi_thread':
            script = os.path.join(repo_root, 'python', 'benchmark_multi_cam.py')
            args = ['python3', script, '--num_stereo', str(num_stereo), '--num_mono', str(num_mono)]
        else:  # single
            script = os.path.join(repo_root, 'python', 'benchmark_single_cam.py')
            args = ['python3', script]
    else:  # cpp
        build_dir = os.path.join(repo_root, 'cpp', 'build')
        if mode == 'multi_process':
            script = os.path.join(repo_root, 'cpp', 'run_multi_process.sh')
            args = [script, '--num_stereo', str(num_stereo), '--num_mono', str(num_mono)]
        elif mode == 'multi_thread':
            script = os.path.join(build_dir, 'benchmark_multi_cam')
            args = [script, '--num_stereo', str(num_stereo), '--num_mono', str(num_mono)]
        else:  # single
            script = os.path.join(build_dir, 'benchmark_single_cam')
            args = [script]

    # Add common parameters
    param_mapping = {
        'resolution': '--resolution',
        'camera_fps': '--camera_fps',
        'depth_mode': '--depth_mode',
        'depth_precision': '--depth_precision',
        'od_model': '--od_model',
        'od_custom_onnx_file': '--od_custom_onnx_file',
        'od_custom_onnx_input_shape': '--od_custom_onnx_input_shape',
        'bt_model': '--bt_model',
        'compression_mode_recording': '--compression_mode_recording',
        'encoding_preset': '--encoding_preset',
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
    if benchmark.get('glass_to_glass_latency'):
        args.append('--glass_to_glass_latency')
    # --camera_one is only needed for single-cam executable calls.
    # Multi-process shell scripts and multi-thread executables derive camera
    # type from --num_stereo / --num_mono.
    is_multi_process = (mode == 'multi_process')
    is_multi_thread = (mode == 'multi_thread')
    if camera_one and not is_multi_process and not is_multi_thread:
        args.append('--camera_one')

    # For multi-process runs, set a per-benchmark log directory so successive
    # runs don't overwrite each other's process logs.
    if is_multi_process:
        log_dir = os.path.join(output_dir, 'logs', benchmark_id)
        args.extend(['--log-dir', log_dir])
        if 'stagger_start' in benchmark and benchmark['stagger_start'] is not None:
            args.extend(['--stagger-start', str(benchmark['stagger_start'])])

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
    if not shutil.which('jetson_clocks'):
        print('Warning: jetson_clocks not found')
        return False
    ret = subprocess.run('echo Y | sudo jetson_clocks --store', shell=True)
    if ret.returncode != 0:
        print('Warning: jetson_clocks --store failed')
        return False
    if os.system('sudo jetson_clocks') != 0:
        print('Warning: jetson_clocks failed')
        return False
    print('Jetson clocks locked to max frequency')
    return True


def jetson_clocks_restore() -> None:
    """Restore clocks to their pre-benchmark state."""
    if not _is_jetson():
        return
    if os.system('sudo jetson_clocks --restore') == 0:
        print('Jetson clocks restored')
    else:
        print('Warning: jetson_clocks --restore failed')


HW_LOAD_ENV_VAR = 'ZED_SDK_ENABLE_HW_LOAD_MEASUREMENT'


def parse_hw_load_reports(hw_dir: str) -> Dict:
    """Parse the per-camera hardware load reports written by the SDK.

    When ``ZED_SDK_ENABLE_HW_LOAD_MEASUREMENT`` points at ``hw_dir``, the SDK
    writes one ``sl_hw_load_<serial>.json`` per camera at ``Camera::close()``,
    containing only the CPU / GPU load (avg / peak / samples) measured while
    the camera was open. Returns {serial: {cpu_load, gpu_load}}.
    """
    cameras = {}
    for path in sorted(globmod.glob(os.path.join(hw_dir, 'sl_hw_load_*.json'))):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not parse hardware load report {path}: {e}")
            continue
        serial = str(data.get('serial_number', os.path.basename(path)))
        cameras[serial] = {
            'cpu_load': data.get('cpu_load'),
            'gpu_load': data.get('gpu_load'),
        }
    return cameras


def format_load_table(load_by_id: Dict) -> str:
    """Render {benchmark_id: {serial: stats}} as an aligned avg/peak text table."""
    id_w = max([len('Benchmark')] + [len(b) for b in load_by_id]) + 2
    sn_w = max([len('Camera SN')] + [len(sn) for cams in load_by_id.values() for sn in cams]) + 2

    def _cell(stats):
        if not isinstance(stats, dict) or stats.get('samples', 0) == 0:
            return '-'
        return f"{stats.get('avg', 0):.1f}/{stats.get('peak', 0):.1f}"

    header = 'Benchmark'.ljust(id_w) + 'Camera SN'.ljust(sn_w) \
        + 'CPU % (avg/peak)'.rjust(20) + 'GPU % (avg/peak)'.rjust(20)
    lines = [header, '-' * len(header)]
    for bid in sorted(load_by_id):
        for sn in sorted(load_by_id[bid]):
            s = load_by_id[bid][sn]
            lines.append(bid.ljust(id_w) + sn.ljust(sn_w)
                         + _cell(s.get('cpu_load')).rjust(20)
                         + _cell(s.get('gpu_load')).rjust(20))
    return '\n'.join(lines)


def run_benchmark(benchmark: Dict, lang: str, repo_root: str, output_dir: str,
                  dry_run: bool = False, monitor_load: bool = False):
    """Run a single benchmark.

    Returns (success, hw_load) where hw_load maps camera serial -> CPU/GPU load
    stats parsed from the per-camera reports the SDK writes at camera close
    (None when load monitoring is off or nothing was reported).
    """
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
        return True, None

    # Trigger the SDK hardware load measurement: the env var points at a
    # per-benchmark directory so reports from successive benchmarks (same
    # camera serial) don't overwrite each other.
    env = None
    hw_dir = None
    if monitor_load:
        hw_dir = os.path.join(output_dir, 'hw_load', benchmark_id)
        os.makedirs(hw_dir, exist_ok=True)
        # Drop reports from a previous run reusing the same output dir, so
        # stale files can't be mistaken for this run's measurements.
        for stale in globmod.glob(os.path.join(hw_dir, 'sl_hw_load_*.json')):
            os.remove(stale)
        env = os.environ.copy()
        env[HW_LOAD_ENV_VAR] = hw_dir

    success = False
    try:
        proc = subprocess.Popen(cmd, cwd=repo_root, env=env)
        proc.wait()
        success = proc.returncode == 0
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Make sure the benchmark executables are built (for C++) or pyzed is installed (for Python)")
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user, waiting for process to exit...")
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("Force killing benchmark process...")
            proc.kill()
            proc.wait()
    finally:
        reset_terminal()

    hw_load = parse_hw_load_reports(hw_dir) if hw_dir else None
    return success, hw_load or None


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

    # Run specific benchmark(s) by ID
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --id depth_neural_hd1080_30
    python scripts/run_benchmarks.py --config configs/zed2i/depth.yaml --id depth_neural_hd1080_30 depth_ultra_hd1080_30

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
    parser.add_argument('--id', '-i', nargs='+',
                        help='Run only the benchmark(s) with these IDs')
    parser.add_argument('--list', action='store_true',
                        help='List all benchmarks in the configuration')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print commands without executing')
    parser.add_argument('--output', '-o',
                        help='Output file for report (markdown or html, auto-detected from extension)')
    parser.add_argument('--format', '-f', choices=['markdown', 'html'],
                        help='Report format (default: auto-detect from --output extension)')
    parser.add_argument('--output-dir',
                        help='Directory for JSON output files (default: ./benchmark_output)')
    parser.add_argument('--no-jetson-clocks', action='store_true',
                        help='Do not lock Jetson clocks to max frequency during benchmarks')
    parser.add_argument('--monitor-load', action='store_true',
                        help=f'Enable SDK hardware load measurement ({HW_LOAD_ENV_VAR}): the SDK '
                             'reports per-camera CPU/GPU load at camera close; results are '
                             'aggregated into load_report.json in the output directory')
    parser.add_argument('--glass-to-glass', action='store_true',
                        help='Enable glass-to-glass latency measurement on all selected benchmarks '
                             '(equivalent to glass_to_glass_latency: true in the config); requires '
                             'the physical marker-clock rig (see docs/glass-to-glass.md)')

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
        id_set = set(args.id)
        benchmarks = [b for b in benchmarks if b.get('id') in id_set]
        missing = id_set - {b.get('id') for b in benchmarks}
        if missing:
            print(f"Error: Benchmark ID(s) not found: {', '.join(sorted(missing))}")
            print("Use --list to see available benchmarks")
            sys.exit(1)
    
    # Merge with defaults
    benchmarks = [merge_with_defaults(b, defaults) for b in benchmarks]

    # --glass-to-glass forces G2G on every selected benchmark, overriding the config
    if args.glass_to_glass:
        for b in benchmarks:
            b['glass_to_glass_latency'] = True

    # Surface any mode/camera-count mismatches before we start running.
    validate_benchmarks(benchmarks)

    print(f"\nZED SDK Benchmark Runner")
    print(f"{'='*60}")
    print(f"Configuration: {config.get('name', 'Unknown')}")
    print(f"Language: {args.lang}")
    print(f"Benchmarks to run: {len(benchmarks)}")
    print(f"Output directory: {output_dir}")
    if args.dry_run:
        print("Mode: DRY RUN (no execution)")
    print(f"{'='*60}")
    
    # Build C++ targets if needed (missing or stale)
    if args.lang == 'cpp' and not args.dry_run:
        resolved_modes = [_resolve_mode(b) for b in benchmarks]
        targets = []
        # 'single' and 'multi_process' both rely on benchmark_single_cam
        # (run_multi_process.sh spawns it per camera).
        if any(m in ('single', 'multi_process') for m in resolved_modes):
            targets.append('benchmark_single_cam')
        if any(m == 'multi_thread' for m in resolved_modes):
            targets.append('benchmark_multi_cam')
        if targets and not build_cpp(repo_root, targets):
            sys.exit(1)
    
    # Lock Jetson clocks for consistent results
    clocks_locked = False
    if not args.no_jetson_clocks and not args.dry_run:
        clocks_locked = jetson_clocks_enable()

    # Run benchmarks
    results = []
    load_by_id = {}
    for step, benchmark in enumerate(benchmarks, 1):
        print(f"\n[{step}/{len(benchmarks)}]", end='')
        success, hw_load = run_benchmark(
            benchmark, args.lang, repo_root, output_dir, args.dry_run,
            monitor_load=args.monitor_load)
        results.append({
            'id': benchmark.get('id'),
            'name': benchmark.get('name'),
            'success': success
        })
        if hw_load:
            load_by_id[benchmark.get('id', 'unnamed')] = hw_load

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

    # Aggregate the per-camera CPU/GPU load reported by the SDK at camera close.
    if load_by_id and not args.dry_run:
        print(f"\n{'='*60}")
        print("HARDWARE LOAD (avg/peak %)")
        print(f"{'='*60}")
        print(format_load_table(load_by_id))
        json_path = os.path.join(output_dir, 'load_report.json')
        try:
            with open(json_path, 'w') as f:
                json.dump(load_by_id, f, indent=2)
            print(f"\nLoad report: {json_path}")
        except OSError as e:
            print(f"Error writing load report: {e}")
    elif args.monitor_load and not args.dry_run:
        print(f"\nWarning: --monitor-load was set but no hardware load report was found. "
              f"Make sure the installed ZED SDK supports {HW_LOAD_ENV_VAR}.")

    # Generate report if requested
    if args.output and not args.dry_run:
        fmt = args.format
        if fmt is None:
            fmt = 'html' if args.output.endswith('.html') else 'markdown'
        try:
            if fmt == 'html':
                from generate_html_report import generate_html_report as gen_html
                gen_html(output_dir, args.output, config)
            else:
                from parse_results import generate_markdown_report
                generate_markdown_report(output_dir, args.output, config)
            print(f"Report ({fmt}): {args.output}")
        except ImportError as e:
            print(f"Note: Could not import report generator ({e}), skipping")
        except Exception as e:
            print(f"Error generating report: {e}")
    
    # Restore Jetson clocks
    if clocks_locked:
        jetson_clocks_restore()

    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
