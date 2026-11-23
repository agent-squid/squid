"""Topic-memory provider rules and one-time human approval challenges."""

from dataclasses import dataclass
import secrets
from time import monotonic

from .memory import _load_frontmatter, topic_memory_path


BLOCK_REASONS = frozenset({"provider_denied", "provider_unverified", "provider_approval_required", "provider_policy_invalid"})


@dataclass(frozen=True)
class Rule:
    allow: frozenset[str] | None
    deny: frozenset[str]

    def decision(self, provider: str | None) -> str | None:
        if provider is None and self.deny:
            return "provider_unverified"
        if provider in self.deny:
            return "provider_denied"
        if provider is None or (self.allow is not None and provider not in self.allow):
            return "provider_approval_required"
        return None


class InvalidPolicy(ValueError):
    pass


def rule_from_memory(content: str) -> Rule | None:
    try:
        squid = _load_frontmatter(content, strict=True).get("squid")
    except ValueError as exc:
        raise InvalidPolicy(str(exc)) from exc
    if not isinstance(squid, dict):
        return None
    fields = {}
    for field in ("allow_providers", "deny_providers"):
        if field not in squid:
            continue
        ids = squid[field]
        if not isinstance(ids, list) or not all(isinstance(i, str) and i.strip() for i in ids):
            raise InvalidPolicy(f"squid.{field} must be a list of provider IDs.")
        fields[field] = frozenset(ids)
    if not fields:
        return None
    return Rule(fields.get("allow_providers"), fields.get("deny_providers", frozenset()))


def rule_for(topic: str) -> Rule | None:
    try:
        content = topic_memory_path(topic).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise InvalidPolicy("Squid could not read topic memory.") from None
    return rule_from_memory(content)


@dataclass(frozen=True)
class Runtime:
    topic: str
    agent: str | None
    fingerprint: str
    model: str | None
    revision: int


# Scoped to the authenticated browser session and the complete submitted request.
# Admission takes the challenge atomically and passes only the runtime to the worker.
_challenges: dict[str, tuple[float, str, str, Runtime]] = {}


def challenge(session: str, request_digest: str, runtime: Runtime) -> str:
    now = monotonic()
    for token, entry in list(_challenges.items()):
        if entry[0] <= now:
            del _challenges[token]
    if len(_challenges) >= 1024:
        del _challenges[next(iter(_challenges))]
    token = secrets.token_urlsafe(32)
    _challenges[token] = (now + 300, session, request_digest, runtime)
    return token


def accept(token: str | None, session: str, request_digest: str, runtime: Runtime) -> bool:
    entry = _challenges.get(token) if token else None
    if entry is None or entry[0] <= monotonic() or entry[1:] != (session, request_digest, runtime):
        return False
    del _challenges[token]
    return True


def detail(decision: str, topic: str, agent: str | None, provider: str | None) -> str:
    target = f"Agent {agent or '(default)'} on topic #{topic}"
    if decision == "provider_denied":
        return f"{target} uses denied provider {provider}."
    if decision == "provider_unverified":
        return f"{target} has a provider Squid could not verify."
    return f"{target} requires one-time approval to use provider {provider or '(unverified)'}."
