"""Operator verifier handoff for self-reexec; plaintext never reaches children."""
import hashlib
import os
from pathlib import Path
import secrets
import stat

RESTART_DIGEST = 'SQUID_INTERNAL_WATCH_CONTROL_DIGEST'


def control_token_file():
    return Path.home() / '.squid' / 'watch-control.token'


def file_token(*, create=False):
    """Read an owner-only regular file; only host startup provisions it."""
    path = control_token_file()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_hex(32) + '\n')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('watch control file must be owner-only and owned by the current user')
        token = stream.read(514).strip()
    if not 32 <= len(token) <= 512:
        raise ValueError('invalid watch control credential file')
    return token


def load_digest(environment, *, create=False):
    secret = environment.pop('SQUID_WATCH_CONTROL_TOKEN', None)
    inherited = environment.pop(RESTART_DIGEST, None)
    if secret is not None:
        if not 32 <= len(secret) <= 512:
            raise ValueError('watch control credential must contain 32 to 512 characters')
        return hashlib.sha256(secret.encode()).digest()
    if inherited is not None:
        try:
            digest = bytes.fromhex(inherited)
            if len(digest) != 32 or len(inherited) != 64:
                raise ValueError()
            return digest
        except ValueError:
            raise ValueError('invalid watch control restart verifier') from None
    secret = file_token(create=create)
    if secret is not None:
        return hashlib.sha256(secret.encode()).digest()
    return None


def restart_environment(environment, digest):
    result = dict(environment)
    for name in ('SQUID_WATCH_CONTROL_TOKEN', RESTART_DIGEST, 'SQUID_TURN_TOKEN', 'SQUID_CONTINUE_TOKEN', 'SQUID_API_URL'):
        result.pop(name, None)
    if digest is not None:
        result[RESTART_DIGEST] = digest.hex()
    return result
