"""Glass-to-glass (G2G) latency support for the ZED Python benchmarks.

G2G measures full end-to-end latency (display + optics + sensor + SDK) by closing the
loop through a physical screen. The display renders the host CLOCK_REALTIME time, in
milliseconds, as a binary, CRC-checked block of AprilTag markers: the value is packed into
6 big-endian payload bytes followed by a 2-byte CRC-16/CCITT, and each byte is shown as a
marker whose id is the byte value (0..255). Each byte is drawn as several identical rows, so
the markers stack into 8 vertical columns, with the readable decimal time drawn beneath the
block for human verification. The camera films the screen; each grabbed frame is decoded back
to a millisecond timestamp (markers grouped into columns left-to-right by image x, bytes
reassembled, CRC verified) and compared against the SDK IMAGE capture time. See
python/README.md ("Glass-to-glass latency").

AprilTag markers make the read robust: markers are self-registering and error-correcting, so
the read tolerates perspective, rotation, blur and low contrast. Each byte is a column of
identical markers, so a screen tear (or rolling-shutter readout) straddling a timestamp change
shows the old value above the tear and the new value below it; the changed byte's column then
disagrees (and the CRC fails as a backstop) and the frame is dropped: it can never yield a
mixed timestamp. A decode is accepted only when every marker in each column agrees and the CRC
checks out.

The marker scheme (dictionary + binary/CRC layout) is kept identical to
cpp/src/glass_to_glass.cpp.

GL (glfw + PyOpenGL) and OpenCV are imported lazily; if any is missing, ``G2G_AVAILABLE``
is False and callers warn-and-continue instead of crashing.
"""
import os
import threading
import time

# ── Optional dependencies ────────────────────────────────────────────────────
try:
    import glfw  # type: ignore  # optional dep; not in the CI type-check env (see requirements.txt)
    from OpenGL.GL import (  # type: ignore  # optional dep; PyOpenGL ships no stubs and isn't in the type-check env
        glBegin, glEnd, glVertex2d, glColor3f, glColor4f, glClear, glClearColor, glViewport,
        glMatrixMode, glLoadIdentity, glOrtho, GL_QUADS, GL_PROJECTION, GL_MODELVIEW,
        GL_COLOR_BUFFER_BIT,
        # Textured rendering (markers pre-baked as textures; text drawn as alpha-blended quads)
        glEnable, glDisable, glBlendFunc, glGenTextures, glDeleteTextures, glBindTexture,
        glTexImage2D, glTexParameteri, glTexCoord2f, glPixelStorei,
        GL_BLEND, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_TEXTURE_2D, GL_RGBA,
        GL_UNSIGNED_BYTE, GL_LINEAR, GL_NEAREST, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER,
        GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE, GL_UNPACK_ALIGNMENT,
    )
    _GL_OK = True
except Exception:
    _GL_OK = False

try:
    import cv2
    import numpy as np
    _CV_OK = hasattr(cv2, "aruco")
except Exception:
    _CV_OK = False

G2G_AVAILABLE = _GL_OK and _CV_OK

# Self-contained scoped-profiler hooks (see profiler.py). The async decoder and the display can
# record their own per-section timings through this exactly like the C++ module. Currently dormant
# (no calls), mirroring glass_to_glass.cpp: kept for parity and future use.
from profiler import Profiler  # noqa: F401

# ── Marker scheme (shared by renderer and decoder) ───────────────────────────
# DICT_APRILTAG_36h11 keeps parity with the C++ module. The timestamp is packed into bytes
# (see below), so ids 0..255 are used: all valid 36h11 codes (the family has 587). The 36h11
# family carries strong per-tag error detection/correction; the application CRC on top guards
# byte assembly and tears.
#
# Binary timestamp layout: epoch milliseconds are packed into PAYLOAD_BYTES big-endian bytes
# (48 bits: good well past year 10000), followed by CRC_BYTES of CRC-16/CCITT over those
# payload bytes. Each byte is one marker (id == byte value, 0..255), so the whole timestamp is
# TAG_COUNT markers per row.
PAYLOAD_BYTES = 6
CRC_BYTES = 2
TAG_COUNT = PAYLOAD_BYTES + CRC_BYTES  # markers per row (vertical columns)
# Human-readable decimal width: epoch ms is 13 digits until ~year 2286. Only the readable text
# drawn beneath the markers uses this; the machine read is the binary marker block above.
TIMESTAMP_DIGITS = 13
# Each byte is drawn as TIMESTAMP_ROWS identical rows, so markers stack into TAG_COUNT vertical
# columns. The decoder trusts a column only when every marker in it reads the same byte: a
# misread or a rolling-shutter tear straddling a timestamp change makes the affected column(s)
# disagree, and the frame is dropped before the CRC even runs.
TIMESTAMP_ROWS = 5
# Minimum markers a column must actually carry before it is trusted. The per-column agreement
# check is otherwise vacuous: a column reduced to a single surviving marker "agrees" with itself,
# so the TIMESTAMP_ROWS-fold redundancy silently degrades to one read. Requiring a majority of
# the rows keeps the redundancy real while tolerating a few dropped/blurred markers.
MIN_COL_MARKERS = 3  # 3 of 5
BORDER_BITS = 1
DECODE_MAX_DIM = 1280  # largest image dimension the decoder works at


