#!/usr/bin/env python3
"""
ZED SDK Benchmark HTML Report Generator

Generates self-contained, interactive HTML reports from benchmark JSON results.
One section per hardware setup (single camera / multi-thread / multi-process),
each with SVG charts in order: FPS, Hardware Load, per-frame latency (glass-to-image with the
grab() and retrieve*() call times stacked on top), Glass-to-Glass latency. A chart appears only
when at least one benchmark has its data; a benchmark (or stack segment) whose module did not run
draws nothing.

No external dependencies at view time: charts are inline SVG built from an
embedded DATA blob, so the report works offline.

Usage:
    python scripts/generate_html_report.py --input benchmark_output/ --output report.html
"""

import argparse
import json
import re
import sys
import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# Allow importing parse_results from the same directory
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import parse_results as pr


# ---------------------------------------------------------------------------
# Benchmark id parsing (resolution / fps / category → chart grouping & colours)
# ---------------------------------------------------------------------------

_CATEGORY_RULES = [
    ('body_tracking', ['_bt_', 'body_tracking']),
    ('object_detection', ['_od_', 'object_detection']),
    ('depth', ['_depth_', 'depth_neural', 'depth_performance', 'depth_quality', 'depth_ultra']),
    ('recording', ['_recording_', 'record']),
    ('streaming', ['_streaming_', 'stream']),
    ('capture', ['_capture_', 'capture']),
]

_CATEGORY_COLORS = {
    'capture': '#26c2f5',
    'depth': '#8e9cff',
    'recording': '#9aa7ba',
    'streaming': '#37d39f',
    'object_detection': '#f7b955',
    'body_tracking': '#37d39f',
    'other': '#ef8080',
}

_RES_ORDER = ['vga', 'svga', 'hd720', 'hd1080', 'hd1200', 'hd2k', '4k']
_RES_DISPLAY = {'vga': 'VGA', 'svga': 'SVGA', 'hd720': 'HD720',
                'hd1080': 'HD1080', 'hd1200': 'HD1200', 'hd2k': 'HD2K', '4k': '4K'}
_RES_HEX = {'VGA': '#37d39f', 'SVGA': '#37d39f', 'HD720': '#f7b955',
            'HD1080': '#26c2f5', 'HD1200': '#8e9cff', 'HD2K': '#ef7bd0', '4K': '#ef7bd0'}


def _categorize(benchmark_id: str) -> str:
    bid = benchmark_id.lower()
    for cat, patterns in _CATEGORY_RULES:
        if any(p in bid for p in patterns):
            return cat
    return 'other'


def _parse_bid(bid: str):
    """Extract (resolution_token, fps_target) from a conventional benchmark id
    like ``4zedx_mt_latency_hd1200_60``. Either part may be None."""
    b = bid.lower()
    res = None
    for r in sorted(_RES_ORDER, key=len, reverse=True):
        if f'_{r}_' in b or b.endswith(f'_{r}'):
            res = r
            break
    m = re.search(r'_(\d+)$', b)
    fps = int(m.group(1)) if m else None
    return res, fps


def _esc(s: str) -> str:
    """Escape HTML entities."""
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


# ---------------------------------------------------------------------------
# Hardware load attachment (load_report.json sidecar)
# ---------------------------------------------------------------------------

def _clean_load_stats(cell: Any) -> Optional[Dict]:
    if not isinstance(cell, dict) or not cell.get('samples'):
        return None
    return {'avg': cell.get('avg'), 'peak': cell.get('peak')}


def _attach_load(cameras: List[Dict], load_entry: Any) -> None:
    """Attach per-camera cpu_load/gpu_load from a load_report entry
    ({serial: {cpu_load, gpu_load}}). Serial keys in the sidecar have no 'SN' prefix."""
    if not isinstance(load_entry, dict):
        return
    for cam in cameras:
        sn = str(cam.get('serial') or '')
        digits = sn[2:] if sn.startswith('SN') else sn
        s = load_entry.get(digits) or load_entry.get(sn)
        if isinstance(s, dict):
            cam['cpu_load'] = _clean_load_stats(s.get('cpu_load'))
            cam['gpu_load'] = _clean_load_stats(s.get('gpu_load'))
    # Single camera whose serial couldn't be matched: a lone sidecar entry is unambiguous.
    if len(cameras) == 1 and not cameras[0].get('cpu_load') and len(load_entry) == 1:
        s = next(iter(load_entry.values()))
        if isinstance(s, dict):
            cameras[0]['cpu_load'] = _clean_load_stats(s.get('cpu_load'))
            cameras[0]['gpu_load'] = _clean_load_stats(s.get('gpu_load'))


def _aggregate_load(cameras: List[Dict]) -> Optional[Dict]:
    """Combo-level load for the chart: mean of per-camera averages, max of peaks."""
    out = {}
    for kind, short in (('cpu_load', 'cpu'), ('gpu_load', 'gpu')):
        stats = [c.get(kind) for c in cameras
                 if isinstance(c.get(kind), dict) and c[kind].get('avg') is not None]
        if stats:
            peaks = [s['peak'] for s in stats if s.get('peak') is not None]
            out[short] = {'avg': sum(s['avg'] for s in stats) / len(stats),
                          'peak': max(peaks) if peaks else None}
    return out or None


# ---------------------------------------------------------------------------
# DATA blob assembly
# ---------------------------------------------------------------------------

def _mk_combo(bid: str, cameras: List[Dict], load_entry: Any) -> Dict:
    _attach_load(cameras, load_entry)
    res, fps = _parse_bid(bid)
    if res:
        group = _RES_DISPLAY[res]
        color = _RES_HEX[group]
        rank = _RES_ORDER.index(res)
    else:
        group = _categorize(bid)
        color = _CATEGORY_COLORS.get(group, _CATEGORY_COLORS['other'])
        rank = 100
    # Short label used when several benchmarks share the same resolution/fps:
    # the bid minus the redundant resolution and fps tokens.
    short = bid
    if fps is not None:
        short = re.sub(r'_%d$' % fps, '', short)
    if res:
        short = re.sub('_%s' % re.escape(res), '', short, flags=re.IGNORECASE)
    return {
        'bid': bid,
        'short': short,
        'group': group,
        'color': color,
        'rank': rank,
        'fps_target': fps,
        'cameras': cameras,
        'load': _aggregate_load(cameras),
    }


def _single_camera_dict(data: Dict, metrics: Dict) -> Dict:
    """Wrap single-camera metrics as a one-element camera list entry."""
    sn = next((k[len('Grab_'):] for k in data if k.startswith('Grab_SN')), None)
    cam = dict(metrics)
    cam['serial'] = sn or '-'
    return cam


def _mk_section(key: str, title: str, mode: str, benchmarks: List[Dict],
                camera_name: Optional[str]) -> Dict:
    benchmarks.sort(key=lambda c: (c['rank'], c['group'], c['fps_target'] or 0, c['bid']))
    max_cams = max((len(c['cameras']) for c in benchmarks), default=1)
    unit = 'process' if key == 'multi_process' else (camera_name or 'camera')
    return {
        'key': key,
        'title': title,
        'mode': mode,
        'rig': f'{max_cams} × {unit}',
        'benchmarks': benchmarks,
    }


