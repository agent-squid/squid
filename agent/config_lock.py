"""Cross-process serialization for host configuration read/modify/write operations."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


@contextmanager
def config_file_lock(path):
    # Lock a stable sidecar: replacing the YAML file must not replace the lock.
    path = Path(path).resolve()
    fd = os.open(path.with_name(path.name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing also releases the lock if acquisition or the body failed.
        os.close(fd)
