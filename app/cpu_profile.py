# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""On-demand CPU profile of the running server — which THREAD is busy, and in
what code.

The server is one Python process holding every collector thread plus
waitress's request threads, so `top` can only say "python is at 100%" (which,
under the GIL, is the ceiling for Python work). This answers the next
question without installing anything on the host: per-thread CPU time comes
from /proc/self/task/<tid>/stat, and a short stack-sampling pass over
sys._current_frames() shows what each busy thread is executing.

Nothing runs until an admin asks (`/admin/cpu?seconds=N`), so it costs nothing
the rest of the time.
"""
import collections
import os
import sys
import threading
import time

MAX_SECONDS = 30
SAMPLE_INTERVAL = 0.02          # 50 Hz
STACK_DEPTH = 8                 # innermost frames kept per sample
TOP_STACKS = 5                  # per thread
MIN_CPU_PCT = 1.0               # threads below this are listed, not detailed

# Frames that mean "this thread is waiting, not computing".
_IDLE_FUNCS = {"wait", "_wait_for_tstate_lock", "select", "poll", "accept",
               "readinto", "recv_into", "recv", "sleep", "get", "run_forever",
               "_recv_bytes", "handler_thread"}

_CLK_TCK = (os.sysconf("SC_CLK_TCK")
            if hasattr(os, "sysconf") and "SC_CLK_TCK" in os.sysconf_names
            else 100)


def _task_cpu_seconds(tid):
    """user+system CPU seconds of one OS thread, or None off Linux / if gone."""
    try:
        with open(f"/proc/self/task/{tid}/stat", encoding="ascii") as f:
            stat = f.read()
    except OSError:
        return None
    # The comm field may contain spaces; everything after the last ')' is
    # whitespace-separated. utime/stime are fields 14/15 → index 11/12 here.
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return (int(fields[11]) + int(fields[12])) / _CLK_TCK
    except (IndexError, ValueError):
        return None


def _all_task_cpu():
    """{tid: cpu_seconds} for every OS thread in the process (Python or not)."""
    try:
        tids = [int(t) for t in os.listdir("/proc/self/task")]
    except OSError:
        return {}
    out = {}
    for tid in tids:
        sec = _task_cpu_seconds(tid)
        if sec is not None:
            out[tid] = sec
    return out


def _task_name(tid):
    try:
        with open(f"/proc/self/task/{tid}/comm", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "?"


def _stack_key(frame):
    """The innermost STACK_DEPTH frames as one readable string, innermost last."""
    parts = []
    while frame is not None and len(parts) < STACK_DEPTH:
        code = frame.f_code
        fname = code.co_filename
        # Shorten to something readable: app/... for ours, the module file for
        # libraries.
        idx = fname.replace("\\", "/").rfind("/app/")
        short = fname[idx + 1:] if idx >= 0 else os.path.basename(fname)
        parts.append(f"{short}:{frame.f_lineno} {code.co_name}")
        frame = frame.f_back
    return "\n      ".join(reversed(parts))


def _is_idle(frame):
    return frame is not None and frame.f_code.co_name in _IDLE_FUNCS


def profile(seconds=10):
    """Sample for `seconds` (capped) and return a plain-text report."""
    seconds = max(1.0, min(float(seconds), MAX_SECONDS))
    me = threading.get_ident()
    py_threads = {t.ident: t for t in threading.enumerate()}
    native = {t.ident: getattr(t, "native_id", None)
              for t in py_threads.values()}

    cpu_before = _all_task_cpu()
    wall_before = time.monotonic()
    proc_before = time.process_time()

    stacks = collections.defaultdict(collections.Counter)
    busy = collections.Counter()
    total = collections.Counter()
    deadline = wall_before + seconds
    while time.monotonic() < deadline:
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            total[ident] += 1
            if _is_idle(frame):
                continue
            busy[ident] += 1
            stacks[ident][_stack_key(frame)] += 1
        time.sleep(SAMPLE_INTERVAL)

    wall = time.monotonic() - wall_before
    proc_cpu = time.process_time() - proc_before
    cpu_after = _all_task_cpu()

    def pct(tid):
        if tid is None or tid not in cpu_before or tid not in cpu_after:
            return None
        return 100.0 * (cpu_after[tid] - cpu_before[tid]) / wall

    rows = []
    seen_tids = set()
    for ident, t in py_threads.items():
        if ident == me:
            continue
        tid = native.get(ident)
        seen_tids.add(tid)
        rows.append((pct(tid), t.name, tid, ident))
    # OS threads with no Python thread object (C extensions, SQLite, etc.)
    my_tid = native.get(me)
    for tid in cpu_after:
        if tid not in seen_tids and tid != my_tid:
            p = pct(tid)
            if p and p >= MIN_CPU_PCT:
                rows.append((p, f"[native] {_task_name(tid)}", tid, None))
    rows.sort(key=lambda r: -(r[0] or 0))

    out = [f"CPU profile over {wall:.1f} s — process {100 * proc_cpu / wall:.0f}% "
           f"of one core (100% = one core; the GIL caps Python work there).",
           f"Sampled {sum(total.values())} thread-stacks at "
           f"{1 / SAMPLE_INTERVAL:.0f} Hz. This request's own thread is excluded.",
           ""]
    out.append(f"{'CPU%':>6}  {'busy%':>6}  thread")
    for p, name, tid, ident in rows:
        b = (100.0 * busy[ident] / total[ident]
             if ident is not None and total[ident] else None)
        out.append(f"{(f'{p:.1f}' if p is not None else '-'):>6}  "
                   f"{(f'{b:.0f}' if b is not None else '-'):>6}  "
                   f"{name} (tid {tid})")
    out.append("")
    out.append("busy% = share of samples NOT parked in a wait/sleep/select.")
    out.append("")
    for p, name, tid, ident in rows:
        if ident is None or not stacks[ident]:
            continue
        if (p or 0) < MIN_CPU_PCT and busy[ident] < 0.05 * max(total[ident], 1):
            continue
        out.append(f"=== {name} — {p:.1f}% CPU" if p is not None
                   else f"=== {name}")
        n = sum(stacks[ident].values())
        for key, count in stacks[ident].most_common(TOP_STACKS):
            out.append(f"  {100 * count / n:5.1f}%  {key}")
        out.append("")
    return "\n".join(out)
