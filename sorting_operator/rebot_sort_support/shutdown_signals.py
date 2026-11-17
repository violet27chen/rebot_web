"""Keep repeated normal stop signals from interrupting device cleanup."""
from contextlib import contextmanager
import signal
import threading


@contextmanager
def finish_cleanup():
    # SIGTERM is deliberately untouched: the explicit force-stop path remains available.
    main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN) if main else None
    try:
        yield
    finally:
        if main:
            signal.signal(signal.SIGINT, previous)
