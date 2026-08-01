import re
from typing import Optional


_TOPIC_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*")


def normalize_topic_slug(value: Optional[str]) -> str:
    slug = (value or "default").strip().lower()
    if not slug:
        slug = "default"
    if not _TOPIC_RE.fullmatch(slug):
        raise ValueError(
            "topic must contain only letters, numbers, underscores, and "
            "dot-separated segments (e.g. 'squid.experiment')"
        )
    return slug


def memory_root_slug(value: Optional[str]) -> str:
    """The topic whose memory.md a dotted sub-topic (e.g. 'squid.experiment')
    shares. Everything but memory — worktrees, branches, queues, sessions —
    stays keyed on the full slug so sub-topics still run in parallel."""
    return normalize_topic_slug(value).split(".", 1)[0]
