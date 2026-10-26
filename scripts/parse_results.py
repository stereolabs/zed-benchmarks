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
import html
import re as _re
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple


def _esc(value: Any) -> str:
    """HTML-escape a value for safe interpolation into the raw <table> markup. Benchmark ids come
    from author-controlled YAML, but a stray '<' or '&' would still break table rendering."""
    return html.escape(str(value))


# Hardware load sidecar written into the output dir by run_benchmarks.py
# --monitor-load (aggregated from the per-camera reports the SDK writes at
# camera close). Not a benchmark result: skipped when loading result JSONs.
LOAD_REPORT_FILENAME = 'load_report.json'


def load_json_results(directory: str) -> Dict[str, Any]:
    """Load all JSON result files from a directory."""
    results = {}

    for filename in os.listdir(directory):
        if filename == LOAD_REPORT_FILENAME:
            continue  # hardware load sidecar, handled separately
        if filename.endswith('.json'):
            filepath = os.path.join(directory, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    print(f"Warning: skipping {filename}: top-level JSON is not an object")
                    continue
                benchmark_id = filename.replace('.json', '')
                results[benchmark_id] = data
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not load {filename}: {e}")

    return results


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------

# Per-camera event sections. The benchmark report emits one key per camera as
# "<ReportName>_SN<serial>" (e.g. "RetrieveImage_SN12345"), mirroring "Grab_SN<serial>". The same
# schema is used for single- and multi-camera reports, so events are always parsed per camera.
_PER_CAMERA_EVENT_MAP = {
    'GrabProcessing':  'grab_processing_ms',
    'RetrieveImage':   'retrieve_image_ms',
    'RetrieveMeasure': 'retrieve_measure_ms',
    'RetrieveBodies':  'retrieve_bodies_ms',
    'RetrieveObjects': 'retrieve_objects_ms',
    'GetPosition':     'get_position_ms',
}

# Legacy top-level event keys (no serial), kept so older reports still parse. Older single-camera
# reports stripped the serial suffix and emitted these bare keys at top level.
_EVENT_MAP = {
    '.retrieveImage(...)': 'retrieve_image_ms',
    '.retrieveMeasure(...)': 'retrieve_measure_ms',
    '.retrieveBodies(...)': 'retrieve_bodies_ms',
    '.retrieveObjects(...)': 'retrieve_objects_ms',
    '.getPosition(...)': 'get_position_ms',
    'RetrieveImage': 'retrieve_image_ms',
    'CameraOneRetrieveImage': 'retrieve_image_ms',
    'RetrieveMeasure': 'retrieve_measure_ms',
    'RetrieveBodies': 'retrieve_bodies_ms',
    'RetrieveObjects': 'retrieve_objects_ms',
    'GetPosition': 'get_position_ms',
}


def _extract_event_metrics(data: Dict, sn_token: str) -> Dict:
    """Extract this camera's per-event median timings.

    ``sn_token`` is the serial token shared with ``Grab_<sn_token>`` (e.g. ``SN12345``); each event
    is looked up as ``<ReportName>_<sn_token>``. Only events actually profiled for the camera are
    present (a mono camera, for instance, only has RetrieveImage).
    """
    out: Dict[str, Any] = {}
    for prefix, metric_key in _PER_CAMERA_EVENT_MAP.items():
        ev = data.get(f'{prefix}_{sn_token}')
        if isinstance(ev, dict):
            # `or {}` guards a present-but-null cpu_stats (the dict default only covers a missing key).
            stats = ev.get('gpu_stats') or ev.get('cpu_stats') or {}
            out[metric_key] = stats.get('median')
    return out


def extract_metrics(data: Dict) -> Dict:
    """Extract key metrics from benchmark JSON data.

    The JSON produced by the ZED SDK contains:

    1. **Per-event execution stats** (``Grab_SN*``, ``RetrieveImage``, …)
       Each event has ``cpu_stats`` and ``gpu_stats`` with mean/median/stddev.

    2. **Grab call frequency** (inside ``Grab_SN*``)
       - ``grab_call_frequency_hz``: measured frame rate.
       - ``grab_call_median_period_ms``: time between grabs.
    """
    metrics = {
        'fps': None,
        'grab_time_ms': None,
        'grab_period_ms': None,
        'depth_runtime_ms': None,
        'grab_processing_ms': None,
        'retrieve_image_ms': None,
        'retrieve_measure_ms': None,
        'retrieve_bodies_ms': None,
        'retrieve_objects_ms': None,
        'get_position_ms': None,
        'g2i_latency_ms': None,
        'g2g_latency_ms': None,
    }

    # --- Glass-to-image / glass-to-glass latency (median, per-camera keys) ---
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if key.startswith('GlassToImageLatency_SN'):
            metrics['g2i_latency_ms'] = (value.get('latency_ms') or {}).get('median')
        elif key.startswith('GlassToGlassLatency_SN'):
            metrics['g2g_latency_ms'] = (value.get('latency_ms') or {}).get('median')

    # --- Grab execution time & frequency (per-event stats) ---
    for key, value in data.items():
        # Match the per-camera "Grab_SN<sn>" key and the legacy bare "Grab"; an exact bare-key
        # check avoids accidentally matching an unrelated key that merely starts with "Grab".
        if key.startswith('Grab_SN') or key == 'Grab':
            if isinstance(value, dict):
                if value.get('gpu_stats') is not None:
                    metrics['grab_time_ms'] = value['gpu_stats'].get('median')
                elif value.get('cpu_stats') is not None:
                    metrics['grab_time_ms'] = value['cpu_stats'].get('median')
                if value.get('grab_call_frequency_hz') is not None:
                    metrics['fps'] = round(value['grab_call_frequency_hz'], 1)
                if 'grab_call_median_period_ms' in value:
                    metrics['grab_period_ms'] = value['grab_call_median_period_ms']

    # --- Grab call frequency at top level (legacy) ---
    if metrics['fps'] is None and data.get('grab_call_frequency_hz') is not None:
        metrics['fps'] = round(data['grab_call_frequency_hz'], 1)
    if metrics['grab_period_ms'] is None and 'grab_call_median_period_ms' in data:
        metrics['grab_period_ms'] = data['grab_call_median_period_ms']

    # --- Per-event execution stats (serial-keyed) ---
    # A single-camera report has exactly one Grab_SN<serial>; derive its serial token and pull this
    # camera's per-event timings (RetrieveImage_SN<serial>, …).
    sn_token = next((k[len('Grab_'):] for k in data if k.startswith('Grab_SN')), None)
    if sn_token:
        metrics.update(_extract_event_metrics(data, sn_token))

    # Legacy fallback: older reports emitted bare top-level event keys (no serial). Only fill what
    # the serial-keyed lookup didn't.
    for event_key, metric_key in _EVENT_MAP.items():
        if metrics.get(metric_key) is None and event_key in data and isinstance(data[event_key], dict):
            # `or {}` guards a present-but-null cpu_stats (the dict default only covers a missing key).
            stats = data[event_key].get('gpu_stats') or data[event_key].get('cpu_stats') or {}
            metrics[metric_key] = stats.get('median')

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
# Hardware load sidecar (load_report.json, aggregated by run_benchmarks.py
# --monitor-load from the per-camera CPU/GPU load reports written by the SDK
# at camera close). Schema: {benchmark_id: {serial: {cpu_load, gpu_load}}}
# where each metric is {avg, peak, samples}.
# ---------------------------------------------------------------------------

def load_load_report(input_dir: str) -> Optional[Dict[str, Any]]:
    """Load the hardware load sidecar (``load_report.json``), or None if absent."""
    path = os.path.join(input_dir, LOAD_REPORT_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Warning: Could not load {LOAD_REPORT_FILENAME}: {e}")
        return None
    return data if isinstance(data, dict) and data else None


def _format_load_cell(cell: Any) -> str:
    """Render an {avg, peak} metric as 'avg / peak', or '-' when missing."""
    if not isinstance(cell, dict) or not cell.get('samples'):
        return '-'
    return f"{format_metric(cell.get('avg'))} / {format_metric(cell.get('peak'))}"


def format_load_markdown(load: Dict[str, Any]) -> List[str]:
    """Build the Markdown 'Hardware Load' section lines."""
    lines = ["## Hardware Load (avg / peak)", ""]
    hdrs = ['Benchmark ID', 'Camera SN', 'CPU %', 'GPU %']
    lines.append("| " + " | ".join(hdrs) + " |")
    lines.append("|" + "|".join("---" for _ in hdrs) + "|")
    for bid in sorted(load.keys()):
        cameras = load[bid] if isinstance(load[bid], dict) else {}
        for sn in sorted(cameras.keys()):
            s = cameras[sn] if isinstance(cameras[sn], dict) else {}
            cells = [bid, sn,
                     _format_load_cell(s.get('cpu_load')),
                     _format_load_cell(s.get('gpu_load'))]
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Column definitions for dynamic hiding
# ---------------------------------------------------------------------------

# (header, metric_key, suffix)
_SINGLE_COLUMNS = [
    ('Benchmark ID',           '_id',                  ''),
    ('FPS',                    'fps',                  ''),
    ('Period (ms)',             'grab_period_ms',       ''),
    ('Grab (ms)',              'grab_time_ms',         ''),
    ('grab() (ms)',            'grab_processing_ms',   ''),
    ('Depth Runtime (ms)',     'depth_runtime_ms',     ''),
    ('Retrieve Image (ms)',    'retrieve_image_ms',    ''),
    ('Retrieve Measure (ms)',  'retrieve_measure_ms',  ''),
    ('Retrieve Bodies (ms)',   'retrieve_bodies_ms',   ''),
    ('Retrieve Objects (ms)',  'retrieve_objects_ms',  ''),
    ('Get Position (ms)',      'get_position_ms',      ''),
    ('G2I (ms)',               'g2i_latency_ms',       ''),
    ('G2G (ms)',               'g2g_latency_ms',       ''),
]

# Columns always shown even if all values are None
_ALWAYS_SHOW = {'_id', 'fps', 'grab_period_ms', 'grab_time_ms'}


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
        sn = key.replace('Grab_', '')  # serial token, e.g. 'SN12345'
        m: Dict[str, Any] = {
            'serial': sn,
            'fps': None,
            'grab_time_ms': None,
            'grab_period_ms': None,
            'grab_processing_ms': None,
            'retrieve_image_ms': None,
            'retrieve_measure_ms': None,
            'retrieve_bodies_ms': None,
            'retrieve_objects_ms': None,
            'get_position_ms': None,
            'g2i_latency_ms': None,
            'g2g_latency_ms': None,
        }
        if v.get('grab_call_frequency_hz') is not None:
            m['fps'] = round(v['grab_call_frequency_hz'], 1)
        if 'grab_call_median_period_ms' in v:
            m['grab_period_ms'] = v['grab_call_median_period_ms']
        if v.get('gpu_stats') is not None:
            m['grab_time_ms'] = v['gpu_stats'].get('median')
        elif v.get('cpu_stats') is not None:
            m['grab_time_ms'] = v['cpu_stats'].get('median')
        # Per-event timings for this camera (RetrieveImage_<sn>, …).
        m.update(_extract_event_metrics(data, sn))
        # Glass-to-image / glass-to-glass are per-camera, keyed by the same serial (SN<sn>).
        # `or {}` guards a present-but-null latency_ms block.
        g2i = data.get('GlassToImageLatency_' + sn)
        if isinstance(g2i, dict):
            m['g2i_latency_ms'] = (g2i.get('latency_ms') or {}).get('median')
        g2g = data.get('GlassToGlassLatency_' + sn)
        if isinstance(g2g, dict):
            m['g2g_latency_ms'] = (g2g.get('latency_ms') or {}).get('median')
        cameras.append(m)
    return cameras


def _group_results(results: Dict[str, Any]):
    """Separate single-camera, multi-thread, and multi-process results.

    Returns (single, multi_thread, multi_process) where:
      single:        list of (benchmark_id, metrics)
      multi_thread:  list of (benchmark_id, per_camera_list)
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
            continue

        is_multi_thread = sum(1 for k in data if k.startswith('Grab_SN')) >= 2

        if is_multi_thread:
            multi_thread.append((benchmark_id, _extract_per_camera_metrics(data)))
        else:
            single.append((benchmark_id, extract_metrics(data)))

    for procs in multi_process.values():
        procs.sort(key=lambda x: x[0])

    return single, multi_thread, multi_process


# ---------------------------------------------------------------------------
# HTML table builders (multi-camera)
# ---------------------------------------------------------------------------

_PER_CAMERA_EVENT_COLUMNS = [
    ('grab() (ms)',           'grab_processing_ms'),
    ('Retrieve Image (ms)',   'retrieve_image_ms'),
    ('Retrieve Measure (ms)', 'retrieve_measure_ms'),
    ('Retrieve Bodies (ms)',  'retrieve_bodies_ms'),
    ('Retrieve Objects (ms)', 'retrieve_objects_ms'),
    ('Get Position (ms)',     'get_position_ms'),
]


def _per_camera_columns(cam_dicts: List[Dict]) -> List[Tuple[str, str]]:
    """Per-camera columns: base grab stats, then per-event and G2I/G2G columns that any camera has."""
    cols = [('FPS', 'fps'), ('Period (ms)', 'grab_period_ms'), ('Grab (ms)', 'grab_time_ms')]
    for hdr, key in _PER_CAMERA_EVENT_COLUMNS:
        if any(c.get(key) is not None for c in cam_dicts):
            cols.append((hdr, key))
    if any(c.get('g2i_latency_ms') is not None for c in cam_dicts):
        cols.append(('G2I (ms)', 'g2i_latency_ms'))
    if any(c.get('g2g_latency_ms') is not None for c in cam_dicts):
        cols.append(('G2G (ms)', 'g2g_latency_ms'))
    return cols


def _html_multi_thread_table(mt_results) -> List[str]:
    """Build an HTML table for multi-thread results with dynamic per-camera columns."""
    per_cam_cols = _per_camera_columns([c for _, cams in mt_results for c in (cams or [])])
    per_cam_hdrs = ["Benchmark ID", "Camera"] + [h for h, _ in per_cam_cols]

    lines: List[str] = []
    lines.append("<table>")
    hdr_cells = "".join(f"<th>{h}</th>" for h in per_cam_hdrs)
    lines.append(f"<tr>{hdr_cells}</tr>")

    for bid, cameras in mt_results:
        n = len(cameras) if cameras else 1
        if not cameras:
            empty_per = "<td>-</td>" * len(per_cam_cols)
            lines.append(f'<tr><td>{_esc(bid)}</td><td>-</td>{empty_per}</tr>')
            continue
        for i, cam in enumerate(cameras):
            cam_cells = "".join(f"<td>{format_metric(cam.get(key))}</td>" for _, key in per_cam_cols)
            if i == 0:
                lines.append(
                    f'<tr><td rowspan="{n}"><b>{_esc(bid)}</b></td>'
                    f'<td>{_esc(cam["serial"])}</td>{cam_cells}</tr>'
                )
            else:
                lines.append(f'<tr><td>{_esc(cam["serial"])}</td>{cam_cells}</tr>')

    lines.append("</table>")
    return lines


def _html_multi_process_table(mp_results: Dict[str, list]) -> List[str]:
    """Build an HTML table for multi-process results with dynamic per-camera columns."""
    per_cam_cols = _per_camera_columns([m for procs in mp_results.values() for _, m in procs])
    per_cam_hdrs = ["Benchmark ID", "Process"] + [h for h, _ in per_cam_cols]

    lines: List[str] = []
    lines.append("<table>")
    hdr_cells = "".join(f"<th>{h}</th>" for h in per_cam_hdrs)
    lines.append(f"<tr>{hdr_cells}</tr>")

    for base_id, procs in sorted(mp_results.items()):
        n = len(procs)
        for i, (proc_num, metrics) in enumerate(procs):
            cam_cells = "".join(f"<td>{format_metric(metrics.get(key))}</td>" for _, key in per_cam_cols)
            if i == 0:
                lines.append(
                    f'<tr><td rowspan="{n}"><b>{_esc(base_id)}</b></td>'
                    f'<td>{_esc(proc_num)}</td>{cam_cells}</tr>'
                )
            else:
                lines.append(f'<tr><td>{_esc(proc_num)}</td>{cam_cells}</tr>')

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
        lines.append("## Multi-Camera Multi-Thread Results")
        lines.append("")
        lines.extend(_html_multi_thread_table(multi_thread))
        lines.append("")

    # --- Multi-camera multi-process results ---
    if multi_process:
        lines.append("## Multi-Camera Multi-Process Results")
        lines.append("")
        lines.extend(_html_multi_process_table(multi_process))
        lines.append("")

    # --- Hardware load (per-camera SDK reports, aggregated by --monitor-load) ---
    load = load_load_report(input_dir)
    if load:
        lines.extend(format_load_markdown(load))

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


def _flat_camera_rows(results: Dict[str, Any]) -> List[Tuple[str, Dict]]:
    """Flatten results into (display_id, metrics) rows for the flat terminal table.

    A multi-camera (multi-thread) file holds several ``Grab_SN<serial>`` keys; ``extract_metrics`` is
    single-camera and would blend them (last-key-wins), so such a file is expanded to one row per
    camera (id suffixed with the serial). Single-camera and per-process files map to one row each.
    """
    rows: List[Tuple[str, Dict]] = []
    for benchmark_id in sorted(results.keys()):
        data = results[benchmark_id]
        if sum(1 for k in data if k.startswith('Grab_SN')) >= 2:
            for c in _extract_per_camera_metrics(data):
                rows.append((f"{benchmark_id} [{c['serial']}]", c))
        else:
            rows.append((benchmark_id, extract_metrics(data)))
    return rows


def print_table(input_dir: str) -> None:
    """Print results as a simple table to stdout."""
    results = load_json_results(input_dir)

    if not results:
        print(f"No results found in {input_dir}")
        return

    # Flatten to one row per camera (multi-camera files expand by serial) so the table never blends
    # metrics from different cameras into a single row.
    table_rows = _flat_camera_rows(results)
    all_metrics = [m for _, m in table_rows]

    col_defs = [
        ('Benchmark ID',  '_id',                  '', 40),
        ('FPS',            'fps',                  '',  8),
        ('Period(ms)',     'grab_period_ms',       '', 12),
        ('Grab(ms)',       'grab_time_ms',         '', 10),
        ('Depth(ms)',      'depth_runtime_ms',     '', 12),
        ('RetrImg(ms)',    'retrieve_image_ms',    '', 12),
        ('RetrMsr(ms)',    'retrieve_measure_ms',  '', 12),
        ('RetrBod(ms)',    'retrieve_bodies_ms',   '', 12),
        ('RetrObj(ms)',    'retrieve_objects_ms',   '', 12),
        ('GetPos(ms)',     'get_position_ms',      '', 12),
        ('G2I(ms)',        'g2i_latency_ms',       '', 10),
        ('G2G(ms)',        'g2g_latency_ms',       '', 10),
    ]

    always = {'_id', 'fps', 'grab_period_ms', 'grab_time_ms'}
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

    for display_id, metrics in table_rows:
        cells = []
        for i, (_, key, sfx, w) in enumerate(visible):
            if key == '_id':
                cells.append(f"{display_id:<{w}}")
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
        """
    )

    parser.add_argument('--input', '-i', required=True,
                        help='Input directory containing JSON result files')
    parser.add_argument('--output', '-o',
                        help='Output file for markdown report')
    parser.add_argument('--format', '-f', choices=['markdown', 'table', 'html'], default='markdown',
                        help='Output format (default: markdown)')

    args = parser.parse_args()

    if not os.path.isdir(args.input):
        print(f"Error: Directory not found: {args.input}")
        sys.exit(1)

    if args.format == 'html':
        if not args.output:
            parser.error("--output is required for HTML format")
        from generate_html_report import generate_html_report as gen_html
        gen_html(args.input, args.output)
        return

    if args.format == 'table' or not args.output:
        print_table(args.input)

    if args.output:
        generate_markdown_report(args.input, args.output)


if __name__ == '__main__':
    main()