def _build_data(results: Dict[str, Any], load: Optional[Dict],
                config: Optional[Dict], generated: str) -> Dict:
    sys_info = pr.get_system_info()
    jc = pr.extract_jetson_clocks(results)
    single, multi_thread, multi_process = pr._group_results(results)
    camera_name = (config or {}).get('camera')

    sections = []
    if single:
        combos = [_mk_combo(bid, [_single_camera_dict(results[bid], m)],
                            (load or {}).get(bid))
                  for bid, m in single]
        sections.append(_mk_section('single', 'Single Camera', 'Single camera',
                                    combos, camera_name))
    if multi_thread:
        combos = [_mk_combo(bid, cams, (load or {}).get(bid))
                  for bid, cams in multi_thread]
        sections.append(_mk_section('multi_thread', 'Multi-Camera · Multi-Thread',
                                    'Multi-thread', combos, camera_name))
    if multi_process:
        combos = []
        for base_id, procs in sorted(multi_process.items()):
            cameras = []
            for proc_num, m in procs:
                cam = dict(m)
                cam['serial'] = f'proc {proc_num}'
                _attach_load([cam], (load or {}).get(f'{base_id}_process_{proc_num}'))
                cameras.append(cam)
            combos.append(_mk_combo(base_id, cameras, None))
        sections.append(_mk_section('multi_process', 'Multi-Camera · Multi-Process',
                                    'Multi-process', combos, camera_name))

    return {
        'generated': generated,
        'config': config or None,
        'system': sys_info,
        'jetson': jc,
        'sections': sections,
    }


# ---------------------------------------------------------------------------
# HTML template (style matched to the Glass-to-Image latency report)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#0b0e13; --panel:#121722; --panel-2:#171d2a; --line:#273142;
    --line-soft:#1c2433; --ink:#e9eef6; --ink-dim:#9aa7ba; --ink-faint:#5c6a7e;
    --accent:#26c2f5; --good:#37d39f; --warn:#f7b955; --grid:#202838;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{
    margin:0;background:
      radial-gradient(1200px 600px at 85% -10%, #15202f 0%, transparent 60%),
      var(--bg);
    color:var(--ink);font-family:var(--sans);line-height:1.5;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:1080px;margin:0 auto;padding:0 24px 96px}

  /* ---- Header ---- */
  header.report{padding:64px 0 34px;border-bottom:1px solid var(--line)}
  .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.32em;
    text-transform:uppercase;color:var(--accent);margin:0 0 16px}
  h1{font-size:clamp(34px,6vw,58px);line-height:1.02;margin:0;font-weight:680;
    letter-spacing:-.025em}
  h1 .dim{color:var(--ink-faint);font-weight:300}
  .lede{color:var(--ink-dim);max-width:60ch;margin:18px 0 0;font-size:15px}
  .runmeta{display:flex;flex-wrap:wrap;gap:0;margin-top:30px;
    border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--panel)}
  .runmeta .cell{padding:14px 18px;border-right:1px solid var(--line-soft);flex:1 1 auto;min-width:150px}
  .runmeta .cell:last-child{border-right:0}
  .runmeta .k{font-family:var(--mono);font-size:10px;letter-spacing:.16em;
    text-transform:uppercase;color:var(--ink-faint)}
  .runmeta .v{font-family:var(--mono);font-size:14px;color:var(--ink);margin-top:5px}

  /* ---- Controls ---- */
  .controls{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;gap:18px 28px;
    align-items:center;padding:14px 0;margin-top:8px;
    background:linear-gradient(var(--bg),var(--bg) 70%,transparent);
    border-bottom:1px solid var(--line-soft)}
  .ctl-label{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
    text-transform:uppercase;color:var(--ink-faint);margin-right:2px}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .seg button{appearance:none;background:transparent;border:0;color:var(--ink-dim);
    font-family:var(--mono);font-size:12px;padding:7px 13px;cursor:pointer;transition:.15s}
  .seg button+button{border-left:1px solid var(--line)}
  .seg button[aria-pressed="true"]{background:var(--accent);color:#04141c;font-weight:600}
  .legend{display:flex;gap:16px;margin-left:auto;flex-wrap:wrap}
  .legend .item{display:flex;align-items:center;gap:7px;font-family:var(--mono);
    font-size:11.5px;color:var(--ink-dim)}
  .swatch{width:11px;height:11px;border-radius:3px;display:inline-block}

  /* ---- Setup section ---- */
  .setup{padding:46px 0 8px;border-bottom:1px solid var(--line-soft)}
  .setup-head{display:flex;align-items:flex-end;gap:18px;flex-wrap:wrap;margin-bottom:6px}
  .setup-idx{font-family:var(--mono);font-size:12px;color:var(--ink-faint);
    border:1px solid var(--line);border-radius:6px;padding:3px 8px}
  .setup h2{font-size:25px;margin:0;font-weight:640;letter-spacing:-.015em}
  .rig{font-family:var(--mono);font-size:12.5px;color:var(--accent);
    background:rgba(38,194,245,.08);border:1px solid rgba(38,194,245,.25);
    border-radius:999px;padding:4px 12px}
  .mode-tag{font-family:var(--mono);font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.12em}

  .stats{display:flex;gap:30px;flex-wrap:wrap;margin:20px 0 8px}
  .stat .k{font-family:var(--mono);font-size:10px;letter-spacing:.14em;
    text-transform:uppercase;color:var(--ink-faint)}
  .stat .v{font-family:var(--mono);font-size:20px;margin-top:3px}
  .stat .v small{font-size:11px;color:var(--ink-dim);font-weight:400}
  .stat .v.good{color:var(--good)} .stat .v.high{color:var(--warn)}

  .chart-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:18px 16px 10px;margin-top:14px;position:relative}
  .chart-title{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px;font-family:var(--mono);
    font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-dim);
    margin:0 4px 6px}
  .chart-title .unit{color:var(--ink-faint);letter-spacing:.04em;text-transform:none}
  .chart-title .clegend{margin-left:auto;display:flex;flex-wrap:wrap;gap:6px 12px;text-transform:none;letter-spacing:.02em}
  .chart-title .clegend .item{display:flex;align-items:center;gap:6px;font-size:11px}
  .clegend button.item{appearance:none;background:none;border:0;padding:2px 0;margin:0;color:inherit;
    font:inherit;letter-spacing:inherit;cursor:pointer}
  .clegend button.item:hover{color:var(--ink)}
  .clegend button.item:focus-visible{outline:1px solid var(--accent);outline-offset:2px;border-radius:3px}
  .clegend button.item[aria-pressed="false"]{opacity:.4;text-decoration:line-through}
  svg.chart{display:block;width:100%;height:auto}
  .bar{transition:filter .15s ease}
  .bar:hover{filter:brightness(1.15)}
  .col-hit{cursor:pointer}
  .col-hit:hover rect.colbg{fill:rgba(255,255,255,.025)}

  .flags{margin:10px 4px 2px;display:flex;flex-direction:column;gap:4px}
  .flag{font-family:var(--mono);font-size:11.5px;color:var(--warn);display:flex;gap:8px;align-items:baseline}
  .flag .dot{color:var(--warn)}

  /* ---- Tooltip ---- */
  #tip{position:fixed;pointer-events:none;z-index:60;opacity:0;transform:translateY(4px);
    transition:opacity .12s;background:#0a0f17;border:1px solid var(--line);
    border-radius:10px;padding:10px 12px;font-family:var(--mono);font-size:11.5px;
    color:var(--ink);box-shadow:0 12px 40px rgba(0,0,0,.55);max-width:300px}
  #tip .tt-h{color:var(--accent);font-weight:600;margin-bottom:6px;word-break:break-all}
  #tip .tt-row{display:flex;justify-content:space-between;gap:18px;color:var(--ink-dim)}
  #tip .tt-row b{color:var(--ink);font-weight:600}

  /* ---- Tables ---- */
  .tables{padding-top:54px}
  .tables>h2{font-size:22px;margin:0 0 4px;letter-spacing:-.015em}
  .tables>p{color:var(--ink-dim);margin:0 0 22px;font-size:14px}
  details.tbl{border:1px solid var(--line);border-radius:12px;margin-bottom:12px;
    background:var(--panel);overflow:hidden}
  details.tbl>summary{list-style:none;cursor:pointer;padding:15px 18px;display:flex;
    align-items:center;gap:14px;font-family:var(--sans);user-select:none}
  details.tbl>summary::-webkit-details-marker{display:none}
  .summary-chev{width:9px;height:9px;border-right:2px solid var(--ink-dim);
    border-bottom:2px solid var(--ink-dim);transform:rotate(-45deg);transition:.2s;flex:none}
  details[open]>summary .summary-chev{transform:rotate(45deg)}
  details.tbl summary .s-title{font-weight:600;font-size:15px}
  details.tbl summary .s-rig{font-family:var(--mono);font-size:11.5px;color:var(--ink-faint);margin-left:auto}
  .tbl-scroll{overflow-x:auto;border-top:1px solid var(--line-soft)}
  table{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:12px}
  thead th{position:sticky;top:0;background:var(--panel-2);color:var(--ink-dim);
    font-weight:500;text-align:right;padding:9px 12px;border-bottom:1px solid var(--line);
    white-space:nowrap;letter-spacing:.02em}
  thead th.l,tbody td.l{text-align:left}
  tbody td{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line-soft);
    color:var(--ink);white-space:nowrap}
  tbody tr:hover td{background:rgba(255,255,255,.018)}
  tbody td.hi{color:var(--accent);font-weight:600}
  tbody td.muted{color:var(--ink-faint)}
  .grp-first td{border-top:1px solid var(--line)}
  td .sn{color:var(--ink-dim)}
  td .bid{color:var(--ink);font-size:11.5px}
  .res-pill{font-size:10px;padding:1px 7px;border-radius:5px;font-weight:600;margin-right:8px}

  /* ---- Raw data ---- */
  details.raw{margin-top:32px;border:1px solid var(--line);border-radius:12px;
    background:var(--panel);overflow:hidden}
  details.raw>summary{list-style:none;cursor:pointer;color:var(--ink-dim);
    font-family:var(--mono);font-size:12px;padding:14px 18px;user-select:none}
  details.raw>summary::-webkit-details-marker{display:none}
  details.raw pre{margin:0;border-top:1px solid var(--line-soft);
    padding:16px 18px;overflow:auto;font-family:var(--mono);font-size:11px;
    max-height:600px;color:var(--ink-faint);line-height:1.45}

  footer{padding-top:40px;color:var(--ink-faint);font-family:var(--mono);font-size:11.5px;
    border-top:1px solid var(--line-soft);margin-top:42px}
  ::selection{background:rgba(38,194,245,.3)}
  @media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
  @media (max-width:640px){
    .wrap{padding:0 16px 72px} header.report{padding:44px 0 26px}
    .stats{gap:20px} .legend{width:100%;margin-left:0}
  }
