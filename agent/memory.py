from pathlib import Path
from typing import Optional

from .topics import normalize_topic_slug


_ROOT = Path(__file__).parent.parent
TOPICS_CONTEXT_DIR = _ROOT / "context" / "topics"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(_ROOT))
    except ValueError:
        return str(path)


def topic_memory_path(topic: str) -> Path:
    slug = normalize_topic_slug(topic)
    return TOPICS_CONTEXT_DIR / slug / "memory.md"


def read_topic_memory(topic: str) -> dict:
    slug = normalize_topic_slug(topic)
    path = topic_memory_path(slug)
    if not path.exists():
        return {
            "topic": slug,
            "exists": False,
            "content": "",
            "path": _display_path(path),
        }
    content = path.read_text(encoding="utf-8")
    return {
        "topic": slug,
        "exists": True,
        "content": content,
        "path": _display_path(path),
    }


def write_topic_memory(topic: str, content: str) -> dict:
    slug = normalize_topic_slug(topic)
    path = topic_memory_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return read_topic_memory(slug)


def topic_memory_prompt_block(topic: str) -> Optional[str]:
    data = read_topic_memory(topic)
    content = data["content"].strip()
    if not content:
        return None
    slug = data["topic"]
    return "\n".join([
        "Persistent user-editable topic memory:",
        f'<topic_memory topic="{slug}">',
        content,
        "</topic_memory>",
    ])
