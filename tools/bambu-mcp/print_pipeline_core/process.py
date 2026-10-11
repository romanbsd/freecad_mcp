"""Bounded pipe capture and process-group termination (MIT)."""
import math, os, selectors, signal, subprocess, time, threading
from pathlib import Path

_active = set()
_lock = threading.RLock()
_cancelled = threading.Event()


def terminate_active_process_groups():
    """Used by the standalone worker's cancellation handler, not a global UI operation."""
    with _lock:
        _cancelled.set()
        for pid in list(_active):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def run_bounded(argv, directory, timeout, log_limit=4*1024*1024):
    """POSIX-only bounded pipe capture, including a deadline after parent exit."""
    if os.name != "posix":
        raise ValueError("Bounded execution requires POSIX process groups")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    directory = Path(directory)
    started = time.monotonic()
    with _lock:
        if _cancelled.is_set():
            raise ValueError('Execution cancelled')
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, cwd=directory)
        _active.add(process.pid)
    head, tail, total, error = b'', b'', 0, None
    completed = False
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        with (directory/'slicer.log').open('wb') as log:
            while selector.get_map() or process.poll() is None:
                remaining = timeout-(time.monotonic()-started)
                if remaining <= 0:
                    error = 'timeout'; break
                events = selector.select(min(remaining, 0.1))
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    head = (head+chunk)[:16384]
                    tail = (tail+chunk)[-16384:]
                    allowed = max(0, log_limit-total)
                    log.write(chunk[:allowed])
                    total += len(chunk)
                    if total > log_limit:
                        error = 'log_limit'; break
                if error:
                    break
            completed = True
    finally:
        if not completed or error or process.poll() is None:
            try:
                # The worker gets a grace interval to terminate its separately grouped slicers.
                os.killpg(process.pid, signal.SIGTERM)
                time.sleep(.25)
                # Reap the group leader before signaling again: macOS can report
                # EPERM for a group containing only an unreaped zombie.
                process.poll()
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS can return EPERM for a zombie-only group. Suppress it
                # only once the leader is reaped; a live permission failure
                # must remain visible rather than entering an unbounded wait.
                if process.poll() is None:
                    raise
        process.wait()
        with _lock:
            _active.discard(process.pid)
        selector.close()
        process.stdout.close()
    captured = head if total <= 16384 else head+b'\n[log truncated; tail follows]\n'+tail
    return process.returncode, error, captured.decode('utf-8', errors='replace'), time.monotonic()-started
