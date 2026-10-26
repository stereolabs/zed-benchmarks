#
# profiler.py
# ZED Benchmarks: internal, self-contained scoped-duration profiler.
#
#   start()       stores the current perf_counter timestamp for a (section, context).
#   stop()        compares against it and appends the elapsed time (ms) as a sample.
#   add_sample()  appends an externally measured duration (ms) as a sample, for spans that a
#                 start()/stop() pair cannot bracket (e.g. a latency measured from a camera timestamp).
#   dump_stats()  computes the descriptive stats of each section, then writes them to a file.
#
# Python port of cpp/include/profiler.hpp; depends on nothing from the SDK.
#
# All durations are in milliseconds. The dump format is:
#
#   {
#     "profile_stats": {
#       "<context>": {
#         "<section>": { "mean","median","stddev","min","max","p99","count" }
#       }
#     }
#   }
#
# Usage:
#   Profiler.enable()
#   Profiler.start("section")   # context defaults to "default"
#   ... work ...
#   Profiler.stop("section")
#   Profiler.dump_stats("report.profiler.json")
#
# Thread-safety: all state is guarded by a single lock. The multi-camera benchmark uses one
# thread per camera and a distinct section name per camera, so (section, context) keys never
# collide across threads. A stop() with no matching start() is ignored.
#

import json
import math
import sys
import threading
import time


def _compute_stats(values):
    """Descriptive stats over a non-empty list of durations (ms). Keys match exactly what
    _write_profiler_report reads back. Mirrors detail::compute_stats in profiler.hpp."""
    v = sorted(values)
    n = len(v)

    total = sum(v)
    mean = total / n
    median = (v[n // 2 - 1] + v[n // 2]) / 2.0 if n % 2 == 0 else v[n // 2]

    sq = 0.0
    for x in v:
        sq += (x - mean) * (x - mean)
    stddev = math.sqrt(sq / n)  # population stddev (/n)

    # p99, nearest-rank: smallest value at or above the 99th percentile position.
    idx = int(math.ceil(0.99 * n))
    if idx > 0:
        idx -= 1
    if idx >= n:
        idx = n - 1
    p99 = v[idx]

    return {
        "mean": mean,
        "median": median,
        "stddev": stddev,
        "min": v[0],
        "max": v[-1],
        "p99": p99,
        "count": n,
    }


class Profiler:
    """Self-contained scoped-duration profiler. All methods are classmethods operating on
    process-wide shared state; never instantiated."""

    _enabled = False
    _lock = threading.Lock()
    # In-flight start() timestamps, per thread, keyed by (context, section).
    _inflight = {}
    # Completed durations (ms), grouped by context then section name.
    _samples = {}

    @classmethod
    def enable(cls):
        """Arm recording. Until enable() is called, start()/stop() are no-ops."""
        cls._enabled = True

    @classmethod
    def start(cls, name, context="default"):
        """Begin timing a (name, context) section on the calling thread.
        In-flight starts are keyed per-thread, so two threads may safely use the same
        (name, context) pair concurrently. Note: the C++ profiler keys globally (not
        per-thread): callers must ensure unique (name, context) pairs across threads
        when mixing both profilers in the same benchmark run."""
        if not cls._enabled:
            return
        now = time.perf_counter()  # sampled before the lock
        tid = threading.get_ident()
        with cls._lock:
            cls._inflight.setdefault(tid, {})[(context, name)] = now

    @classmethod
    def stop(cls, name, context="default"):
        """End timing a section started with start() on the same thread. A stop() with no
        matching start() is silently ignored."""
        if not cls._enabled:
            return
        now = time.perf_counter()  # sampled before the lock
        tid = threading.get_ident()
        with cls._lock:
            thread_inflight = cls._inflight.get(tid)
            if thread_inflight is None:
                return
            start = thread_inflight.pop((context, name), None)
            if start is None:
                return
            ms = (now - start) * 1000.0
            cls._samples.setdefault(context, {}).setdefault(name, []).append(ms)

    @classmethod
    def add_sample(cls, name, ms, context="default"):
        """Append an externally measured duration (ms) to a (name, context) section, for spans
        that a start()/stop() pair cannot bracket: a latency measured from a camera timestamp,
        or a sum of non-contiguous calls. Mirrors Profiler::addSample in profiler.hpp."""
        if not cls._enabled:
            return
        with cls._lock:
            cls._samples.setdefault(context, {}).setdefault(name, []).append(ms)

    @classmethod
    def dump_stats(cls, filename):
        """Write the accumulated statistics to `filename` as the profile_stats JSON the benchmark
        report builders consume."""
        profile_stats = {}
        with cls._lock:
            for ctx in sorted(cls._samples):
                sections = {}
                for sec in sorted(cls._samples[ctx]):
                    samples = cls._samples[ctx][sec]
                    if not samples:
                        continue
                    sections[sec] = _compute_stats(samples)
                profile_stats[ctx] = sections

        out = {"profile_stats": profile_stats}
        try:
            with open(filename, "w") as f:
                json.dump(out, f, indent=4)
                f.write("\n")
        except OSError:
            print(f"[Profiler] Could not write {filename}", file=sys.stderr)

    @classmethod
    def print_stats(cls):
        """Print a compact per-section summary to stdout."""
        with cls._lock:
            print("Profiler stats (ms):")
            for ctx in sorted(cls._samples):
                print(f"[{ctx}]")
                for sec in sorted(cls._samples[ctx]):
                    samples = cls._samples[ctx][sec]
                    if not samples:
                        continue
                    st = _compute_stats(samples)
                    print(f"  {sec}  mean={st['mean']}  median={st['median']}  count={st['count']}")