</style>
</head>
<body>
<div class="wrap">

  <header class="report">
    <p class="eyebrow">ZED SDK Benchmark · Performance Report</p>
    <h1>Benchmark<br><span class="dim">Report</span></h1>
    <p class="lede" id="lede"></p>
    <div class="runmeta" id="runmeta"></div>
  </header>

  <div class="controls" role="toolbar" aria-label="Chart controls">
    <span class="ctl-label">Y-axis</span>
    <div class="seg" id="seg-sync">
      <button data-v="auto" aria-pressed="true">Per-section scale</button>
      <button data-v="sync" aria-pressed="false">Locked scale</button>
    </div>
    <span class="ctl-label">Bar value</span>
    <div class="seg" id="seg-agg">
      <button data-v="mean" aria-pressed="true">Mean</button>
      <button data-v="worst" aria-pressed="false">Worst-case</button>
    </div>
    <div class="legend" id="legend"></div>
  </div>

  <main id="sections"></main>

  <section class="tables">
    <h2>Full benchmark tables</h2>
    <p>Per-camera detail for every benchmark. Click a setup to expand.</p>
    <div id="tables"></div>
  </section>

  <details class="raw">
    <summary>Raw Benchmark Data (JSON)</summary>
    <pre>__RAW__</pre>
  </details>

  <footer>
    <div id="footmeta"></div>
    <div style="margin-top:8px">G2I = Glass-to-Image (capture to end of read(), plus retrieveImage()), G2G = Glass-to-Glass
      latency (ms), lower is better. Latency per frame stacks the median of each stage (G2I, then grab() alone and
      the retrieve*() calls), so its total approximates, not equals, the median end-to-end time. Grab in the
      table is read() + grab().
      Hardware load is the SDK per-camera CPU/GPU utilisation (avg, peak in tooltip).
      Charts rendered from the raw benchmark exports; no values altered.</div>
  </footer>
</div>

<div id="tip" role="tooltip"></div>

<script>
const DATA = __DATA__;
let MODE = {sync:false, agg:"mean"};
// Latency stages hidden by clicking their legend entry (shared by every section).
const HIDDEN = new Set();

const LOAD_CPU = "#26c2f5", LOAD_GPU = "#8e9cff";
// Per-frame latency stack, bottom to top. The stages do not overlap: glass-to-image covers the
// capture up to the end of read() plus retrieveImage(); grab() is the SDK processing call alone
// (read() excluded); the retrieve*() / getPosition() calls follow it. A stage whose module did
// not run is absent, so it draws nothing.
const LAT_STACK = [
  {key:"g2i_latency_ms",      name:"Glass-to-image",    color:"#26c2f5"},
  {key:"grab_processing_ms",  name:"grab()",            color:"#8e9cff"},
  {key:"retrieve_measure_ms", name:"retrieveMeasure()", color:"#37d39f"},
  {key:"retrieve_objects_ms", name:"retrieveObjects()", color:"#f7b955"},
  {key:"retrieve_bodies_ms",  name:"retrieveBodies()",  color:"#f47ea8"},
  {key:"get_position_ms",     name:"getPosition()",     color:"#c792ea"},
];
const METRICS = [
  {id:"fps",  title:"FPS",                     unit:"frames / s", key:"fps",            worst:"min"},
  {id:"load", title:"Hardware Load",           unit:"% util (avg)"},
  {id:"lat",  title:"Latency per Frame",       unit:"ms, median per stage", stack:LAT_STACK, worst:"max"},
  {id:"g2g",  title:"Glass-to-Glass Latency",  unit:"ms",         key:"g2g_latency_ms", worst:"max"},
];

/* ---------- helpers ---------- */
const esc = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const mean = a => a.reduce((x,y)=>x+y,0)/a.length;
const fmt = v => v==null ? "–" : (Math.abs(v)>=100 ? v.toFixed(0) : Math.abs(v)<1 ? v.toFixed(2) : v.toFixed(1));
const camVals = (c,key) => c.cameras.map(k=>k[key]).filter(v=>v!=null);
// Stages with data for this benchmark, then the subset currently shown (not hidden in the legend).
const stackPresent = (c,m) => m.stack.filter(seg=>camVals(c,seg.key).length>0);
const stackSegs = (c,m) => stackPresent(c,m).filter(seg=>!HIDDEN.has(seg.key));
const comboHas = (c,m) => m.id==="load" ? !!c.load
  : m.stack ? stackPresent(c,m).length>0
  : camVals(c,m.key).length>0;
const sectionCombos = (s,m) => s.benchmarks.filter(c=>comboHas(c,m));
const comboLabel = c => c.dupe ? c.short
  : (c.fps_target!=null ? `${c.group.toUpperCase()} · ${c.fps_target}` : c.bid);

