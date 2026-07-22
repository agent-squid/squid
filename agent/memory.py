import hashlib
from pathlib import Path
from typing import Optional

import yaml

from .topics import normalize_topic_slug


_SQUID_HOME = Path.home() / ".squid"
TOPICS_CONTEXT_DIR = _SQUID_HOME / "context" / "topics"


def _content_revision(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _split_frontmatter(content: str) -> tuple[Optional[str], str]:
    if not content.startswith("---"):
        return None, content
    lines = content.splitlines(keepends=True)
    if not lines:
        return None, content
    if lines[0].strip() != "---":
        return None, content

    offset = len(lines[0])
    for line in lines[1:]:
        if line.strip() == "---":
            yaml_text = content[len(lines[0]):offset]
            body_start = offset + len(line)
            return yaml_text, content[body_start:]
        offset += len(line)
    return None, content


def _load_frontmatter(content: str) -> dict:
    yaml_text, _body = _split_frontmatter(content)
    if yaml_text is None:
        return {}
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


_CODE_ROOTS_HINT_LINES = ["  # code_roots:", "  #   - /absolute/path/to/repo"]

_PLACEHOLDER_MEMORY = (
    "---\n"
    "squid:\n"
    "  # code_roots:\n"
    "  #   - /absolute/path/to/repo\n"
    "  # code_roots_skipped: true\n"
    "---\n"
)


def _insert_code_roots_hint(yaml_text: str) -> str:
    lines = yaml_text.split("\n")
    out = []
    for line in lines:
        out.append(line)
        if line.strip() == "squid:":
            out.extend(_CODE_ROOTS_HINT_LINES)
    return "\n".join(out)


def _dump_memory(frontmatter: dict, body: str) -> str:
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False).strip()
    squid = frontmatter.get("squid")
    if isinstance(squid, dict) and not squid.get("code_roots"):
        yaml_text = _insert_code_roots_hint(yaml_text)
    if body:
        return f"---\n{yaml_text}\n---\n{body}"
    return f"---\n{yaml_text}\n---\n"


def _normalize_code_roots(value) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    roots: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            roots.append(text)
    return roots


def _display_path(path: Path) -> str:
    try:
        return "~/.squid/" + str(path.relative_to(_SQUID_HOME))
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
            "revision": _content_revision(""),
            "path": _display_path(path),
            "squid": topic_memory_squid_config_from_content(""),
        }
    content = path.read_text(encoding="utf-8")
    return {
        "topic": slug,
        "exists": True,
        "content": content,
        "revision": _content_revision(content),
        "path": _display_path(path),
        "squid": topic_memory_squid_config_from_content(content),
    }


def ensure_topic_memory_placeholder(topic: str) -> dict:
    slug = normalize_topic_slug(topic)
    path = topic_memory_path(slug)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_PLACEHOLDER_MEMORY, encoding="utf-8")
    return read_topic_memory(slug)


def write_topic_memory(topic: str, content: str) -> dict:
    slug = normalize_topic_slug(topic)
    path = topic_memory_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return read_topic_memory(slug)


def write_topic_memory_squid_code_roots(
    topic: str,
    *,
    code_roots: Optional[list[str]] = None,
    code_roots_skipped: bool = False,
) -> dict:
    slug = normalize_topic_slug(topic)
    path = topic_memory_path(slug)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    yaml_text, body = _split_frontmatter(content)
    frontmatter = _load_frontmatter(content) if yaml_text is not None else {}
    squid = frontmatter.get("squid")
    if not isinstance(squid, dict):
        squid = {}
    roots = _normalize_code_roots(code_roots or [])
    if roots:
        squid["code_roots"] = roots
        squid.pop("code_roots_skipped", None)
    elif code_roots_skipped:
        squid.pop("code_roots", None)
        squid["code_roots_skipped"] = True
    else:
        squid.pop("code_roots", None)
        squid.pop("code_roots_skipped", None)
    if squid:
        frontmatter["squid"] = squid
    else:
        frontmatter.pop("squid", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_memory(frontmatter, body if yaml_text is not None else content), encoding="utf-8")
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


def topic_memory_squid_config_from_content(content: str) -> dict:
    frontmatter = _load_frontmatter(content)
    squid = frontmatter.get("squid")
    if not isinstance(squid, dict):
        squid = {}
    code_roots = _normalize_code_roots(squid.get("code_roots"))
    skipped = bool(squid.get("code_roots_skipped"))
    return {
        "code_roots": code_roots,
        "code_roots_skipped": skipped and not code_roots,
        "code_roots_missing": not code_roots and not skipped,
    }


def topic_memory_squid_config(topic: str) -> dict:
    return topic_memory_squid_config_from_content(read_topic_memory(topic)["content"])


def code_roots_prompt_block(code_roots: list[str], isolated: bool = False) -> Optional[str]:
    roots = _normalize_code_roots(code_roots)
    if not roots:
        return None
    lines = [
        "Topic code roots:",
        "<squid_code_roots>",
        *roots,
        "</squid_code_roots>",
        "Treat these paths as the primary codebase roots for this topic. Prefer working in them over the process working directory.",
    ]
    if isolated:
        lines.append(
            "You are the sole writer in this worktree for this turn. Trust your writes — never re-read a file to confirm an edit, never re-verify state you just set. Do not delete dependency/cache directories such as .venv, node_modules, or vendor; they may be symlinks to the source repo."
        )
        lines.append(
            "This worktree is a temporary staging copy: edits made here are synced into the real repository automatically once the turn ends, with no action needed from you. Do not run git commit, push, or other branch/history-changing commands against these paths — if asked to commit or push, run that against the process's working directory (the real repository) instead."
        )
    return "\n".join(lines)
