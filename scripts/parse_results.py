#!/usr/bin/env python3
"""
ZED SDK Benchmark Results Parser

Parses JSON benchmark output files and generates Markdown reports.

Usage:
    python scripts/parse_results.py --input benchmark_output/ --output results.md
    python scripts/parse_results.py --input benchmark_output/ --format table
"""

import argparse
import os
import sys
import json
import datetime
import re as _re
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple


def load_json_results(directory: str) -> Dict[str, Any]:
    """Load all JSON result files from a directory."""
    results = {}

    for filename in os.listdir(directory):
        if filename.endswith('.json'):
            filepath = os.path.join(directory, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    benchmark_id = filename.replace('.json', '')
                    results[benchmark_id] = data
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not load {filename}: {e}")

    return results


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------

# Maps JSON event keys to metric keys
_EVENT_MAP = {
    'RetrieveImage': 'retrieve_image_ms',
    'CameraOneRetrieveImage': 'retrieve_image_ms',
    'RetrieveMeasure': 'retrieve_measure_ms',
    'RetrieveBodies': 'retrieve_bodies_ms',
    'RetrieveObjects': 'retrieve_objects_ms',
    'GetPosition': 'get_position_ms',
}


def extract_metrics(data: Dict) -> Dict:
    """Extract key metrics from benchmark JSON data.

    The JSON produced by the ZED SDK contains:

    1. **Per-event execution stats** (``Grab_SN*``, ``RetrieveImage``, …)
       Each event has ``cpu_stats`` and ``gpu_stats`` with mean/median/stddev.

    2. **Grab call frequency** (inside ``Grab_SN*``)
       - ``grab_call_frequency_hz``: measured frame rate.
       - ``grab_call_median_period_ms``: time between grabs.

    3. **Hardware load** (top-level scalars)
       - ``mean_cpu_load``: CPU utilisation %.
       - ``mean_gpu_temporal_load`` / ``mean_gpu_throughput_load``: GPU %.
       - ``mean_cpu_mem_load`` / ``mean_gpu_mem_load``: memory in MB.
    """
    metrics = {
        'fps': None,
        'grab_time_ms': None,
        'grab_period_ms': None,
        'retrieve_image_ms': None,
        'retrieve_measure_ms': None,
        'retrieve_bodies_ms': None,
        'retrieve_objects_ms': None,
        'get_position_ms': None,
        'cpu_load': None,
        'gpu_temporal_load': None,
        'gpu_throughput_load': None,
        'memory_mb': None,
    }

    # --- Grab execution time & frequency (per-event stats) ---
    for key, value in data.items():
        if key.startswith('Grab_SN') or key.startswith('Grab'):
            if isinstance(value, dict):
                if 'gpu_stats' in value:
                    metrics['grab_time_ms'] = value['gpu_stats'].get('median')
                elif 'cpu_stats' in value:
                    metrics['grab_time_ms'] = value['cpu_stats'].get('median')
                if 'grab_call_frequency_hz' in value:
                    metrics['fps'] = round(value['grab_call_frequency_hz'], 1)
                if 'grab_call_median_period_ms' in value:
                    metrics['grab_period_ms'] = value['grab_call_median_period_ms']

    # --- Grab call frequency at top level (legacy) ---
    if metrics['fps'] is None and 'grab_call_frequency_hz' in data:
        metrics['fps'] = round(data['grab_call_frequency_hz'], 1)
    if metrics['grab_period_ms'] is None and 'grab_call_median_period_ms' in data:
        metrics['grab_period_ms'] = data['grab_call_median_period_ms']

    # --- Per-event execution stats ---
    for event_key, metric_key in _EVENT_MAP.items():
        if event_key in data and isinstance(data[event_key], dict):
            stats = data[event_key].get('gpu_stats') or data[event_key].get('cpu_stats', {})
            metrics[metric_key] = stats.get('median')

    # --- Hardware loads ---
    if 'mean_cpu_load' in data:
        metrics['cpu_load'] = data['mean_cpu_load']
    if 'mean_gpu_temporal_load' in data:
        metrics['gpu_temporal_load'] = data['mean_gpu_temporal_load']
    if 'mean_gpu_throughput_load' in data:
        metrics['gpu_throughput_load'] = data['mean_gpu_throughput_load']
    if 'mean_cpu_mem_load' in data:
        metrics['memory_mb'] = data['mean_cpu_mem_load']

    # --- Fallbacks for older / alternative JSON formats ---
    if metrics['fps'] is None and 'performance_report' in data:
        report = data['performance_report']
        if 'mean_grab_frequency' in report:
            metrics['fps'] = report['mean_grab_frequency']
        if 'events' in report:
            for event in report['events']:
                if 'Grab' in event.get('name', ''):
                    metrics['grab_time_ms'] = event.get('cpu_mean_ms')
                    break

    if metrics['cpu_load'] is None and 'hardware_load' in data:
        hw = data['hardware_load']
        metrics['cpu_load'] = hw.get('cpu_mean')
        metrics['memory_mb'] = hw.get('memory_mean_mb')

    if metrics['fps'] is None and 'final_fps' in data:
        metrics['fps'] = data['final_fps']

    # Last resort: derive from grab execution time (less accurate).
    if metrics['fps'] is None and metrics['grab_time_ms'] is not None and metrics['grab_time_ms'] > 0:
        metrics['fps'] = round(1000.0 / metrics['grab_time_ms'], 1)

    return metrics


def extract_jetson_clocks(results: Dict[str, Any]) -> Optional[Dict]:
    """Extract jetson_clocks info from the first result that has it."""
    for data in results.values():
        jc = data.get('jetson_clocks')
        if jc and isinstance(jc, dict):
            return jc
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_metric(value: Any, suffix: str = '') -> str:
    """Format a metric value for display."""
    if value is None:
        return '-'
    if isinstance(value, float):
        return f"{value:.1f}{suffix}"
    return f"{value}{suffix}"


# ---------------------------------------------------------------------------
# Column definitions for dynamic hiding
# ---------------------------------------------------------------------------

# (header, metric_key, suffix)
_SINGLE_COLUMNS = [
    ('Benchmark ID',           '_id',                  ''),
    ('FPS',                    'fps',                  ''),
    ('Period (ms)',             'grab_period_ms',       ''),
    ('Grab (ms)',              'grab_time_ms',         ''),
    ('Retrieve Image (ms)',    'retrieve_image_ms',    ''),
    ('Retrieve Measure (ms)',  'retrieve_measure_ms',  ''),
    ('Retrieve Bodies (ms)',   'retrieve_bodies_ms',   ''),
    ('Retrieve Objects (ms)',  'retrieve_objects_ms',  ''),
    ('Get Position (ms)',      'get_position_ms',      ''),
    ('CPU Load (%)',           'cpu_load',             '%'),
    ('GPU Temporal (%)',       'gpu_temporal_load',    '%'),
    ('GPU Throughput (%)',     'gpu_throughput_load',  '%'),
    ('Mem (MB)',               'memory_mb',            ''),
]

_SHARED_EVENT_KEYS = [
    ('Retrieve Image (ms)',    'retrieve_image_ms',    ''),
    ('Retrieve Measure (ms)',  'retrieve_measure_ms',  ''),
    ('Retrieve Bodies (ms)',   'retrieve_bodies_ms',   ''),
    ('Retrieve Objects (ms)',  'retrieve_objects_ms',  ''),
    ('Get Position (ms)',      'get_position_ms',      ''),
]

_SHARED_LOAD_KEYS = [
    ('CPU Load (%)',           'cpu_load',             '%'),
    ('GPU Temporal (%)',       'gpu_temporal_load',    '%'),
    ('GPU Throughput (%)',     'gpu_throughput_load',  '%'),
    ('Mem (MB)',               'memory_mb',            ''),
]

# Columns always shown even if all values are None
_ALWAYS_SHOW = {'_id', 'fps', 'grab_period_ms', 'grab_time_ms', 'cpu_load', 'memory_mb'}


def _visible_columns(columns, all_metrics_list, always_show=None):
    """Return only columns that have at least one non-None value (or are in always_show)."""
    if always_show is None:
        always_show = _ALWAYS_SHOW
    visible = []
    for header, key, suffix in columns:
        if key in always_show:
            visible.append((header, key, suffix))
            continue
        for m in all_metrics_list:
            val = m.get(key) if isinstance(m, dict) else None
            if val is not None:
                visible.append((header, key, suffix))
                break
    return visible


def _get_visible_shared_columns(all_shared_dicts: List[Dict]) -> List[Tuple[str, str, str]]:
    """Determine which shared columns (events + loads) have data."""
    all_cols = _SHARED_EVENT_KEYS + _SHARED_LOAD_KEYS
    always = {'cpu_load', 'memory_mb'}
    visible = []
    for header, key, suffix in all_cols:
        if key in always:
            visible.append((header, key, suffix))
            continue
        for d in all_shared_dicts:
            if d.get(key) is not None:
                visible.append((header, key, suffix))
                break
    return visible


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------

def get_system_info() -> Dict[str, str]:
    """Auto-detect system information."""
    import subprocess
    info = {}

    # GPU / Platform
    try:
        dt_model = Path('/proc/device-tree/model').read_text().strip('\x00').strip()
        if 'Jetson' in dt_model or 'NVIDIA' in dt_model:
            info['GPU'] = dt_model
        else:
            raise FileNotFoundError
    except Exception:
        try:
            out = subprocess.check_output(
                ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                text=True, stderr=subprocess.DEVNULL).strip()
            info['GPU'] = out.split('\n')[0]
        except Exception:
            info['GPU'] = '(unknown)'

    # CPU
    try:
        out = subprocess.check_output(['lscpu'], text=True)
        for line in out.splitlines():
            if 'Model name' in line:
                info['CPU'] = line.split(':', 1)[1].strip()
                break
        if 'CPU' not in info:
            info['CPU'] = '(unknown)'
    except Exception:
        info['CPU'] = '(unknown)'

    # RAM
    try:
        out = subprocess.check_output(['free', '-h'], text=True)
        for line in out.splitlines():
            if line.startswith('Mem:'):
                info['RAM'] = line.split()[1]
                break
    except Exception:
        info['RAM'] = '(unknown)'

    # ZED SDK
    try:
        ver_cmake = Path('/usr/local/zed/zed-config-version.cmake')
        if ver_cmake.exists():
            import re
            content = ver_cmake.read_text()
            m = re.search(r'set\(PACKAGE_VERSION\s+"([^"]+)"\)', content)
            if m:
                info['ZED SDK'] = m.group(1)
        if 'ZED SDK' not in info:
            ver_file = Path('/usr/local/zed/settings/sl_version.txt')
            if ver_file.exists():
                info['ZED SDK'] = ver_file.read_text().strip()
        if 'ZED SDK' not in info:
            info['ZED SDK'] = '(unknown)'
    except Exception:
        info['ZED SDK'] = '(unknown)'

    return info


# ---------------------------------------------------------------------------
# Jetson clocks formatting
# ---------------------------------------------------------------------------

def _format_jetson_clocks(jc: Dict) -> List[str]:
    """Format jetson_clocks info into markdown lines."""
    lines = []
    lines.append("## Jetson Configuration")
    lines.append("")

    nvpmodel = jc.get('nvpmodel', '(unknown)')
    constant = jc.get('constant', None)
    cpu_min = jc.get('cpu_min_freq_khz')
    cpu_max = jc.get('cpu_max_freq_khz')
    gpu_min = jc.get('gpu_min_freq_khz')
    gpu_max = jc.get('gpu_max_freq_khz')

    def _freq_str(min_khz, max_khz):
        if min_khz is None and max_khz is None:
            return '(unknown)'
        if min_khz == max_khz:
            return f"{min_khz / 1e6:.2f} GHz"
        return f"{min_khz / 1e6:.2f} – {max_khz / 1e6:.2f} GHz"

    constant_str = "Yes" if constant else "**No** (clocks varied during benchmark)"

    lines.append("| Setting | Value |")
    lines.append("|---------|-------|")
    lines.append(f"| **NVP Model** | {nvpmodel} |")
    lines.append(f"| **Clocks Constant** | {constant_str} |")
    lines.append(f"| **CPU Frequency** | {_freq_str(cpu_min, cpu_max)} |")
    lines.append(f"| **GPU Frequency** | {_freq_str(gpu_min, gpu_max)} |")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Per-camera / grouping helpers
# ---------------------------------------------------------------------------

def _extract_per_camera_metrics(data: Dict) -> List[Dict]:
    """Extract per-camera metrics from a multi-thread JSON."""
    cameras = []
    for key in sorted(data.keys()):
        if not key.startswith('Grab_SN'):
            continue
        v = data[key]
        if not isinstance(v, dict):
            continue
        sn = key.replace('Grab_', '')
        m: Dict[str, Any] = {
            'serial': sn,
            'fps': None,
            'grab_time_ms': None,
            'grab_period_ms': None,
        }
        if 'grab_call_frequency_hz' in v:
            m['fps'] = round(v['grab_call_frequency_hz'], 1)
        if 'grab_call_median_period_ms' in v:
            m['grab_period_ms'] = v['grab_call_median_period_ms']
        if 'gpu_stats' in v:
            m['grab_time_ms'] = v['gpu_stats'].get('median')
        elif 'cpu_stats' in v:
            m['grab_time_ms'] = v['cpu_stats'].get('median')
        cameras.append(m)
    return cameras


def _extract_shared_metrics(data: Dict) -> Dict:
    """Extract shared (non-per-camera) metrics from a JSON result."""
    shared = {
        'cpu_load': data.get('mean_cpu_load'),
        'gpu_temporal_load': data.get('mean_gpu_temporal_load'),
        'gpu_throughput_load': data.get('mean_gpu_throughput_load'),
        'memory_mb': data.get('mean_cpu_mem_load'),
    }
    for event_key, metric_key in _EVENT_MAP.items():
        if event_key in data and isinstance(data[event_key], dict):
            stats = data[event_key].get('gpu_stats') or data[event_key].get('cpu_stats', {})
            shared[metric_key] = stats.get('median')
        else:
            shared.setdefault(metric_key, None)
    return shared


def _group_results(results: Dict[str, Any]):
    """Separate single-camera, multi-thread, and multi-process results.

    Returns (single, multi_thread, multi_process) where:
      single:        list of (benchmark_id, metrics)
      multi_thread:  list of (benchmark_id, per_camera_list, shared_metrics)
      multi_process: dict  { base_id: [(process_num, metrics), ...] }
    """
    single = []
    multi_thread = []
    multi_process: Dict[str, list] = {}

    for benchmark_id in sorted(results.keys()):
        data = results[benchmark_id]
        m = _re.match(r'^(.+)_process_(\d+)$', benchmark_id)
        if m:
            base_id = m.group(1)
            proc_num = int(m.group(2))
            metrics = extract_metrics(data)
            multi_process.setdefault(base_id, []).append((proc_num, metrics))
        elif sum(1 for k in data if k.startswith('Grab_SN')) >= 2:
            per_cam = _extract_per_camera_metrics(data)
            shared = _extract_shared_metrics(data)
            multi_thread.append((benchmark_id, per_cam, shared))
        else:
            metrics = extract_metrics(data)
            single.append((benchmark_id, metrics))

    for procs in multi_process.values():
        procs.sort(key=lambda x: x[0])

    return single, multi_thread, multi_process


# ---------------------------------------------------------------------------
# HTML table builders (multi-camera)
# ---------------------------------------------------------------------------

def _html_multi_thread_table(mt_results, visible_shared) -> List[str]:
    """Build an HTML table for multi-thread results with dynamic columns."""
    per_cam_hdrs = ["Benchmark ID", "Camera", "FPS", "Period (ms)", "Grab (ms)"]

    lines: List[str] = []
    lines.append("<table>")
    hdr_cells = "".join(f"<th>{h}</th>" for h in per_cam_hdrs)
    hdr_cells += "".join(f"<th>{h}</th>" for h, _, _ in visible_shared)
    lines.append(f"<tr>{hdr_cells}</tr>")

    for bid, cameras, shared in mt_results:
        n = len(cameras) if cameras else 1
        if not cameras:
            empty_per = "<td>-</td>" * 3
            empty_shared = "<td>-</td>" * len(visible_shared)
            lines.append(f'<tr><td>{bid}</td><td>-</td>{empty_per}{empty_shared}</tr>')
            continue
        for i, cam in enumerate(cameras):
            cam_cells = (
                f"<td>{format_metric(cam['fps'])}</td>"
                f"<td>{format_metric(cam['grab_period_ms'])}</td>"
                f"<td>{format_metric(cam['grab_time_ms'])}</td>"
            )
            if i == 0:
                shared_cells = "".join(
                    f'<td rowspan="{n}">{format_metric(shared.get(key), sfx)}</td>'
                    for _, key, sfx in visible_shared
                )
                lines.append(
                    f'<tr><td rowspan="{n}"><b>{bid}</b></td>'
                    f'<td>{cam["serial"]}</td>{cam_cells}{shared_cells}</tr>'
                )
            else:
                lines.append(f'<tr><td>{cam["serial"]}</td>{cam_cells}</tr>')

    lines.append("</table>")
    return lines


def _html_multi_process_table(mp_results: Dict[str, list], visible_shared) -> List[str]:
    """Build an HTML table for multi-process results with dynamic columns."""
    per_cam_hdrs = ["Benchmark ID", "Process", "FPS", "Period (ms)", "Grab (ms)"]

    lines: List[str] = []
    lines.append("<table>")
    hdr_cells = "".join(f"<th>{h}</th>" for h in per_cam_hdrs)
    hdr_cells += "".join(f"<th>{h}</th>" for h, _, _ in visible_shared)
    lines.append(f"<tr>{hdr_cells}</tr>")

    def _avg(values):
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    # CPU load and memory are per-process; show them per-row.
    # GPU load is a global metric reported identically by each process, so
    # it is averaged and merged with rowspan.
    _PER_PROCESS_KEYS = {'cpu_load', 'memory_mb'}

    for base_id, procs in sorted(mp_results.items()):
        n = len(procs)
        # Pre-compute merged (averaged) values for shared columns
        merged = {}
        for _, key, _ in visible_shared:
            if key not in _PER_PROCESS_KEYS:
                merged[key] = _avg([m.get(key) for _, m in procs])

        for i, (proc_num, metrics) in enumerate(procs):
            cam_cells = (
                f"<td>{format_metric(metrics['fps'])}</td>"
                f"<td>{format_metric(metrics['grab_period_ms'])}</td>"
                f"<td>{format_metric(metrics['grab_time_ms'])}</td>"
            )
            # Per-process columns get a cell on every row;
            # shared (averaged) columns get rowspan on the first row only.
            shared_cells = ""
            for _, key, sfx in visible_shared:
                if key in _PER_PROCESS_KEYS:
                    shared_cells += f'<td>{format_metric(metrics.get(key), sfx)}</td>'
                elif i == 0:
                    shared_cells += (
                        f'<td rowspan="{n}">{format_metric(merged.get(key), sfx)}</td>'
                    )
            if i == 0:
                lines.append(
                    f'<tr><td rowspan="{n}"><b>{base_id}</b></td>'
                    f'<td>{proc_num}</td>{cam_cells}{shared_cells}</tr>'
                )
            else:
                lines.append(f'<tr><td>{proc_num}</td>{cam_cells}{shared_cells}</tr>')

    lines.append("</table>")
    return lines


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_markdown_report(input_dir: str, output_file: str, config: Optional[Dict] = None) -> None:
    """Generate a Markdown report from benchmark results."""
    results = load_json_results(input_dir)

    if not results:
        print(f"No results found in {input_dir}")
        return

    sys_info = get_system_info()

    lines = []

    # Header
    lines.append("# Benchmark Results")
    lines.append("")
    lines.append(f"**Generated:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if config:
        lines.append(f"**Configuration:** {config.get('name', 'Unknown')}")
        lines.append(f"**Camera:** {config.get('camera', 'Unknown')}")
    lines.append("")

    # System info
    lines.append("## System Information")
    lines.append("")
    lines.append("| Component | Value |")
    lines.append("|-----------|-------|")
    lines.append(f"| **GPU** | {sys_info.get('GPU', '(unknown)')} |")
    lines.append(f"| **CPU** | {sys_info.get('CPU', '(unknown)')} |")
    lines.append(f"| **RAM** | {sys_info.get('RAM', '(unknown)')} |")
    lines.append(f"| **ZED SDK** | {sys_info.get('ZED SDK', '(unknown)')} |")
    lines.append("")

    # Jetson clocks
    jc = extract_jetson_clocks(results)
    if jc:
        lines.extend(_format_jetson_clocks(jc))

    single, multi_thread, multi_process = _group_results(results)

    # --- Single-camera results (dynamic columns) ---
    if single:
        all_single_metrics = [m for _, m in single]
        visible = _visible_columns(_SINGLE_COLUMNS, all_single_metrics)

        lines.append("## Single Camera Results")
        lines.append("")
        hdrs = [h for h, _, _ in visible]
        lines.append("| " + " | ".join(hdrs) + " |")
        lines.append("|" + "|".join("---" for _ in visible) + "|")
        for benchmark_id, metrics in single:
            cells = []
            for _, key, sfx in visible:
                if key == '_id':
                    cells.append(benchmark_id)
                else:
                    cells.append(format_metric(metrics.get(key), sfx))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # --- Multi-camera multi-thread results ---
    if multi_thread:
        mt_shared = [sh for _, _, sh in multi_thread]
        visible_shared_mt = _get_visible_shared_columns(mt_shared)
        lines.append("## Multi-Camera Multi-Thread Results")
        lines.append("")
        lines.extend(_html_multi_thread_table(multi_thread, visible_shared_mt))
        lines.append("")

    # --- Multi-camera multi-process results ---
    if multi_process:
        mp_shared = [m for procs in multi_process.values() for _, m in procs]
        visible_shared_mp = _get_visible_shared_columns(mp_shared)
        lines.append("## Multi-Camera Multi-Process Results")
        lines.append("")
        lines.extend(_html_multi_process_table(multi_process, visible_shared_mp))
        lines.append("")

    lines.append("")

    # Raw data
    lines.append("---")
    lines.append("")
    lines.append("<details>")
    lines.append("<summary>Raw Benchmark Data (JSON)</summary>")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(results, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("</details>")

    with open(output_file, 'w') as f:
        f.write('\n'.join(lines))

    print(f"Report written to: {output_file}")


def print_table(input_dir: str) -> None:
    """Print results as a simple table to stdout."""
    results = load_json_results(input_dir)

    if not results:
        print(f"No results found in {input_dir}")
        return

    # Collect all metrics to determine which columns to show
    all_metrics = []
    for data in results.values():
        all_metrics.append(extract_metrics(data))

    col_defs = [
        ('Benchmark ID',  '_id',                  '', 40),
        ('FPS',            'fps',                  '',  8),
        ('Period(ms)',     'grab_period_ms',       '', 12),
        ('Grab(ms)',       'grab_time_ms',         '', 10),
        ('RetrImg(ms)',    'retrieve_image_ms',    '', 12),
        ('RetrMsr(ms)',    'retrieve_measure_ms',  '', 12),
        ('RetrBod(ms)',    'retrieve_bodies_ms',   '', 12),
        ('RetrObj(ms)',    'retrieve_objects_ms',   '', 12),
        ('GetPos(ms)',     'get_position_ms',      '', 12),
        ('CPU%',           'cpu_load',             '%',  8),
        ('GPU-T%',         'gpu_temporal_load',    '%',  8),
        ('GPU-P%',         'gpu_throughput_load',  '%',  8),
        ('Mem(MB)',        'memory_mb',            '', 10),
    ]

    always = {'_id', 'fps', 'grab_period_ms', 'grab_time_ms', 'cpu_load', 'memory_mb'}
    visible = []
    for header, key, sfx, width in col_defs:
        if key in always:
            visible.append((header, key, sfx, width))
            continue
        for m in all_metrics:
            if m.get(key) is not None:
                visible.append((header, key, sfx, width))
                break

    # Print header
    hdr = "".join(
        f"{h:<{w}}" if i == 0 else f"{h:>{w}}"
        for i, (h, _, _, w) in enumerate(visible)
    )
    sep = "".join(
        f"{'-'*w} " if i == 0 else f"{'-'*w}"
        for i, (_, _, _, w) in enumerate(visible)
    )
    print(f"\n{hdr}")
    print(sep)

    for benchmark_id, data in sorted(results.items()):
        metrics = extract_metrics(data)
        cells = []
        for i, (_, key, sfx, w) in enumerate(visible):
            if key == '_id':
                cells.append(f"{benchmark_id:<{w}}")
            else:
                val = format_metric(metrics.get(key), sfx)
                cells.append(f"{val:>{w}}")
        print("".join(cells))

    # Jetson clocks summary for terminal
    jc = extract_jetson_clocks(results)
    if jc:
        nvp = jc.get('nvpmodel', '?')
        const = "constant" if jc.get('constant') else "VARIED"
        cpu_mhz = (jc.get('cpu_max_freq_khz', 0)) / 1000
        gpu_mhz = (jc.get('gpu_max_freq_khz', 0)) / 1000
        print(f"\nJetson: {nvp}, clocks {const}, CPU {cpu_mhz:.0f} MHz, GPU {gpu_mhz:.0f} MHz")


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_results(dir1: str, dir2: str, output_file: Optional[str] = None) -> None:
    """Compare results from two benchmark runs."""
    results1 = load_json_results(dir1)
    results2 = load_json_results(dir2)

    all_ids = set(results1.keys()) | set(results2.keys())

    rows = []
    for benchmark_id in sorted(all_ids):
        metrics1 = extract_metrics(results1.get(benchmark_id, {}))
        metrics2 = extract_metrics(results2.get(benchmark_id, {}))

        fps1 = metrics1['fps']
        fps2 = metrics2['fps']

        fps1_str = format_metric(fps1)
        fps2_str = format_metric(fps2)

        if fps1 is not None and fps2 is not None:
            diff = fps2 - fps1
            diff_pct = (diff / fps1) * 100 if fps1 != 0 else 0
            diff_str = f"{diff:+.1f} ({diff_pct:+.1f}%)"
        else:
            diff_str = "N/A"

        rows.append((benchmark_id, metrics1, metrics2, fps1_str, fps2_str, diff_str))

    print(f"\n{'Benchmark ID':<35} {'FPS (1)':>10} {'FPS (2)':>10} {'Diff':>10}")
    print(f"{'-'*35} {'-'*10} {'-'*10} {'-'*10}")
    for benchmark_id, _, _, fps1_str, fps2_str, diff_str in rows:
        print(f"{benchmark_id:<35} {fps1_str:>10} {fps2_str:>10} {diff_str:>10}")

    if output_file:
        _write_compare_report(dir1, dir2, results1, results2, rows, output_file)


# Comparison load columns definition
_COMPARE_LOAD_COLS = [
    ('CPU Load',       'cpu_load',             '%'),
    ('GPU Temporal',   'gpu_temporal_load',    '%'),
    ('GPU Throughput', 'gpu_throughput_load',  '%'),
    ('Mem (MB)',       'memory_mb',            ''),
]
_COMPARE_ALWAYS = {'cpu_load', 'memory_mb'}


def _write_compare_report(dir1, dir2, results1, results2, rows, output_file):
    """Write a compact markdown comparison report."""
    sys_info = get_system_info()
    lines = []
    lines.append("# Benchmark Comparison")
    lines.append("")
    lines.append(f"**Generated:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"**Run 1:** `{dir1}`  ")
    lines.append(f"**Run 2:** `{dir2}`  ")
    lines.append("")

    lines.append("## System Information")
    lines.append("")
    lines.append("| Component | Value |")
    lines.append("|-----------|-------|")
    lines.append(f"| **GPU** | {sys_info.get('GPU', '(unknown)')} |")
    lines.append(f"| **CPU** | {sys_info.get('CPU', '(unknown)')} |")
    lines.append(f"| **RAM** | {sys_info.get('RAM', '(unknown)')} |")
    lines.append(f"| **ZED SDK** | {sys_info.get('ZED SDK', '(unknown)')} |")
    lines.append("")

    # Jetson clocks from either run
    jc1 = extract_jetson_clocks(results1)
    jc2 = extract_jetson_clocks(results2)
    jc = jc1 or jc2
    if jc:
        lines.extend(_format_jetson_clocks(jc))

    single1, mt1, mp1 = _group_results(results1)
    single2, mt2, mp2 = _group_results(results2)

    def _diff_str(v1, v2, suffix=''):
        if v1 is not None and v2 is not None and v1 != 0:
            d = v2 - v1
            pct = (d / v1) * 100
            return f"{d:+.1f}{suffix} ({pct:+.1f}%)"
        return '-'

    def _avg(values):
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    # Determine visible load columns across both runs
    all_metrics_both = [m for _, m in single1] + [m for _, m in single2]
    vis_cmp = []
    for h, k, s in _COMPARE_LOAD_COLS:
        if k in _COMPARE_ALWAYS:
            vis_cmp.append((h, k, s))
            continue
        for m in all_metrics_both:
            if m.get(k) is not None:
                vis_cmp.append((h, k, s))
                break

    # --- Single camera comparison ---
    single1_dict = {bid: m for bid, m in single1}
    single2_dict = {bid: m for bid, m in single2}
    all_single = sorted(set(single1_dict) | set(single2_dict))
    if all_single:
        lines.append("## Single Camera Comparison")
        lines.append("")
        hdr = "| Benchmark ID | FPS (1) | FPS (2) | FPS Diff"
        sep = "|--------------|---------|---------|--------"
        for h, _, _ in vis_cmp:
            hdr += f" | {h} (1) | {h} (2) | {h} Diff"
            sep += "|--------|--------|--------"
        hdr += " |"
        sep += "|"
        lines.append(hdr)
        lines.append(sep)
        for bid in all_single:
            m1 = single1_dict.get(bid, {})
            m2 = single2_dict.get(bid, {})
            row = f"| {bid}"
            row += f" | {format_metric(m1.get('fps'))} | {format_metric(m2.get('fps'))} | {_diff_str(m1.get('fps'), m2.get('fps'))}"
            for _, k, s in vis_cmp:
                row += f" | {format_metric(m1.get(k), s)} | {format_metric(m2.get(k), s)} | {_diff_str(m1.get(k), m2.get(k), s)}"
            row += " |"
            lines.append(row)
        lines.append("")

    # --- Multi-thread comparison ---
    mt1_dict = {bid: (cams, shared) for bid, cams, shared in mt1}
    mt2_dict = {bid: (cams, shared) for bid, cams, shared in mt2}
    all_mt = sorted(set(mt1_dict) | set(mt2_dict))
    if all_mt:
        mt_shared_both = [sh for _, _, sh in mt1] + [sh for _, _, sh in mt2]
        vis_mt_cmp = []
        for h, k, s in _COMPARE_LOAD_COLS:
            if k in _COMPARE_ALWAYS:
                vis_mt_cmp.append((h, k, s))
                continue
            for d in mt_shared_both:
                if d.get(k) is not None:
                    vis_mt_cmp.append((h, k, s))
                    break

        lines.append("## Multi-Camera Multi-Thread Comparison")
        lines.append("")
        lines.append("<table>")
        hdr_html = '<tr><th>Benchmark ID</th><th>Camera</th><th>FPS (1)</th><th>FPS (2)</th><th>FPS Diff</th>'
        for h, _, _ in vis_mt_cmp:
            hdr_html += f'<th>{h} (1)</th><th>{h} (2)</th><th>{h} Diff</th>'
        hdr_html += '</tr>'
        lines.append(hdr_html)

        for bid in all_mt:
            cams1, sh1 = mt1_dict.get(bid, ([], {}))
            cams2, sh2 = mt2_dict.get(bid, ([], {}))
            cam_map1 = {c['serial']: c for c in cams1}
            cam_map2 = {c['serial']: c for c in cams2}
            all_cams = sorted(set(cam_map1) | set(cam_map2))
            n = len(all_cams) if all_cams else 1
            for i, sn in enumerate(all_cams):
                c1 = cam_map1.get(sn, {})
                c2 = cam_map2.get(sn, {})
                fps_cells = (
                    f'<td>{format_metric(c1.get("fps"))}</td>'
                    f'<td>{format_metric(c2.get("fps"))}</td>'
                    f'<td>{_diff_str(c1.get("fps"), c2.get("fps"))}</td>'
                )
                if i == 0:
                    shared_cells = ""
                    for h, k, s in vis_mt_cmp:
                        shared_cells += (
                            f'<td rowspan="{n}">{format_metric(sh1.get(k), s)}</td>'
                            f'<td rowspan="{n}">{format_metric(sh2.get(k), s)}</td>'
                            f'<td rowspan="{n}">{_diff_str(sh1.get(k), sh2.get(k), s)}</td>'
                        )
                    lines.append(
                        f'<tr><td rowspan="{n}"><b>{bid}</b></td>'
                        f'<td>{sn}</td>{fps_cells}{shared_cells}</tr>'
                    )
                else:
                    lines.append(f'<tr><td>{sn}</td>{fps_cells}</tr>')
        lines.append("</table>")
        lines.append("")

    # --- Multi-process comparison ---
    all_mp = sorted(set(mp1) | set(mp2))
    if all_mp:
        mp_shared_both = [m for procs in mp1.values() for _, m in procs] + \
                         [m for procs in mp2.values() for _, m in procs]
        vis_mp_cmp = []
        for h, k, s in _COMPARE_LOAD_COLS:
            if k in _COMPARE_ALWAYS:
                vis_mp_cmp.append((h, k, s))
                continue
            for d in mp_shared_both:
                if d.get(k) is not None:
                    vis_mp_cmp.append((h, k, s))
                    break

        lines.append("## Multi-Camera Multi-Process Comparison")
        lines.append("")
        lines.append("<table>")
        hdr_html = '<tr><th>Benchmark ID</th><th>Process</th><th>FPS (1)</th><th>FPS (2)</th><th>FPS Diff</th>'
        for h, _, _ in vis_mp_cmp:
            hdr_html += f'<th>{h} (1)</th><th>{h} (2)</th><th>{h} Diff</th>'
        hdr_html += '</tr>'
        lines.append(hdr_html)

        for base_id in all_mp:
            procs1 = mp1.get(base_id, [])
            procs2 = mp2.get(base_id, [])
            p1_dict = {p: m for p, m in procs1}
            p2_dict = {p: m for p, m in procs2}
            all_p = sorted(set(p1_dict) | set(p2_dict))
            n = len(all_p) if all_p else 1

            merged1 = {k: _avg([m.get(k) for _, m in procs1]) for _, k, _ in vis_mp_cmp}
            merged2 = {k: _avg([m.get(k) for _, m in procs2]) for _, k, _ in vis_mp_cmp}

            for i, p in enumerate(all_p):
                m1 = p1_dict.get(p, {})
                m2 = p2_dict.get(p, {})
                fps_cells = (
                    f'<td>{format_metric(m1.get("fps"))}</td>'
                    f'<td>{format_metric(m2.get("fps"))}</td>'
                    f'<td>{_diff_str(m1.get("fps"), m2.get("fps"))}</td>'
                )
                if i == 0:
                    shared_cells = ""
                    for h, k, s in vis_mp_cmp:
                        shared_cells += (
                            f'<td rowspan="{n}">{format_metric(merged1.get(k), s)}</td>'
                            f'<td rowspan="{n}">{format_metric(merged2.get(k), s)}</td>'
                            f'<td rowspan="{n}">{_diff_str(merged1.get(k), merged2.get(k), s)}</td>'
                        )
                    lines.append(
                        f'<tr><td rowspan="{n}"><b>{base_id}</b></td>'
                        f'<td>{p}</td>{fps_cells}{shared_cells}</tr>'
                    )
                else:
                    lines.append(f'<tr><td>{p}</td>{fps_cells}</tr>')
        lines.append("</table>")
        lines.append("")

    with open(output_file, 'w') as f:
        f.write('\n'.join(lines))
    print(f"Comparison report written to: {output_file}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='ZED SDK Benchmark Results Parser',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Generate markdown report
    python scripts/parse_results.py --input benchmark_output/ --output results.md

    # Print table to stdout
    python scripts/parse_results.py --input benchmark_output/ --format table

    # Compare two benchmark runs
    python scripts/parse_results.py --compare run1/ run2/
        """
    )

    parser.add_argument('--input', '-i',
                        help='Input directory containing JSON result files')
    parser.add_argument('--output', '-o',
                        help='Output file for markdown report')
    parser.add_argument('--format', '-f', choices=['markdown', 'table'], default='markdown',
                        help='Output format (default: markdown)')
    parser.add_argument('--compare', nargs=2, metavar=('DIR1', 'DIR2'),
                        help='Compare results from two directories')

    args = parser.parse_args()

    if args.compare:
        compare_results(args.compare[0], args.compare[1], args.output)
        return

    if not args.input:
        parser.error("--input is required unless using --compare")

    if not os.path.isdir(args.input):
        print(f"Error: Directory not found: {args.input}")
        sys.exit(1)

    if args.format == 'table' or not args.output:
        print_table(args.input)

    if args.output:
        generate_markdown_report(args.input, args.output)


if __name__ == '__main__':
    main()