// Mark benchmarks whose (group, fps target) is not unique within their section,
// so charts and stats label them by id instead of an ambiguous "RES · fps".
DATA.sections.forEach(s=>{
  const seen={};
  s.benchmarks.forEach(c=>{ const k=c.group+"|"+c.fps_target; seen[k]=(seen[k]||0)+1; });
  s.benchmarks.forEach(c=>{ c.dupe = seen[c.group+"|"+c.fps_target]>1; });
});

function barValue(vals, m){
  if(MODE.agg==="mean" || vals.length===1) return mean(vals);
  return m.worst==="min" ? Math.min(...vals) : Math.max(...vals);
}
// Stacked bar: each present stage's value (aggregated across cameras) and their sum.
function stackParts(c, m){
  const parts=stackSegs(c,m).map(seg=>({seg, v:barValue(camVals(c,seg.key),m)}));
  return {parts, total:parts.reduce((a,p)=>a+p.v,0)};
}

function niceScale(max){
  const steps=[0.01,0.02,0.05,0.1,0.2,0.25,0.5,1,2,5,10,15,20,25,30,40,50,75,100,150,200];
  const rough=max/5;
  let step=steps.find(s=>s>=rough) || Math.ceil(rough/100)*100;
  const niceMax=Math.ceil(max/step)*step || step;
  // toFixed(2) then unary +: keeps sub-ms ticks exact (0.1, 0.2, ...) and integer ticks as before
  const ticks=[]; for(let v=0;v<=niceMax+1e-6;v+=step) ticks.push(+v.toFixed(2));
  return {niceMax, ticks, step};
}

function metricMax(m, section){
  const secs = section ? [section] : DATA.sections;
  let mx = 0;
  secs.forEach(sec=>sectionCombos(sec,m).forEach(c=>{
    if(m.id==="load"){
      if(c.load.cpu) mx=Math.max(mx, c.load.cpu.avg);
      if(c.load.gpu) mx=Math.max(mx, c.load.gpu.avg);
    } else if(m.stack){
      mx=Math.max(mx, stackParts(c,m).total);
    } else {
      mx=Math.max(mx, ...camVals(c,m.key));
    }
  }));
  return mx || 1;
}

/* ---------- metadata header ---------- */
(function(){
  const sys = DATA.system || {};
  const total = DATA.sections.reduce((n,s)=>n+s.benchmarks.length,0);
  const cfg = DATA.config || {};
  const ledeBits = [
    `Frames per second, hardware load and end-to-end latency measured across <b>${total}</b> benchmark run${total===1?"":"s"}`,
    "one section per hardware setup, one bar per benchmark."
  ];
  document.getElementById("lede").innerHTML = ledeBits.join(": ") +
    (cfg.name ? ` Configuration: <b>${esc(cfg.name)}</b>.` : "");
  const cells = [];
  if(sys.GPU) cells.push(["Platform", sys.GPU.replace("NVIDIA ","").replace(" Developer Kit","")]);
  if(sys.CPU) cells.push(["CPU", sys.CPU]);
  if(sys.RAM) cells.push(["RAM", sys.RAM]);
  if(sys["ZED SDK"]) cells.push(["ZED SDK", sys["ZED SDK"]]);
  if(DATA.jetson && DATA.jetson.nvpmodel) cells.push(["NVP Model", DATA.jetson.nvpmodel]);
  cells.push(["Benchmarks", String(total)]);
  cells.push(["Generated", DATA.generated.split(" ")[0]]);
  document.getElementById("runmeta").innerHTML = cells.map(c=>
    `<div class="cell"><div class="k">${esc(c[0])}</div><div class="v">${esc(c[1])}</div></div>`).join("");
  document.getElementById("footmeta").textContent =
    [sys.GPU, sys.CPU, sys["ZED SDK"] ? "ZED SDK "+sys["ZED SDK"] : null, "generated "+DATA.generated]
      .filter(Boolean).join(" · ");
  // legend: unique group/colour pairs across every section
  const groups = new Map();
  DATA.sections.forEach(s=>s.benchmarks.forEach(c=>groups.set(c.group, c.color)));
  document.getElementById("legend").innerHTML = [...groups].map(([g,col])=>
    `<span class="item"><span class="swatch" style="background:${col}"></span>${esc(g.toUpperCase())}</span>`).join("");
})();

