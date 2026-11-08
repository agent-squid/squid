"""Capture local Git identity reads before a continuation claim's write lock.

Only the short admission check reuses these observations. Execution and each
publication effect still validate fresh filesystem state. Database authority,
expiry and budgets are never cached. A new read under the lock aborts admission
so changed destinations cannot accidentally invoke Git inside the transaction.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_reads = ContextVar('continuation_admission_reads', default=None)


class UncapturedRead(Exception):
    pass


@contextmanager
def capture():
    observations = {}
    token = _reads.set((observations, False))
    try:
        yield observations
    finally:
        _reads.reset(token)


@contextmanager
def replay(observations):
    token = _reads.set((observations, True))
    try:
        yield
    finally:
        _reads.reset(token)


def local_git_read(function):
    @wraps(function)
    def read(*args, **kwargs):
        context = _reads.get()
        if context is None:
            return function(*args, **kwargs)
        observations, replaying = context
        key = (function, args, tuple(sorted(kwargs.items())))
        if key not in observations:
            if replaying:
                raise UncapturedRead()
            try:
                observations[key] = (True, function(*args, **kwargs))
            except Exception as exc:
                # Invalid/missing repositories must also fail closed during
                # locked validation, without repeating their failed Git read.
                observations[key] = (False, exc)
        successful, result = observations[key]
        if not successful:
            raise result
        return result
    return read
