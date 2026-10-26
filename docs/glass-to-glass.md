# Glass-to-glass latency

`--glass_to_glass_latency` measures full end-to-end latency (display + optics + sensor + SDK):
a superset of glass-to-image latency. The benchmark opens a fullscreen window that renders the
host `CLOCK_REALTIME` time, in milliseconds, as a binary, CRC-checked block of AprilTag markers: the
value is packed into 6 big-endian payload bytes plus a 2-byte CRC-16/CCITT, and each byte is shown as
a marker whose id is the byte value (0–255), drawn as several identical rows. The readable decimal
time is drawn beneath the block. The screen is repainted ~every millisecond, so the displayed time is
always current. The camera films the screen and each grabbed frame is decoded back to a millisecond
timestamp. Latency = camera capture time − decoded displayed time, written as
`GlassToGlassLatency_SN<serial>` (same schema as `GlassToImageLatency`). For multi-camera, every
camera films the same screen and gets its own per-camera entry.

AprilTag markers are self-registering and error-correcting, so the read tolerates perspective,
rotation, blur and low contrast. Each byte is shown as a **column** of identical markers (one per
row): if a frame's rolling-shutter readout straddles a timestamp change, the rows above the tear show
the old value and the rows below show the new one, so the changed byte's column disagrees, and the
reassembled bytes fail the CRC as a backstop, so the decoder **drops the frame** rather than
reconstructing a mixed timestamp. A decode is accepted only when every marker in each column agrees
**and** the CRC checks out. The on-screen border shows status: **green** = LOCKED (live latency in ms
is shown), **red** = NO SIGNAL. Every camera must see the full marker block (8 columns).

Statistics are computed over the **whole** set of decoded samples: a frame only produces a sample
after it passes per-column agreement, the CRC and the plausibility check (see below), so
mis-detections are rejected at decode time and need no statistical tail-trim. The encoding/decoding
scheme (dictionary + binary/CRC layout) is **identical** on both sides
(`cpp/src/glass_to_glass.cpp` / `python/glass_to_glass.py`).