/* ---------- chart builder ---------- */
function buildChart(si, s, m){
  // Every benchmark keeps its slot so charts line up; one without this metric draws no bar.
  const combos = s.benchmarks;
  const W=760, H=340, PL=54, PR=14, PT=26, PB=68;
  const plotW=W-PL-PR, plotH=H-PT-PB;
  const n=combos.length;
  const slot=plotW/n, barW=Math.min(38, slot*0.54);
  const sc=niceScale(metricMax(m, MODE.sync?null:s));
  const y=v=>PT+plotH-(Math.min(v,sc.niceMax)/sc.niceMax)*plotH;

  let out=`<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(s.title)} ${esc(m.title)} by benchmark">`;
  // gridlines + y labels
  for(const t of sc.ticks){
    out+=`<line x1="${PL}" y1="${y(t)}" x2="${W-PR}" y2="${y(t)}" stroke="var(--grid)" stroke-width="1"/>`;
    out+=`<text x="${PL-9}" y="${y(t)+3.5}" text-anchor="end" font-family="var(--mono)" font-size="10" fill="var(--ink-faint)">${t}</text>`;
  }
  out+=`<text x="${PL-9}" y="${PT-10}" text-anchor="end" font-family="var(--mono)" font-size="9.5" fill="var(--ink-faint)" letter-spacing="1">${esc(m.id==="load"?"%":(m.id==="fps"?"fps":"ms"))}</text>`;
  out+=`<line x1="${PL}" y1="${y(0)}" x2="${W-PR}" y2="${y(0)}" stroke="var(--line)" stroke-width="1.5"/>`;

  // group brackets
  let gi=0;
  while(gi<n){
    let gj=gi; const g=combos[gi].group, col=combos[gi].color;
    while(gj<n && combos[gj].group===g) gj++;
    const x1=PL+gi*slot+slot*0.12, x2=PL+gj*slot-slot*0.12, ym=H-PB+30;
    out+=`<line x1="${x1}" y1="${ym}" x2="${x2}" y2="${ym}" stroke="${col}" stroke-width="2" opacity="0.55"/>`;
    out+=`<text x="${(x1+x2)/2}" y="${ym+15}" text-anchor="middle" font-family="var(--mono)" font-size="10.5" letter-spacing="1" fill="${col}">${esc(g.toUpperCase())}</text>`;
    gi=gj;
  }

  // bars
  combos.forEach((c,i)=>{
    const cx=PL+i*slot+slot/2;
    out+=`<g class="col-hit" data-s="${si}" data-c="${s.benchmarks.indexOf(c)}">`;
    out+=`<rect class="colbg" x="${PL+i*slot+1}" y="${PT}" width="${slot-2}" height="${plotH}" fill="transparent"/>`;

    if(!comboHas(c,m)){
      // module not enabled for this benchmark: nothing drawn
    } else if(m.id==="load"){
      const bw=Math.min(16, slot*0.24);
      [["cpu",LOAD_CPU,-bw-1.5],["gpu",LOAD_GPU,1.5]].forEach(([k,col,dx])=>{
        const st=c.load[k];
        if(!st || st.avg==null) return;
        const top=y(st.avg), bh=Math.max(2, y(0)-top);
        out+=`<rect class="bar" x="${cx+dx}" y="${top}" width="${bw}" height="${bh}" rx="2.5" fill="${col}" fill-opacity="0.9"/>`;
        out+=`<text x="${cx+dx+bw/2}" y="${Math.max(PT+11, top-5)}" text-anchor="middle" font-family="var(--mono)" font-size="9" fill="var(--ink-dim)">${fmt(st.avg)}</text>`;
      });
    } else if(m.stack){
      // Stacked stages, bottom to top; the total is labelled above the bar.
      const {parts,total}=stackParts(c,m);
      let base=0;
      parts.forEach(({seg,v})=>{
        const yb=y(base), yt=y(base+v), h=Math.max(1, yb-yt);
        out+=`<rect class="bar" x="${cx-barW/2}" y="${yb-h}" width="${barW}" height="${h}" fill="${seg.color}" fill-opacity="0.9"/>`;
        if(h>=14)
          out+=`<text x="${cx}" y="${yb-h/2+3.5}" text-anchor="middle" font-family="var(--mono)" font-size="9" fill="#0b0e13">${fmt(v)}</text>`;
        base+=v;
      });
      // no total when every stage of this bar is hidden
      if(parts.length)
        out+=`<text x="${cx}" y="${Math.max(PT+12, y(total)-7)}" text-anchor="middle" font-family="var(--mono)" font-size="11" font-weight="600" fill="var(--ink)">${fmt(total)}</text>`;
    } else {
      const vals=camVals(c,m.key);
      const val=barValue(vals,m);
      const top=y(val), bh=Math.max(2, y(0)-top);
      const col=c.color;
      out+=`<rect class="bar" x="${cx-barW/2}" y="${top}" width="${barW}" height="${bh}" rx="3" fill="${col}" fill-opacity="0.9"/>`;
      out+=`<rect x="${cx-barW/2}" y="${top}" width="${barW}" height="${Math.min(bh,5)}" rx="3" fill="#fff" fill-opacity="0.14"/>`;

      // whisker + per-camera dots for multi-cam spreads
      const vmin=Math.min(...vals), vmax=Math.max(...vals);
      if(vals.length>1 && vmax!==vmin){
        const yt=y(vmax), yb=y(vmin), cap=6;
        out+=`<line x1="${cx}" y1="${yt}" x2="${cx}" y2="${yb}" stroke="var(--ink)" stroke-opacity="0.55" stroke-width="1.3"/>`;
        out+=`<line x1="${cx-cap}" y1="${yt}" x2="${cx+cap}" y2="${yt}" stroke="var(--ink)" stroke-opacity="0.7" stroke-width="1.3"/>`;
        out+=`<line x1="${cx-cap}" y1="${yb}" x2="${cx+cap}" y2="${yb}" stroke="var(--ink)" stroke-opacity="0.7" stroke-width="1.3"/>`;
        const dn=vals.length;
        vals.forEach((v,j)=>{
          const jx=cx-barW*0.30 + (dn>1 ? (j/(dn-1))*barW*0.60 : 0);
          out+=`<circle cx="${jx}" cy="${y(v)}" r="2" fill="#0b0e13" stroke="${col}" stroke-width="1.4" opacity="0.95"/>`;
        });
      }
      const labY=Math.max(PT+12, top-7);
      out+=`<text x="${cx}" y="${labY}" text-anchor="middle" font-family="var(--mono)" font-size="11" font-weight="600" fill="var(--ink)">${fmt(val)}</text>`;
    }

    // under-axis label: fps target when parsed and unambiguous, otherwise a truncated id
    let lbl = (c.fps_target!=null && !c.dupe) ? String(c.fps_target) : (c.dupe ? c.short : c.bid);
    const maxChars = Math.max(4, Math.floor(slot/7));
    if(lbl.length>maxChars) lbl = "…"+lbl.slice(-(maxChars-1));
    out+=`<text x="${cx}" y="${H-PB+16}" text-anchor="middle" font-family="var(--mono)" font-size="11" fill="var(--ink-dim)">${esc(lbl)}</text>`;
    out+=`</g>`;
  });
  const caption = combos.every(c=>c.fps_target!=null && !c.dupe) ? "FPS →" : "BENCHMARK →";
  out+=`<text x="${PL}" y="${H-6}" font-family="var(--mono)" font-size="9.5" fill="var(--ink-faint)" letter-spacing="1">${caption}</text>`;
  out+=`</svg>`;
  return out;
}

/* ---------- summary stats ---------- */
function sectionStats(s){
  const cells=[];
  cells.push({k:"Benchmarks", v:String(s.benchmarks.length), sm:"", cls:""});
  const cams=Math.max(...s.benchmarks.map(c=>c.cameras.length));
  cells.push({k:s.key==="multi_process"?"Processes":"Cameras", v:String(cams), sm:" concurrent", cls:""});
  const withFps=s.benchmarks.filter(c=>camVals(c,"fps").length);
  if(withFps.length){
    const best=withFps.reduce((a,b)=>mean(camVals(b,"fps"))>mean(camVals(a,"fps"))?b:a);
    cells.push({k:"Peak FPS", v:fmt(mean(camVals(best,"fps"))), sm:" · "+comboLabel(best), cls:"good"});
  }
  [["g2i_latency_ms","G2I"],["g2g_latency_ms","G2G"]].forEach(([key,name])=>{
    const withIt=s.benchmarks.filter(c=>camVals(c,key).length);
    if(withIt.length){
      const lo=withIt.reduce((a,b)=>mean(camVals(b,key))<mean(camVals(a,key))?b:a);
      const hi=withIt.reduce((a,b)=>mean(camVals(b,key))>mean(camVals(a,key))?b:a);
      cells.push({k:"Lowest "+name, v:fmt(mean(camVals(lo,key))), sm:" ms · "+comboLabel(lo), cls:"good"});
      cells.push({k:"Highest "+name, v:fmt(mean(camVals(hi,key))), sm:" ms · "+comboLabel(hi), cls:"high"});
    }
  });
  return cells;
}

/* ---------- fps shortfall flags ---------- */
function fpsFlags(s){
  const out=[];
  s.benchmarks.forEach(c=>{
    const vals=camVals(c,"fps");
    if(c.fps_target!=null && vals.length && mean(vals) < c.fps_target*0.97){
      out.push(`${comboLabel(c)} sustained ~${mean(vals).toFixed(0)} fps against a ${c.fps_target} fps target.`);
    }
  });
  return out;
}

/* ---------- render sections ---------- */
function renderSections(){
  const host=document.getElementById("sections");
  host.innerHTML=DATA.sections.map((s,si)=>{
    const stats=sectionStats(s).map(c=>
      `<div class="stat"><div class="k">${esc(c.k)}</div><div class="v ${c.cls}">${c.v}<small>${esc(c.sm)}</small></div></div>`).join("");
    // charts in fixed order (METRICS), only when at least one benchmark has the data
    const charts=METRICS.map(m=>{
      if(!sectionCombos(s,m).length) return "";
      const clegend = m.id==="load"
        ? `<span class="clegend"><span class="item"><span class="swatch" style="background:${LOAD_CPU}"></span>CPU</span>` +
          `<span class="item"><span class="swatch" style="background:${LOAD_GPU}"></span>GPU</span></span>`
        : m.stack
        ? `<span class="clegend">` + m.stack.filter(seg=>s.benchmarks.some(c=>camVals(c,seg.key).length))
            .map(seg=>`<button type="button" class="item" data-seg="${esc(seg.key)}" aria-pressed="${!HIDDEN.has(seg.key)}" title="Show or hide ${esc(seg.name)}">` +
              `<span class="swatch" style="background:${seg.color}"></span>${esc(seg.name)}</button>`).join("") + `</span>`
        : "";
      return `<div class="chart-card"><div class="chart-title">${esc(m.title)}<span class="unit">${esc(m.unit)}</span>${clegend}</div>${buildChart(si,s,m)}</div>`;
    }).join("");
    const flags=fpsFlags(s);
    const flagHtml=flags.length?`<div class="flags">${flags.map(f=>`<div class="flag"><span class="dot">▲</span><span>${esc(f)}</span></div>`).join("")}</div>`:"";
    return `<section class="setup">
      <div class="setup-head">
        <span class="setup-idx">${String(si+1).padStart(2,"0")}</span>
        <h2>${esc(s.title)}</h2>
        <span class="rig">${esc(s.rig)}</span>
        <span class="mode-tag">${esc(s.mode)}</span>
      </div>
      <div class="stats">${stats}</div>
      ${charts}
      ${flagHtml}
    </section>`;
  }).join("");
  attachTips();
}

