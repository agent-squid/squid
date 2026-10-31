"""Operator verifier handoff for self-reexec; plaintext never reaches children."""
import hashlib

RESTART_DIGEST = 'SQUID_INTERNAL_WATCH_CONTROL_DIGEST'


def load_digest(environment):
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
    return None


def restart_environment(environment, digest):
    result = dict(environment)
    for name in ('SQUID_WATCH_CONTROL_TOKEN', RESTART_DIGEST, 'SQUID_TURN_TOKEN', 'SQUID_API_URL'):
        result.pop(name, None)
    if digest is not None:
        result[RESTART_DIGEST] = digest.hex()
    return result