def _crc16_ccitt(data):
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final xor) over `data`
    bytes. Identical to crc16_ccitt() in glass_to_glass.cpp: both sides MUST agree bit-for-bit."""
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def _encode_timestamp(ms):
    """Pack epoch-ms into the TAG_COUNT marker ids: PAYLOAD_BYTES big-endian payload bytes then
    CRC_BYTES of CRC-16 over them. Mirrored by the decoder (reassemble + verify)."""
    payload = [(ms >> (8 * (PAYLOAD_BYTES - 1 - i))) & 0xFF for i in range(PAYLOAD_BYTES)]
    crc = _crc16_ccitt(payload)
    return payload + [(crc >> 8) & 0xFF, crc & 0xFF]

# G2G_DEBUG=<dir>: print decoded ids and dump annotated detections. None when unset.
_G2G_DEBUG = os.environ.get("G2G_DEBUG")
_dump_n = 0
_dump_lock = threading.Lock()  # guards _dump_n across concurrent Decoder workers

_DICT = None


def _g2g_dict():
    global _DICT
    if _DICT is None:
        _DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    return _DICT


def _marker_cells(marker_id, side):
    """Return the list of (row, col) black cells of the marker bitmap for ``marker_id``."""
    img = cv2.aruco.generateImageMarker(_g2g_dict(), marker_id, side, borderBits=BORDER_BITS)
    return [(r, c) for r in range(side) for c in range(side) if img[r, c] == 0]


class Status:
    NO_SIGNAL = 0
    LOCKED = 1


def realtime_now_ns() -> int:
    """Host CLOCK_REALTIME, nanoseconds (the clock the displayed time is sampled from)."""
    return time.time_ns()


# ── Decoder (OpenCV aruco, main module only) ─────────────────────────────────
class Decoder:
    def __init__(self):
        self._detector = cv2.aruco.ArucoDetector(_g2g_dict(), cv2.aruco.DetectorParameters())
        self.implausible_time_count = 0  # decodes rejected by the plausibility window

    def decode(self, sl_mat, reference_now_ns=0):
        """sl_mat: pyzed LEFT/CPU view (BGRA). Returns (Status, present_ns).

        reference_now_ns: the "now" the decoded screen time is sanity-checked against (a decode
        far from it is treated as a misread). 0 (default) uses the host CLOCK_REALTIME: correct
        for live capture. On SVO playback the screen shows the recording time, not the host's
        current time, so callers must pass the frame's reconstructed live "now" instead."""
        if sl_mat is None:
            return (Status.NO_SIGNAL, 0)
        return self.decode_array(sl_mat.get_data(), reference_now_ns)

    def decode_array(self, data, reference_now_ns=0):
        """Decode the on-screen timestamp from a BGRA HxWx4 numpy array (the deep-copied frame
        the async worker owns). See decode() for reference_now_ns semantics."""
        if data is None or data.size == 0:
            return (Status.NO_SIGNAL, 0)
        h, w = data.shape[0], data.shape[1]
        gray = cv2.cvtColor(data, cv2.COLOR_BGRA2GRAY)

        sc = float(DECODE_MAX_DIM) / max(w, h)
        if sc < 1.0:
            gray = cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)

        corners, ids, _ = self._detector.detectMarkers(gray)

        # Debug dump (G2G_DEBUG=<dir>): annotate the actual outcome ON the dumped PNG so it is
        # self-explanatory: decoded timestamp + host delta when locked, or the reject reason
        # (with the offending values) when not. The write is deferred to _finish(), so every
        # return below goes through _finish(...) after setting dbg["note"] (and dbg["ms"] once a
        # value is reconstructed); when G2G_DEBUG is unset _finish() is a no-op pass-through.
        # Mirrors the finish() lambda in glass_to_glass.cpp.
        debug = _G2G_DEBUG
        column_values = []  # left-to-right; declared here so a partial dump can show it
        dbg = {"note": "", "ms": 0, "ref_ms": 0, "locked": False}
        dbg_boxes = []  # regions to ring in red on a failed decode (decode-image coords)

        def _ms_to_clock(ms):
            lt = time.localtime(ms // 1000)
            return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms % 1000:03d}"

        def _finish(result):
            if not debug:
                return result
            # Build the flat id list only when actually dumping: kept off the non-debug hot path
            # (and matches the C++, which only walks `ids` for the print inside finish()).
            flat = [] if ids is None else [int(x) for x in ids.flatten()]
            ids_str = "".join(f" {i}" for i in flat)  # space-separated, no brackets (matches C++)
            print(f"[G2G] detected ids:{ids_str}  -> {dbg['note'] or '?'}")
            global _dump_n
            with _dump_lock:
                if _dump_n >= 120:
                    return result
                n = _dump_n
                _dump_n += 1
            vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            fs = max(0.6, vis.shape[1] / 1280.0)  # font scale tracks image size
            thick = max(2, int(2 * fs + 0.5))
            # Ring whatever the failing path flagged as wrong, in red FIRST, so the marker
            # outlines and the text overlay below draw on top of it and stay legible.
            for (x0, y0, x1, y1) in dbg_boxes:
                cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 0, 255), max(2, int(3 * fs)))
            if flat:
                cv2.aruco.drawDetectedMarkers(vis, corners, ids)
            line = [0]

            def label(s, color):
                org = (12, int((28 + line[0] * 34) * fs))
                cv2.putText(vis, s, org, cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), thick + 3, cv2.LINE_AA)  # outline
                cv2.putText(vis, s, org, cv2.FONT_HERSHEY_SIMPLEX, fs, color, thick, cv2.LINE_AA)
                line[0] += 1

            if dbg["locked"]:
                green = (0, 200, 0)
                d = int(dbg["ref_ms"]) - int(dbg["ms"])
                label(f"LOCKED  {dbg['ms']} ms  ({_ms_to_clock(dbg['ms'])})", green)
                label(f"host-now lead: {d} ms", green)  # ~glass-to-glass latency
            else:
                label(f"REJECT: {dbg['note']}", (0, 0, 255))
                if dbg["ms"]:
                    label(f"decoded {dbg['ms']} ms  ({_ms_to_clock(dbg['ms'])})", (0, 0, 255))
            cols = "cols:" + "".join(f" {v}" for v in column_values)
            cols += f"   markers:{len(flat)}"
            label(cols, (0, 220, 220))
            cv2.imwrite(f"{debug}/g2g_aruco_{n:03d}.png", vis)
            return result

        # Keep byte markers (id 0..255) with image center, on-image width and bounding box. The
        # timestamp is drawn as TIMESTAMP_ROWS identical rows, so markers stack into vertical
        # columns; the width sets the clustering scale below (robust to perspective). The bbox
        # (decode-image coords) lets a failed decode ring the offending region on the dump.
        markers = []  # (x, y, w, byte, bx0, by0, bx1, by1)
        if ids is not None:
            for i, v in enumerate(ids.flatten()):
                v = int(v)
                pts = corners[i][0]
                if not (0 <= v <= 255) or pts.shape[0] != 4:
                    continue
                xs, ys = pts[:, 0], pts[:, 1]
                minx, maxx = float(xs.min()), float(xs.max())
                miny, maxy = float(ys.min()), float(ys.max())
                markers.append((float(xs.mean()), float(ys.mean()), maxx - minx, v,
                                minx, miny, maxx, maxy))
        if not markers:
            # Nothing usable as a byte marker, ring whatever ArUco *did* find so the dump shows
            # detection happened but every id fell outside 0..255 (or had bad corners).
            if debug:
                for c in corners:
                    pts = c[0]
                    if pts.shape[0] != 4:
                        continue
                    dbg_boxes.append((int(pts[:, 0].min()) - 4, int(pts[:, 1].min()) - 4,
                                      int(pts[:, 0].max()) + 4, int(pts[:, 1].max()) + 4))
            dbg["note"] = "no byte markers"
            return _finish((Status.NO_SIGNAL, 0))

        # Group markers into columns by image x. Within a column markers are vertically aligned
        # (near-equal x); adjacent columns sit a full pitch apart (> one marker width), so an
        # x-gap wider than ~0.6 marker widths starts a new column. The median width is a
        # perspective-robust scale.
        markers.sort(key=lambda m: m[0])
        widths = sorted(m[2] for m in markers)
        col_gap = 0.6 * widths[len(widths) // 2]

        # Bounding box (with a little padding) around markers[a:b]: used to ring a column, a
        # single offending marker, or the whole reconstructed block in red on the debug dump.
        def _col_box(a, b):
            x0, y0 = markers[a][4], markers[a][5]
            x1, y1 = markers[a][6], markers[a][7]
            for k in range(a + 1, b):
                x0, y0 = min(x0, markers[k][4]), min(y0, markers[k][5])
                x1, y1 = max(x1, markers[k][6]), max(y1, markers[k][7])
            return (int(x0) - 4, int(y0) - 4, int(x1) + 4, int(y1) + 4)

        dbg_col_boxes = []  # per-column boxes, kept so a value-level reject can ring the whole block

        # A column is trusted only when every marker in it reads the same byte. A misread, or
        # a rolling-shutter tear straddling a timestamp change (top rows old, bottom rows new),
        # makes the affected column disagree, and the whole frame is dropped, never mixed.
        i, n_m = 0, len(markers)
        while i < n_m:
            v = markers[i][3]
            j = i + 1
            while j < n_m and markers[j][0] - markers[j - 1][0] <= col_gap:
                if markers[j][3] != v:
                    if debug:
                        e = j  # walk to the column end so the whole cluster is ringed
                        while e < n_m and markers[e][0] - markers[e - 1][0] <= col_gap:
                            e += 1
                        dbg_boxes.append(_col_box(i, e))      # the disagreeing column
                        dbg_boxes.append(_col_box(j, j + 1))  # the specific conflicting marker
                    dbg["note"] = f"column disagreement ({v} vs {markers[j][3]})"
                    return _finish((Status.NO_SIGNAL, 0))
                j += 1
            # The agreement above is only meaningful with enough rows: a sparse column (most of
            # its markers dropped, e.g. one side lost to a tear) would otherwise pass trivially.
            # Require a real majority of the rows so a torn column is far more likely to still
            # hold a disagreeing marker and be rejected.
            if (j - i) < MIN_COL_MARKERS:
                if debug:
                    dbg_boxes.append(_col_box(i, j))
                got = j - i
                unit = "markers" if got > 1 else "marker"
                dbg["note"] = f"sparse column (got only {got} {unit} instead of minimum {MIN_COL_MARKERS})"
                return _finish((Status.NO_SIGNAL, 0))
            if debug:
                dbg_col_boxes.append(_col_box(i, j))
            column_values.append(v)
            i = j

        # Require the full timestamp: exactly TAG_COUNT consistent columns (payload + CRC).
        if len(column_values) != TAG_COUNT:
            if debug:
                dbg_boxes = dbg_col_boxes  # ring every column found so the count is visible
            dbg["note"] = f"wrong column count ({len(column_values)} != {TAG_COUNT})"
            return _finish((Status.NO_SIGNAL, 0))

        # Reassemble the bytes and verify the CRC. column_values are the marker ids left-to-right:
        # PAYLOAD_BYTES big-endian payload bytes then CRC_BYTES of CRC-16. A misread that slipped
        # past per-tag 36h11 correction, or a tear that left every column internally consistent,
        # fails the CRC here and the frame is dropped.
        payload = column_values[:PAYLOAD_BYTES]
        crc_read = (column_values[PAYLOAD_BYTES] << 8) | column_values[PAYLOAD_BYTES + 1]
        if _crc16_ccitt(payload) != crc_read:
            if debug:
                dbg_boxes = dbg_col_boxes  # a misread byte we can't localize, ring the whole block
            dbg["note"] = "CRC mismatch"
            return _finish((Status.NO_SIGNAL, 0))

        ms = 0
        for b in payload:
            ms = (ms << 8) | b

        # Plausibility guard: the screen shows ~the reference "now", so a reconstruction far from
        # it is a misread, not a real frame. Live capture uses the host clock (reference_now_ns
        # == 0); SVO playback passes the frame's reconstructed live "now" (the recorded screen
        # time is not the host's current time).
        now_ms = (reference_now_ns if reference_now_ns else realtime_now_ns()) // 1_000_000
        dbg["ms"] = ms
        dbg["ref_ms"] = now_ms
        # ms is a non-negative epoch-ms, so a zero/garbage value is already caught by the >10s-old
        # test (now_ms - ms is huge); no separate "ms <= 0" check is needed.
        if ms > now_ms + 1000 or (now_ms > ms and now_ms - ms > 10_000):
            if debug:
                dbg_boxes = dbg_col_boxes
            dbg["note"] = "implausible time"
            self.implausible_time_count += 1
            return _finish((Status.NO_SIGNAL, 0))

        dbg["locked"] = True
        dbg["note"] = "LOCKED"
        return _finish((Status.LOCKED, ms * 1_000_000))


# ── Async decoder ─────────────────────────────────────────────────────────────
class AsyncDecoder:
    """Runs the (expensive) Decoder on its own worker thread so it never inflates the grab loop
    period. The grab thread submit()s a deep copy of each frame plus the sampled "now" timestamp
    into a SINGLE-SLOT mailbox; if the worker is still busy with the previous frame, the pending
    one is overwritten: dropped and counted, never an unbounded queue. The worker owns the
    Decoder, computes the G2G latency on a LOCKED decode, accumulates it into its own list, and
    feeds the Display its per-frame feedback. Mirrors g2g::AsyncDecoder in glass_to_glass.cpp."""

    def __init__(self):
        self._decoder = Decoder()
        self._display = None
        self._cam_id = 0   # which camera's feedback slot this worker updates on the display
        self._ctx = ""
        self._thread = None
        self._cv = threading.Condition()
        self._stop = False
        self._has_frame = False
        self._mailbox = None      # (numpy_frame, current_ns) the worker has not yet taken
        self._dropped = 0
        self._implausible = 0     # LOCKED decodes with a non-positive latency (bad clock offset)
        self._latencies = []      # worker-owned; no lock on the hot path
        self._started = False

    def start(self, display, profile_context, latency_reserve=0, cam_id=0):
        """Spawn the worker. `display` (may be None) receives per-frame feedback; `profile_context`
        is a scoped-profiler context (camera serial) reserved for future per-section timing; `cam_id`
        is the display feedback slot this worker updates (worker index, see Display.set_cameras)."""
        self._display = display
        self._cam_id = cam_id
        self._ctx = profile_context
        self._stop = False
        self._started = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def submit(self, image, current_ns):
        """Grab-thread entry point. Deep-copies `image` (the SDK reuses it on the next grab) and
        stores it with `current_ns` (the now-timestamp on the display clock) into the single-slot
        mailbox, overwriting and counting any frame the worker has not yet taken. Non-blocking."""
        # Deep-copy outside the lock: get_data() is a view onto the Mat buffer the SDK reuses, so
        # .copy() hands the worker an independent frame. The lock then only swaps the slot.
        data = image.get_data()
        if data is None or data.size == 0:
            return
        frame = data.copy()
        with self._cv:
            if self._has_frame:
                self._dropped += 1  # overwriting a frame the worker never took
            self._mailbox = (frame, current_ns)
            self._has_frame = True
            self._cv.notify()

    def _worker(self):
        while True:
            with self._cv:
                while not self._has_frame and not self._stop:
                    self._cv.wait()
                if not self._has_frame:  # woken to stop with an empty mailbox
                    break
                assert self._mailbox is not None  # set together with _has_frame under the lock
                frame, current_ns = self._mailbox
                self._mailbox = None
                self._has_frame = False
            # A throw from decode_array()/display.update() must not escape the thread function:
            # that would silently kill this camera's G2G stream for the rest of the run. Log it and
            # stop the worker cleanly instead (mirrors the C++ worker_loop try/catch).
            try:
                status, display_ns = self._decoder.decode_array(frame, current_ns)
                lat_ms = 0.0
                if status == Status.LOCKED:
                    lat_ms = (current_ns - display_ns) / 1.0e6
                    # A non-positive latency means the on-screen time decoded AFTER capture, i.e. the
                    # display/camera clock offset is wrong: drop it rather than biasing the stats.
                    if lat_ms > 0.0:
                        self._latencies.append(lat_ms)
                    else:
                        self._implausible += 1
                if self._display is not None:
                    self._display.update(status, lat_ms, self._cam_id)
            except Exception as e:
                print(f"[G2G] async decoder worker stopped on exception: {e}")
                break

    def stop(self):
        """Stop + join the worker (idempotent). Logs how many frames were dropped."""
        if not self._started:
            return
        with self._cv:
            self._stop = True
            self._cv.notify()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._started = False
        msg = f"[G2G] async decoder dropped {self._dropped} frame(s) (worker busy)"
        if self._implausible:
            msg += f"; rejected {self._implausible} sample(s) with non-positive latency (bad clock offset)"
        if self._decoder.implausible_time_count:
            msg += f"; rejected {self._decoder.implausible_time_count} decode(s) with an implausible timestamp (check the clock offset)"
        print(msg)

    def dropped(self):
        """Number of frames dropped because the worker was busy. Final after stop()."""
        return self._dropped

    def take_latencies(self):
        """Move out the accumulated per-frame G2G latencies (ms). Call after stop()."""
        out = self._latencies
        self._latencies = []
        return out


# ── Display (GLFW + OpenGL) ──────────────────────────────────────────────────
def _draw_rect(x, y, w, h):
    glBegin(GL_QUADS)
    glVertex2d(x, y)
    glVertex2d(x + w, y)
    glVertex2d(x + w, y + h)
    glVertex2d(x, y + h)
    glEnd()


def _draw_marker_tex(tex, x0, y0, size):
    """Draw a pre-baked marker texture as one textured quad."""
    glBindTexture(GL_TEXTURE_2D, tex)
    glBegin(GL_QUADS)
    glTexCoord2f(0.0, 0.0)
    glVertex2d(x0, y0)
    glTexCoord2f(1.0, 0.0)
    glVertex2d(x0 + size, y0)
    glTexCoord2f(1.0, 1.0)
    glVertex2d(x0 + size, y0 + size)
    glTexCoord2f(0.0, 1.0)
    glVertex2d(x0, y0 + size)
    glEnd()


class _GlText:
    """Real text rendered via cv2.putText into a transparent RGBA bitmap, uploaded as a GL
    texture and drawn as an alpha-blended quad. Replaces the old 7-segment glyphs so the
    readable digits and latency readout appear as real text (human verification only: the
    machine-read timestamp is the ArUco row). One texture per instance, re-uploaded only
    when the string changes; must be used on the render thread that owns the GL context."""

    def __init__(self):
        self._tex = None
        self._tw = 0
        self._th = 0
        self._cached = None

    def render(self, s):
        if self._tex is not None and s == self._cached:
            return
        self._cached = s
        font, scale, thick = cv2.FONT_HERSHEY_SIMPLEX, 4.0, 8
        (w, h), baseline = cv2.getTextSize(s if s else " ", font, scale, thick)
        pad = thick + 4
        self._tw, self._th = w + 2 * pad, h + baseline + 2 * pad
        canvas = np.zeros((self._th, self._tw, 4), dtype=np.uint8)
        # RGB stay 0 (black); the alpha channel carries the anti-aliased glyph mask.
        cv2.putText(canvas, s, (pad, pad + h), font, scale, (0, 0, 0, 255), thick, cv2.LINE_AA)
        if self._tex is None:
            self._tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self._tex)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, self._tw, self._th, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, canvas)

    def width_for_height(self, h):
        return h * self._tw / self._th if self._th else 0.0

    def draw(self, x, y, h):
        w = self.width_for_height(h)
        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, self._tex)
        glColor4f(1.0, 1.0, 1.0, 1.0)
        glBegin(GL_QUADS)
        glTexCoord2f(0.0, 0.0)
        glVertex2d(x, y)
        glTexCoord2f(1.0, 0.0)
        glVertex2d(x + w, y)
        glTexCoord2f(1.0, 1.0)
        glVertex2d(x + w, y + h)
        glTexCoord2f(0.0, 1.0)
        glVertex2d(x, y + h)
        glEnd()
        glDisable(GL_TEXTURE_2D)

    def destroy(self):
        if self._tex is not None:
            glDeleteTextures([self._tex])
            self._tex = None