/* ---------- tooltip ---------- */
const tip=document.getElementById("tip");
function tipHtml(s,c){
  const row=(k,v,style)=>v==null?"":`<div class="tt-row"><span>${k}</span><b${style?` style="${style}"`:""}>${v}</b></div>`;
  let h=`<div class="tt-h">${esc(c.bid)}</div>`;
  const fps=camVals(c,"fps");
  if(fps.length){
    const bad=c.fps_target!=null && mean(fps)<c.fps_target*0.97;
    h+=row("fps", fmt(mean(fps))+(c.fps_target!=null?` / ${c.fps_target}`:""), bad?"color:var(--warn)":null);
  }
  const period=camVals(c,"grab_period_ms"), grab=camVals(c,"grab_time_ms");
  if(period.length) h+=row("period", fmt(mean(period))+" ms");
  if(grab.length) h+=row("grab", fmt(mean(grab))+" ms");
  [["g2i_latency_ms","G2I"],["grab_processing_ms","grab()"],["retrieve_measure_ms","retrieveMeasure()"],
   ["retrieve_objects_ms","retrieveObjects()"],["retrieve_bodies_ms","retrieveBodies()"],
   ["get_position_ms","getPosition()"],["g2g_latency_ms","G2G"]].forEach(([key,name])=>{
    const v=camVals(c,key);
    if(!v.length) return;
    if(v.length>1 && Math.max(...v)!==Math.min(...v)){
      h+=row(name+" range", `${fmt(Math.min(...v))}–${fmt(Math.max(...v))} ms`);
      h+=row(name+" mean", fmt(mean(v))+" ms");
    } else {
      h+=row(name, fmt(v[0])+" ms");
    }
  });
  if(c.load){
    if(c.load.cpu) h+=row("CPU", `${fmt(c.load.cpu.avg)}% · pk ${fmt(c.load.cpu.peak)}%`);
    if(c.load.gpu) h+=row("GPU", `${fmt(c.load.gpu.avg)}% · pk ${fmt(c.load.gpu.peak)}%`);
  }
  if(c.cameras.length>1) h+=row("cameras", String(c.cameras.length));
  return h;
}
function attachTips(){
  document.querySelectorAll(".col-hit").forEach(g=>{
    g.addEventListener("mousemove",e=>{
      const s=DATA.sections[+g.dataset.s], c=s.benchmarks[+g.dataset.c];
      tip.innerHTML=tipHtml(s,c);
      tip.style.opacity="1";
      let x=e.clientX+16, yv=e.clientY+16;
      const r=tip.getBoundingClientRect();
      if(x+r.width>innerWidth-10) x=e.clientX-r.width-16;
      if(yv+r.height>innerHeight-10) yv=e.clientY-r.height-16;
      tip.style.left=x+"px"; tip.style.top=yv+"px";
    });
    g.addEventListener("mouseleave",()=>{tip.style.opacity="0";});
  });
}

/* ---------- tables ---------- */
const TABLE_COLS=[
  ["FPS","fps",""],["Period","grab_period_ms","muted"],["Grab","grab_time_ms","muted"],["grab()","grab_processing_ms","muted"],
  ["Retr. Img","retrieve_image_ms","muted"],["Retr. Meas","retrieve_measure_ms","muted"],
  ["Retr. Bodies","retrieve_bodies_ms","muted"],["Retr. Objects","retrieve_objects_ms","muted"],
  ["Get Pos","get_position_ms","muted"],["G2I","g2i_latency_ms","hi"],["G2G","g2g_latency_ms","hi"],
];
function resPill(group,color){
  return `<span class="res-pill" style="background:${color}22;color:${color}">${esc(group.toUpperCase())}</span>`;
}
function renderTables(){
  const host=document.getElementById("tables");
  host.innerHTML=DATA.sections.map((s,si)=>{
    const allCams=s.benchmarks.flatMap(c=>c.cameras);
    const cols=TABLE_COLS.filter(([,k])=>allCams.some(cam=>cam[k]!=null));
    const loadCols=[];
    if(allCams.some(cam=>cam.cpu_load)) loadCols.push(["CPU %","cpu_load"]);
    if(allCams.some(cam=>cam.gpu_load)) loadCols.push(["GPU %","gpu_load"]);
    const head=`<tr><th class="l">Benchmark</th><th class="l">${s.key==="multi_process"?"Process":"Camera"}</th>`+
      cols.map(([h])=>`<th>${esc(h)}</th>`).join("")+
      loadCols.map(([h])=>`<th>${esc(h)}</th>`).join("")+`</tr>`;
    let body="";
    s.benchmarks.forEach(c=>{
      c.cameras.forEach((cam,j)=>{
        const first=j===0;
        body+=`<tr class="${first?"grp-first":""}">`;
        body+=`<td class="l">${first?resPill(c.group,c.color)+`<span class="bid">${esc(c.bid)}</span>`:""}</td>`;
        body+=`<td class="l"><span class="sn">${esc(cam.serial||"-")}</span></td>`;
        cols.forEach(([,k,cls])=>{ body+=`<td class="${cls}">${fmt(cam[k])}</td>`; });
        loadCols.forEach(([,k])=>{
          const v=cam[k];
          body+=`<td class="muted">${v?`${fmt(v.avg)} / ${fmt(v.peak)}`:"–"}</td>`;
        });
        body+=`</tr>`;
      });
    });
    return `<details class="tbl"${si===0?" open":""}>
      <summary><span class="summary-chev"></span>
        <span class="s-title">${String(si+1).padStart(2,"0")} · ${esc(s.title)}</span>
        <span class="s-rig">${esc(s.rig)} · ${esc(s.mode.toLowerCase())}</span>
      </summary>
      <div class="tbl-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>
    </details>`;
  }).join("");
}

/* ---------- controls ---------- */
function wireSeg(id, apply){
  const seg=document.getElementById(id);
  seg.addEventListener("click",e=>{
    const b=e.target.closest("button"); if(!b)return;
    [...seg.children].forEach(x=>x.setAttribute("aria-pressed", String(x===b)));
    apply(b.dataset.v);
    renderSections();
  });
}
wireSeg("seg-sync", v=>{ MODE.sync = v==="sync"; });
// Clicking a latency legend entry hides (or shows again) that stage in every latency chart.
document.getElementById("sections").addEventListener("click",e=>{
  const b=e.target.closest("button[data-seg]"); if(!b) return;
  const k=b.dataset.seg;
  if(HIDDEN.has(k)) HIDDEN.delete(k); else HIDDEN.add(k);
  renderSections();
  const again=document.querySelector(`button[data-seg="${k}"]`); if(again) again.focus();
});
wireSeg("seg-agg",  v=>{ MODE.agg  = v==="worst" ? "worst" : "mean"; });

renderSections();
renderTables();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ── Latency tab (per-module / per-camera pipeline latency, fully data-driven) ──

