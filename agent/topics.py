import re
from typing import Optional


_TOPIC_RE = re.compile(r"[a-z0-9_]+")


def normalize_topic_slug(value: Optional[str]) -> str:
    slug = (value or "default").strip().lower()
    if not slug:
        slug = "default"
    if not _TOPIC_RE.fullmatch(slug):
        raise ValueError("topic must contain only letters, numbers, and underscores")
    return slug