> This document describes the language-agnostic G2G scheme, setup and troubleshooting. For the
> dependency install and the exact command, see the [Python](../python/README.md#glass-to-glass-latency)
> and [C++](../cpp/README.md#glass-to-glass-latency) READMEs. In the examples below, `<benchmark>`
> stands for `python3 benchmark_single_cam.py` (Python) or `./build/benchmark_single_cam` (C++).

## Setup for accurate numbers

- Dedicated monitor, fullscreen, **no compositor** (a compositing desktop adds ~1 frame). The
  benchmark repaints at ~1 kHz (vsync off) and re-samples the clock each frame, so the encoded
  millisecond is always current, but the panel still emits photons only at its own refresh rate,
  which bounds the achievable resolution (a 120/144 Hz screen helps). Use a short, **fixed** camera
  exposure / a bright screen so a capture cleanly reads one frame of the marker block.
- Put the Jetson at max performance first: `sudo nvpmodel -m 0 && sudo jetson_clocks`.
- Clock alignment: the on-screen time is the display host's `CLOCK_REALTIME` and the benchmark
  compares it against the SDK `CURRENT` timestamp, so drive the screen and run the benchmark on the
  **same host**. Sanity check: G2G should be ≥ glass-to-image for the same setup.
- Known fixed bias: the displayed time is encoded at **millisecond** granularity (the sub-ms part is
  truncated), so every reported latency is biased high by up to ~1 ms (≈0.5 ms on average): the same
  order as the ~1 ms repaint. The C++ and Python decoders truncate identically, so the two agree; just
  treat absolute numbers as accurate to ~1 ms.

```bash
<benchmark> --resolution HD1200 --camera_fps 60 --glass_to_glass_latency
```

## How the timestamp is encoded and decoded

The display encodes the host `CLOCK_REALTIME` millisecond into a block of AprilTag markers; the
decoder reconstructs it and rejects anything it cannot fully trust.

**On-screen layout**: `TAG_COUNT` (8) marker columns, each repeated over `TIMESTAMP_ROWS` (5)
identical rows, with the readable decimal time below and a status border around everything:

```
status border around the window:  green = LOCKED   /   red = NO SIGNAL
top-left readout:  "Last detection: 12.3 ms   number of detections: 1843"

machine-read marker block: TAG_COUNT = 8 columns × TIMESTAMP_ROWS = 5 rows:

      col   0    1    2    3    4    5    6    7
          [p0] [p1] [p2] [p3] [p4] [p5] [c0] [c1]   ┐
          [p0] [p1] [p2] [p3] [p4] [p5] [c0] [c1]   │
          [p0] [p1] [p2] [p3] [p4] [p5] [c0] [c1]   │  5 identical rows
          [p0] [p1] [p2] [p3] [p4] [p5] [c0] [c1]   │  (one byte id per cell)
          [p0] [p1] [p2] [p3] [p4] [p5] [c0] [c1]   ┘
          └──────── 6 payload bytes ───────┘ └ 2 CRC ┘

human-readable decimal below the block (verification only, NOT decoded, no CRC):

          1 7 4 8 9 2 3 0 5 1 2 8 6      13 digits of the same millisecond
```

**Encoding**: epoch-ms → 6 big-endian payload bytes + 2 CRC bytes → 8 marker ids:

```
host CLOCK_REALTIME → epoch milliseconds (uint64)        e.g. 1748923051268
        │
        ▼  low 48 bits, big-endian (MSB first)
   payload = [ p0 p1 p2 p3 p4 p5 ]
        │
        ▼  crc = CRC-16/CCITT-FALSE(payload)   poly 0x1021, init 0xFFFF
   crc bytes = [ c0 = crc>>8 , c1 = crc & 0xFF ]
        │
        ▼  marker id = byte value (0..255), AprilTag 36h11
   8 markers, left → right:  [p0][p1][p2][p3][p4][p5][c0][c1]
   each drawn as 5 identical rows → 8 vertical columns
```

**Decoding & checks**: a frame is accepted only if *every* check passes; otherwise it is dropped
(and, with `G2G_DEBUG`, the reason is recorded). The decoder never reconstructs a partial or mixed
timestamp:

```
camera frame (BGRA)
   │ → gray, downscale so max(w,h) ≤ DECODE_MAX_DIM (1280)
   ▼
detectMarkers (DICT_APRILTAG_36h11)
   │ keep ids 0..255 with 4 corners ──── none ─────────► reject: no byte markers
   ▼ sort by image x; new column when x-gap > 0.6 × median marker width
   │ two markers in a column read different bytes ─────► reject: column disagreement (X vs Y)
   │ a column kept < MIN_COL_MARKERS (3 of 5) rows ────► reject: sparse column (got only N …)
   ▼
   columns == TAG_COUNT (8)? ──── no ─────────────────► reject: wrong column count (N != 8)
   ▼
   CRC-16(payload) == received CRC? ──── no ──────────► reject: CRC mismatch
   ▼
   plausible vs reference "now"? ──── no ─────────────► reject: implausible time
   │     reject if  ms > now + 1000 ms  •  now − ms > 10 000 ms  (a 0/garbage ms is caught by the latter)
   ▼
   LOCKED → latency = reference_now − decoded_display_time
```

The two redundancies work together: **per-column agreement** + `MIN_COL_MARKERS` catch a
rolling-shutter tear straddling a millisecond change (old value in the rows above the tear, new
value below ⇒ the changed byte's column disagrees), and the **CRC** is the backstop for a byte
misread that slipped past AprilTag's per-tag error correction, or a tear that happened to leave
every column internally consistent.

| Constant | Value | Meaning |
|---|---|---|
| Dictionary | `DICT_APRILTAG_36h11` | AprilTag family; ids 0–255 used |
| `PAYLOAD_BYTES` | 6 | big-endian epoch-ms payload (48 bits, good past year 10000) |
| `CRC_BYTES` | 2 | CRC-16/CCITT-FALSE over the payload |
| `TAG_COUNT` | 8 | marker columns (6 payload + 2 CRC) |
| `TIMESTAMP_ROWS` | 5 | identical rows per byte (vertical redundancy) |
| `MIN_COL_MARKERS` | 3 | minimum rows a column must keep to be trusted |
| `TIMESTAMP_DIGITS` | 13 | readable decimal digits (human only) |
| `DECODE_MAX_DIM` | 1280 | longest image side the decoder runs at |
| CRC params | poly `0x1021`, init `0xFFFF`, no reflection, no final XOR | must match bit-for-bit on both sides |

## Debugging locks (`G2G_DEBUG`)

Set `G2G_DEBUG=<dir>` to diagnose why frames aren't locking. For every decoded frame the decoder:

- prints `[G2G] detected ids: <id id …>  -> <outcome>` (the outcome is `LOCKED` or the reject reason), and
- dumps an annotated PNG `g2g_aruco_000.png … g2g_aruco_119.png` (first 120 frames) into `<dir>`.

```bash
mkdir -p /tmp/g2g
G2G_DEBUG=/tmp/g2g <benchmark> --resolution HD1200 --camera_fps 60 --glass_to_glass_latency
```

Each PNG shows the detected markers plus an overlay:

- **green** `LOCKED <ms> (HH:MM:SS.mmm)` and `host-now lead: <d> ms`, the lead is ≈ the
  glass-to-glass latency for that frame;
- **red** `REJECT: <reason>` and, once a value was reconstructed, `decoded <ms> (HH:MM:SS.mmm)`;
- **yellow** `cols: <column values>  markers:<N>`, the reconstructed column bytes and the total
  detected-marker count;
- **red rings** around exactly what failed: the disagreeing column and the conflicting marker, a
  sparse column, or the whole block for a CRC / count / plausibility failure.

| Reject note | What it means | Typical cause |
|---|---|---|
| `no byte markers` | nothing detected, or no detected id was a 0–255 byte marker with 4 corners | screen not (fully) framed, markers too small, severe blur/glare |
| `column disagreement (X vs Y)` | two markers in one column read different bytes | rolling-shutter tear across a ms change (rare, expected), or a misread |
| `sparse column (got only N …)` | a column kept fewer than `MIN_COL_MARKERS` of its 5 rows | rows lost to blur, glare, low contrast or partial occlusion |
| `wrong column count (N != 8)` | not exactly 8 columns reconstructed | part of the block off-frame, or glare splitting/merging columns |
| `CRC mismatch` | 8 consistent columns but the CRC failed | a byte misread past per-tag correction, or a self-consistent tear |
| `implausible time` | decoded ms in the future (> now + 1 s) or > 10 s old | wrong screen filmed, host-clock skew, or a foreign/stale SVO (the decoder counts these and the async decoder reports the total at stop) |

## Troubleshooting

The decoder is conservative on purpose (it would rather drop a frame than report a wrong latency)
so a few rejects per second are normal. Persistent failure or surprising numbers usually trace to
the physical setup:

| Symptom | Likely cause | Fix |
|---|---|---|
| Border never turns green / no detections | the full 8-column block isn't in frame, markers too small, out of focus | reframe so the whole block fills a good part of the image; focus; check all 8 columns are visible |
| Frequent `sparse column` / `wrong column count` | motion blur, long/auto exposure, glare, dim screen | short **fixed** exposure; raise screen brightness; remove reflections; disable auto-exposure |
| Latency higher than expected | desktop **compositor** adds ≥1 frame; vsync; low refresh | run fullscreen with **no compositor**; the 1 kHz repaint removes encode staleness, not panel latency |
| Latency looks quantized / coarse | **screen refresh rate** too low: a 60 Hz panel emits a new image only every ~16.7 ms | use a 120/144 Hz (or faster) panel; the panel emits photons only at its refresh rate, which bounds the resolution |
| G2G < G2I (impossible: G2G is a superset) | the display host and the camera host have different clocks | drive the screen and run the benchmark on the **same host** (the on-screen time is the display host's `CLOCK_REALTIME`); sanity-check G2G ≥ G2I |
| `implausible time` on a multi-host rig | the display host and the camera host have different clocks | drive the screen and run the benchmark on the **same host**, or sync the clocks (the on-screen time is the display host's `CLOCK_REALTIME`) |

> **Why the refresh rate matters.** The benchmark repaints at ~1 kHz with vsync off, so the
> *encoded* millisecond is at most ~1 ms stale. But the panel still turns pixels into photons only
> at its own refresh rate, so a 60 Hz screen quantizes the displayed time into ~16.7 ms steps no
> matter how often the value is re-sampled. A higher-refresh panel (120/144 Hz) directly lowers
> that floor. Screen tearing from vsync-off is **intended** and harmless here: the column
> redundancy + CRC reject any frame whose tear straddles a millisecond change.