_LAT_STYLE = """
<style>
.tabbar{display:flex;gap:6px;margin:0 0 20px}
.tabbtn{padding:9px 22px;border-radius:8px 8px 0 0;cursor:pointer;font-weight:600;
  background:transparent;border:1px solid var(--border,#334155);color:var(--mut,#94a3b8)}
.tabbtn.active{background:var(--accent,#3b82f6);color:#fff;border-color:var(--accent,#3b82f6)}
.tabpage{display:none}.tabpage.active{display:block}
.lat-defn{background:rgba(59,130,246,.08);border:1px solid var(--border,#334155);border-radius:8px;
  padding:12px 16px;margin-bottom:18px}
.lat-lane{margin:10px 0 16px}.lat-laneh{font-weight:600;margin-bottom:6px}
.lat-ctx{opacity:.6;font-weight:400;font-size:.85em}
.lat-flow{display:flex;align-items:center;flex-wrap:wrap;gap:4px}
.lat-node{border:1px solid var(--border,#334155);border-radius:10px;padding:8px 12px;min-width:92px;text-align:center}
.lat-ref{border-color:var(--accent,#3b82f6);box-shadow:0 0 0 1px var(--accent,#3b82f6) inset}
.lat-nname{font-weight:600}.lat-nstat{color:#38bdf8;font-variant-numeric:tabular-nums;margin-top:3px}
.lat-p99{display:block;opacity:.6;font-size:.8em}.lat-arrow{font-size:1.3em;opacity:.5;padding:0 2px}
tr.lat-grp td{font-weight:600;background:rgba(255,255,255,.05)}
.lat-note{color:#fbbf24;font-weight:600;font-size:.85em}
.lat-top{font-size:.6em;opacity:.6;margin-left:10px}
html{scroll-behavior:smooth}
#tab-lat a{color:var(--accent,#3b82f6);text-decoration:none}
#tab-lat .card{scroll-margin-top:70px}
</style>
"""

_LAT_TABBAR = ('<div class="tabbar">'
               '<div class="tabbtn active" onclick="showTab(\'tab-bench\',this)">Benchmarks</div>'
               '<div class="tabbtn" onclick="showTab(\'tab-lat\',this)">Latency</div></div>')


def _lat_stage_label(m: str) -> str:
    if '::' in m:
        return m.split('::')[-1]
    if ']' in m:
        return m.split(']')[-1].strip() or m
    return m


def _lat_branch(ctx: str) -> str:
    if ctx.startswith('SN'):
        return 'Camera consumer (capture → grab → retrieve)'
    if ctx.startswith('EncodingPipe'):
        return 'Recording / encoder (parallel)'
    if 'UDP' in ctx or 'Stream' in ctx:
        return 'Streaming (parallel)'
    return ctx


def _lat_normalize(pm: Dict) -> Dict:
    """perf-monitor JSON -> {context: {branch, reference_module, stages[ordered]}}.

    Each context's stages are re-anchored so the earliest stage sits at 0. The SDK
    picks ONE reference module per context; for a parallel pipeline such as the
    recording encoder (its own ``EncodingPipe`` context), that reference may not be
    the true first stage, producing NEGATIVE deltas for stages that actually run
    earlier. Shifting every stage by the lane minimum removes the negatives while
    preserving the real per-stage ordering (latency = time from each lane's 1st stage).
    """
    out = {}
    for ctx, items in (pm.get('latency_stats') or {}).items():
        if not items:
            continue
        ref = items[0].get('from_module')
        stages = [{'stage': _lat_stage_label(it.get('to_module', '?')), 'module': it.get('to_module', '?'),
                   'mean': it.get('mean', 0.0), 'p99': it.get('p99', 0.0), 'count': it.get('count', 0)}
                  for it in items]
        # The reference module itself is latency 0 in the SDK's frame; add it as a node.
        if ref:
            stages.append({'stage': _lat_stage_label(ref), 'module': ref, 'mean': 0.0, 'p99': 0.0,
                           'count': max((s['count'] for s in stages), default=0)})
        # Re-anchor to the earliest stage (fixes negative encoder-lane deltas).
        shift = min((s['mean'] for s in stages), default=0.0)
        if shift < 0:
            for s in stages:
                s['mean'] -= shift
                s['p99'] = max(0.0, s['p99'] - shift)
        stages.sort(key=lambda s: s['mean'])
        if stages:
            stages[0]['reference'] = True
        out[ctx] = {'reference_module': stages[0]['module'] if stages else ref,
                    'branch': _lat_branch(ctx), 'stages': stages, 'lane_note': ''}
    return out


def _lat_diagram(cb: Dict) -> str:
    lanes = ''
    for ctx, blk in sorted(cb.items(), key=lambda kv: (not kv[0].startswith('SN'), kv[0])):
        nodes = ''
        for i, st in enumerate(blk['stages']):
            arrow = '' if i == 0 else '<div class="lat-arrow">&#8594;</div>'
            refc = ' lat-ref' if st.get('reference') else ''
            stat = ('ref (t=0)' if st.get('reference')
                    else f"{st['mean']:.2f} ms<span class='lat-p99'>p99 {st['p99']:.2f}</span>")
            nodes += (f'{arrow}<div class="lat-node{refc}"><div class="lat-nname">{_esc(st["stage"])}</div>'
                      f'<div class="lat-nstat">{stat}</div></div>')
        note = blk.get('lane_note', '')
        note_html = f' <span class="lat-note">{_esc(note)}</span>' if note else ''
        lanes += (f'<div class="lat-lane"><div class="lat-laneh">{_esc(blk["branch"])} '
                  f'<span class="lat-ctx">[{_esc(ctx)}]</span>{note_html}</div>'
                  f'<div class="lat-flow">{nodes}</div></div>')
    return f'<div class="lat-diagram">{lanes}</div>'


def _lat_table(cb: Dict) -> str:
    rows = ''
    for ctx, blk in sorted(cb.items()):
        rows += f'<tr class="lat-grp"><td colspan="5">{_esc(blk["branch"])} &mdash; {_esc(ctx)}</td></tr>'
        for st in blk['stages']:
            rows += (f'<tr><td>{_esc(st["stage"])}</td><td class="num">{st["mean"]:.3f}</td>'
                     f'<td class="num">{st["p99"]:.3f}</td><td class="num">{st["count"]}</td>'
                     f'<td>{_esc(st["module"])}</td></tr>')
    return ('<table class="data"><thead><tr><th>Stage</th><th>mean (ms)</th><th>p99 (ms)</th>'
            f'<th>samples</th><th>module</th></tr></thead><tbody>{rows}</tbody></table>')