class _CamFeedback:
    """Per-camera decode feedback shown on screen. One per camera filming the display; all fields
    are written by that camera's decode thread and read by the render thread under Display._lock.
    Mirrors g2g::CamFeedback in glass_to_glass.cpp."""
    __slots__ = ("lat_ms", "last_success_perf", "success_count", "label")
    last_success_perf: "float | None"  # None = never; a perf_counter() float once locked

    def __init__(self, label=""):
        self.lat_ms = 0.0
        self.last_success_perf = None  # perf_counter() of the most recent LOCKED decode (None = never)
        self.success_count = 0         # running total of successful detections
        self.label = label             # camera serial (or "" when unlabeled)


class Display:
    """Fullscreen GL window on its own thread; renders the millisecond timestamp as a binary,
    CRC-checked block of AprilTag markers (readable decimal below), re-sampled ~1 ms."""

    def __init__(self):
        self._side = _g2g_dict().markerSize + 2 * BORDER_BITS
        self._byte_cells = [_marker_cells(b, self._side) for b in range(256)]  # bitmaps for bytes 0..255
        self._marker_tex = []   # pre-baked GL textures for bytes 0..255 (built on the render thread)
        self._digit_text = [_GlText() for _ in range(10)]  # real-text glyphs for digits 0..9
        self._readout_text = []                            # one real-text readout per camera (render-thread owned)
        # One feedback slot per camera (worker index order). Default to a single unlabeled camera so
        # the single-camera benchmark works without calling set_cameras(); set_cameras() rebuilds it.
        self._cams = [_CamFeedback()]
        self._running = False
        self._lock = threading.Lock()
        self._thread = None
        self._init_done = threading.Event()
        self._init_ok = False

    def start(self) -> bool:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._init_done.wait(timeout=10)
        if not self._init_ok:
            # GL init failed (or timed out): the render thread has exited (or never got going). Clear
            # _running and join so the dead thread and a stale _running flag don't linger (mirrors the
            # C++ Display, which joins + clears running on init failure).
            self._running = False
            if self._thread is not None:
                self._thread.join(timeout=5)
                self._thread = None
        return self._init_ok

    def set_cameras(self, serials):
        """Called once before start() (render thread not yet reading _cams). Rebuild one feedback
        slot per camera, in worker-index order, carrying its serial label. Mirrors
        Display::setCameras() in glass_to_glass.cpp."""
        with self._lock:
            if not serials:
                self._cams = [_CamFeedback()]  # never leave it empty
            else:
                self._cams = [_CamFeedback(sn) for sn in serials]

    def update(self, status, latency_ms=0.0, cam_id=0):
        if status != Status.LOCKED:
            return
        with self._lock:
            if cam_id < 0 or cam_id >= len(self._cams):
                return  # unknown camera id: ignore rather than scribble out of bounds
            c = self._cams[cam_id]
            c.lat_ms = latency_ms
            c.last_success_perf = time.perf_counter()
            c.success_count += 1

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        try:
            if not glfw.init():
                self._init_done.set()
                return
            glfw.window_hint(glfw.DECORATED, glfw.FALSE)
            mon = glfw.get_primary_monitor()
            vm = glfw.get_video_mode(mon) if mon else None
            win_w = vm.size.width if vm else 1280
            win_h = vm.size.height if vm else 720
            win = glfw.create_window(win_w, win_h, "ZED Glass-to-Glass", mon, None)
            if not win:
                glfw.terminate()
                self._init_done.set()
                return
            glfw.make_context_current(win)
            # vsync OFF + ~1 kHz redraw (see loop): the on-screen millisecond is re-sampled
            # and repainted about every 1 ms, so the encoded time is never more than ~1 ms
            # stale. The timestamp is drawn as TIMESTAMP_ROWS identical rows, so a tear (or
            # rolling-shutter readout) straddling a timestamp change leaves the rows above it
            # showing the old value and those below the new one; the changed digit's column
            # disagrees and the frame is dropped: it can never produce a mixed timestamp. The
            # panel still emits photons only at its refresh rate: a 1 ms refresh removes
            # encode staleness, not panel latency.
            glfw.swap_interval(0)
            # Alpha-blend the anti-aliased text and marker textures over the white background.
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            # Pre-bake all 256 byte-marker bitmaps as GL textures (black-opaque cells, transparent
            # background). Each marker is then one textured quad per frame instead of ~side² immediate-
            # mode quads: mirrors upload_marker_texture() in glass_to_glass.cpp.
            for b in range(256):
                rgba = np.zeros((self._side, self._side, 4), dtype=np.uint8)
                for (r, c) in self._byte_cells[b]:
                    rgba[r, c, 3] = 255  # alpha = opaque, RGB stays 0 (black)
                tex = glGenTextures(1)
                glBindTexture(GL_TEXTURE_2D, tex)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, self._side, self._side, 0,
                             GL_RGBA, GL_UNSIGNED_BYTE, rgba)
                self._marker_tex.append(tex)
            # Pre-upload readable-digit glyphs so their per-frame render() is a pure cache hit.
            for d in range(10):
                self._digit_text[d].render(str(d))
            # One readout row per camera (cams is fixed by the time start() runs). Lazily uploaded
            # on first render() per row, like the digits.
            self._readout_text = [_GlText() for _ in self._cams]
            self._init_ok = True
        finally:
            self._init_done.set()
        if not self._init_ok:
            return

        n_tags = TAG_COUNT          # machine-read marker columns
        n_digits = TIMESTAMP_DIGITS  # human-readable decimal digits
        while self._running:
            iter_start = time.perf_counter()
            fbw, fbh = glfw.get_framebuffer_size(win)
            glViewport(0, 0, fbw, fbh)
            glMatrixMode(GL_PROJECTION)
            glLoadIdentity()
            glOrtho(0, fbw, fbh, 0, -1, 1)
            glMatrixMode(GL_MODELVIEW)
            glLoadIdentity()
            glClearColor(1.0, 1.0, 1.0, 1.0)
            glClear(GL_COLOR_BUFFER_BIT)

            # Layout is fixed (TAG_COUNT marker columns up top, TIMESTAMP_DIGITS readable digits
            # below), so it needs no clock sample. TIMESTAMP_ROWS identical rows, one marker per byte
            # per row, with a white quiet zone between markers (horizontally and vertically) and a
            # readable decimal row below the whole block for human verification.
            margin = 0.05 * fbw
            pitch = (fbw - 2.0 * margin) / n_tags          # marker column pitch
            digit_pitch = (fbw - 2.0 * margin) / n_digits  # readable digit pitch
            rows_top = 0.04 * fbh            # top of the marker block
            rows_region = 0.78 * fbh         # vertical band the rows share
            row_pitch = rows_region / TIMESTAMP_ROWS
            # Square markers fit both the horizontal pitch and the per-row vertical band.
            msize = min(pitch * 0.74, row_pitch * 0.74)
            text_y = rows_top + rows_region + msize * 0.22  # below the block
            gh = min(msize * 0.9, fbh * 0.97 - text_y)

            # Draw everything that does NOT depend on the clock first, so the only work between
            # sampling the timestamp and presenting the frame is the marker block plus the small
            # human-only readable-digit quads: the encoded millisecond then matches the emitted
            # photons as closely as the panel allows.
            # Snapshot every camera's feedback under the lock; draw outside it.
            now_perf = time.perf_counter()
            with self._lock:
                snap = [(c.label, c.lat_ms, c.last_success_perf, c.success_count) for c in self._cams]
            # Hold the border green as long as ANY camera locked within the last second; only fall
            # back to red after a full second with no success on any camera. This stops the border
            # flickering green/red when detections lock intermittently from frame to frame.
            any_locked_recently = any(last is not None and (now_perf - last) <= 1.0
                                      for (_, _, last, _) in snap)
            glColor3f(0.0, 0.8, 0.0) if any_locked_recently else glColor3f(0.9, 0.0, 0.0)
            bt = 0.02 * fbh
            _draw_rect(0, 0, fbw, bt)
            _draw_rect(0, fbh - bt, fbw, bt)
            _draw_rect(0, 0, bt, fbh)
            _draw_rect(fbw - bt, 0, bt, fbh)
            # Human-readable readout at the very top edge: all cameras on a single line, drawn side
            # by side, each showing that camera's latest latency once locked plus its running
            # detection count (always shown). With many cameras the line can run past the right edge;
            # that only clips the readout, not the markers below.
            readout_band = rows_top * 0.92  # keep clear of the markers
            glyph_h = readout_band * 0.82   # single row; leave a small gap below
            gap = 0.02 * fbw                # horizontal space between cameras
            rx = 0.04 * fbw
            for ci, (label, lat, last, count) in enumerate(snap):
                tag = f"C{ci}" + (f" [{label}]" if label else "")
                if last is not None:
                    text = f"{tag}: {lat:.1f} ms   detections: {count}"
                else:
                    text = f"{tag}: --   detections: {count}"
                t = self._readout_text[ci]
                t.render(text)
                t.draw(rx, 0.004 * fbh, glyph_h)
                rx += t.width_for_height(glyph_h) + gap

            # Sample the host clock as late as possible, then pack that millisecond into the
            # TAG_COUNT byte markers (payload + CRC) and render them.
            present_ns = realtime_now_ns()
            present_ms = present_ns // 1_000_000
            tags = _encode_timestamp(present_ms)
            # Machine-read marker block: TAG_COUNT columns x TIMESTAMP_ROWS identical rows.
            # Each marker is one textured quad (pre-baked at startup): mirrors the C++ path.
            glEnable(GL_TEXTURE_2D)
            glColor4f(1.0, 1.0, 1.0, 1.0)
            for i in range(n_tags):
                b = tags[i]
                mx = margin + i * pitch + (pitch - msize) * 0.5
                for row in range(TIMESTAMP_ROWS):
                    my = rows_top + row * row_pitch + (row_pitch - msize) * 0.5
                    _draw_marker_tex(self._marker_tex[b], mx, my, msize)
            glDisable(GL_TEXTURE_2D)
            # Readable decimal timestamp beneath the block (human verification only: each glyph is
            # uploaded once on first use and a cache hit thereafter).
            dec = f"{present_ms:0{n_digits}d}"
            for i in range(n_digits):
                d = int(dec[i])
                mx = margin + i * digit_pitch
                t = self._digit_text[d]
                t.render(dec[i])
                t.draw(mx + digit_pitch * 0.5 - t.width_for_height(gh) * 0.5, text_y, gh)

            glfw.swap_buffers(win)
            glfw.poll_events()
            if glfw.window_should_close(win):
                self._running = False

            # Pace to ~1 kHz so the displayed millisecond advances every ~1 ms.
            sleep = 0.001 - (time.perf_counter() - iter_start)
            if sleep > 0:
                time.sleep(sleep)

        for t in self._digit_text:
            t.destroy()
        for t in self._readout_text:
            t.destroy()
        if self._marker_tex:
            glDeleteTextures(self._marker_tex)
            self._marker_tex = []
        glfw.destroy_window(win)
        glfw.terminate()
