"""Thread-local structured progress, independent of terminal output."""
from contextvars import ContextVar
from contextlib import contextmanager

_sink = ContextVar('block_party_progress', default=None)


def emit(phase, **values):
    sink = _sink.get()
    if sink: sink({'phase': phase, **values})


@contextmanager
def reporting(callback):
    token = _sink.set(callback)
    try: yield
    finally: _sink.reset(token)