def _html_latency_tab(latency_dir: Optional[str]) -> str:
    """Build the Latency tab: a summary table (TOC) that links to per-config detail
    cards (pipeline diagram + per-module/per-camera table). Drop-frame % comes from
    the already-measured grab frequency / image_drop_rate, nothing recomputed."""
    if not latency_dir or not os.path.isdir(latency_dir):
        return ''
    import glob as _glob
    configs = []
    idx = 0
    for lf in sorted(_glob.glob(os.path.join(latency_dir, 'results_*_latency.json'))):
        base = lf[:-len('_latency.json')]
        label = os.path.basename(base)
        rj = {}
        try:
            with open(base + '.json') as f:
                rj = json.load(f)
            label = rj.get('SequenceID', label)
        except Exception:
            pass
        try:
            with open(lf) as f:
                cb = _lat_normalize(json.load(f))
        except Exception:
            continue
        if not cb:
            continue
        idx += 1
        # Annotate camera lanes with achieved FPS + frame-drop% (already-measured fields).
        raw = rj.get('BenchmarkRawData') or {}
        try:
            req = float(rj.get('Parameters', {}).get('Sequence_parameters', {}).get('requested_frame_rate', 0) or 0)
        except Exception:
            req = 0.0
        cap_lat, worst_drop, min_fps = 0.0, None, None
        for ctx, blk in cb.items():
            lane_max = max((s['mean'] for s in blk['stages']), default=0.0)
            if ctx.startswith('SN'):
                cap_lat = max(cap_lat, lane_max)
                gk = raw.get(f'Grab_SN{ctx[2:]}') or raw.get(f'Grab_{ctx[2:]}') or {}
                fps = gk.get('grab_call_frequency_hz') if isinstance(gk, dict) else None
                if fps is not None:
                    drop = max(0.0, (1 - fps / req) * 100.0) if req > 0 else 0.0
                    blk['lane_note'] = f'achieved {fps:.1f} fps · frame drop {drop:.1f}%'
                    worst_drop = drop if worst_drop is None else max(worst_drop, drop)
                    min_fps = fps if min_fps is None else min(min_fps, fps)
        configs.append({'idx': idx, 'label': label, 'cb': cb,
                        'cap_lat': cap_lat, 'fps': min_fps, 'drop': worst_drop})
    if not configs:
        return ''

    # Summary table (table of contents): click a config to jump to its diagram.
    srows = ''
    for c in configs:
        fps = f"{c['fps']:.1f}" if c['fps'] is not None else '-'
        drop = f"{c['drop']:.1f}%" if c['drop'] is not None else '-'
        srows += (f'<tr><td><a href="#lat-{c["idx"]}">{_esc(c["label"])}</a></td>'
                  f'<td class="num">{c["cap_lat"]:.2f}</td><td class="num">{fps}</td>'
                  f'<td class="num">{drop}</td></tr>')
    summary = ('<h3>Summary <span style="opacity:.6;font-weight:400">(click a config for its pipeline diagram)</span></h3>'
               '<table class="data"><thead><tr><th>Config</th><th>capture→image (ms)</th>'
               f'<th>achieved fps</th><th>frame drop</th></tr></thead><tbody>{srows}</tbody></table>')

    # Detail cards (anchored, with a back-to-top link).
    cards = ''
    for c in configs:
        cards += (f'<div class="card" id="lat-{c["idx"]}"><h3>{_esc(c["label"])} '
                  f'<a href="#lat-top" class="lat-top">↑ summary</a></h3>'
                  f'<h4>Pipeline (latency at each stage output, per parallel lane)</h4>{_lat_diagram(c["cb"])}'
                  f'<h4>Per-module / per-camera table</h4>{_lat_table(c["cb"])}</div>')

    defn = ('<b>What is measured:</b> per-frame latency of each pipeline stage, measured from its lane\'s first '
            'stage (<code>value = stage_ts − first_stage_ts</code>). '
            '<b>Lanes are parallel pipelines:</b> the <em>Camera consumer</em> lane (capture → grab → retrieve) and, '
            'when recording/streaming, a separate <em>Encoder/Streaming</em> lane (its own context) that runs '
            '<em>concurrently</em> on the same frames, so it is shown side-by-side, not in series. Lanes are '
            're-anchored to their first stage (the recording encoder reports its own reference, which would '
            'otherwise yield negative deltas). Camera lanes also show achieved FPS and frame-drop% vs the requested '
            'rate. Stages, ordering and lanes are all derived from the reported JSON.')
    return (f'<div id="lat-top"></div><div class="lat-defn">{defn}</div>'
            f'{summary}<h3 style="margin-top:28px">Details</h3>{cards}')


def _html_failed_section(results_dir: Optional[str]) -> str:
    """List hard-failed configs (stub results with no metrics) so every config is
    reported even when it fails, with its error message for debugging."""
    if not results_dir or not os.path.isdir(results_dir):
        return ''
    import glob as _glob
    rows = ''
    for rf in sorted(_glob.glob(os.path.join(results_dir, 'results_*.json'))):
        if rf.endswith('_latency.json'):
            continue
        try:
            with open(rf) as f:
                d = json.load(f)
        except Exception:
            continue
        raw = d.get('BenchmarkRawData') or {}
        exit_err = raw.get('_exit_error', '') if isinstance(raw, dict) else ''
        perf = d.get('PerformanceResults', {})
        stub_err = ''
        test_pass = None
        for v in perf.values():
            if isinstance(v, dict):
                if v.get('error') and not stub_err:
                    stub_err = v['error']
                if 'test_pass' in v:
                    test_pass = v.get('test_pass')
        has_raw = bool(raw)
        # A clean pass (data produced, no crash, validation OK) is not listed here.
        if has_raw and not exit_err and test_pass is not False:
            continue
        if exit_err:
            status, err = 'crashed (metrics kept)', exit_err
        elif not has_raw:
            status, err = 'no data', (stub_err or 'failed before producing metrics')
        else:
            status, err = 'validation failed', (stub_err or 'did not meet thresholds (see table above)')
        sid = d.get('SequenceID', os.path.basename(rf))
        p = d.get('Parameters', {}).get('Sequence_parameters', {})
        cfg = (f"{p.get('number_stereo_cameras', '?')}x stereo / {p.get('number_mono_cameras', 0)}x mono, "
               f"{p.get('resolution', '?')}@{int(p.get('requested_frame_rate', 0) or 0)}fps, "
               f"depth={p.get('depth_mode', '?')}")
        rows += (f'<tr><td>{_esc(sid)}</td><td>{_esc(cfg)}</td><td>{_esc(status)}</td>'
                 f'<td style="color:#f87171">{_esc(err)}</td></tr>')
    if not rows:
        return ''
    return ('<div class="section"><h2>Failed / Incomplete Configs</h2>'
            '<p style="opacity:.7">Every config that did not cleanly pass, for debugging. '
            '"crashed (metrics kept)" configs also appear with full metrics in the tables/Latency tab above.</p>'
            '<table class="data"><thead><tr><th>Config (SequenceID)</th><th>Parameters</th>'
            f'<th>Status</th><th>Error</th></tr></thead><tbody>{rows}</tbody></table></div>')


def generate_html_report(input_dir: str, output_file: str, config: Optional[Dict] = None,
                         latency_dir: Optional[str] = None) -> None:
    """Generate a self-contained HTML benchmark report.

    latency_dir: the test_results directory (with results_<id>.json + results_<id>_latency.json);
    when provided, adds a clickable Latency tab and a Failed-configs section.
    """
    results = pr.load_json_results(input_dir)
    if not results:
        print(f"No results found in {input_dir}")
        return

    load = pr.load_load_report(input_dir)
    generated = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data = _build_data(results, load, config, generated)

    # `</` must not appear verbatim inside <script>/<pre> content
    data_json = json.dumps(data).replace('</', '<\\/')
    raw_json = _esc(json.dumps(results, indent=2))

    html = (_HTML_TEMPLATE
            .replace('__TITLE__', _esc('ZED SDK Benchmark Report'))
            .replace('__DATA__', data_json)
            .replace('__RAW__', raw_json))

    with open(output_file, 'w') as f:
        f.write(html)

    print(f"HTML report written to: {output_file}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='ZED SDK Benchmark HTML Report Generator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    python scripts/generate_html_report.py --input benchmark_output/ --output report.html
"""
    )
    parser.add_argument('--input', '-i', required=True, help='Input directory containing JSON result files')
    parser.add_argument('--output', '-o', required=True, help='Output HTML file')
    args = parser.parse_args()

    generate_html_report(args.input, args.output)


if __name__ == '__main__':
    main()
