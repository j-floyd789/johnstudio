"""Small pure helpers shared across modules. Keep dependency-free."""
from __future__ import annotations

import errno
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable

import yaml


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s or "item"


def approx_token_count(text: str) -> int:
    """Cheap deterministic token approximation: ~4 chars per token, min 1."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


_TOLERANT_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:\s*(.*)$")


def _tolerant_parse(fm_raw: str) -> dict:
    """Line-by-line YAML fallback for skill frontmatter with unquoted colons.

    Handles flat `key: value` with optional list-on-one-line `[a, b, c]` literals.
    Triggered only when full YAML parse fails (common in third-party SKILL.md files
    where descriptions contain colons like "REST APIs: design and patterns").
    """
    out: dict = {}
    for line in fm_raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _TOLERANT_LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            out[key] = [
                v.strip().strip('"').strip("'")
                for v in inner.split(",") if v.strip()
            ]
        elif val.startswith(('"', "'")) and val.endswith(val[0]):
            out[key] = val[1:-1]
        elif val == "":
            out[key] = ""
        else:
            out[key] = val
    return out


def split_frontmatter(markdown: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Empty frontmatter -> {}.

    Tolerant: missing or malformed frontmatter falls back to a line-based parser
    that handles common upstream conventions (e.g. unquoted colon-containing
    descriptions, simple list literals). Non-mapping YAML returns {}.
    """
    if not markdown.startswith("---"):
        return {}, markdown
    m = _FRONTMATTER_RE.match(markdown)
    if not m:
        return {}, markdown
    fm_raw, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_raw)
    except yaml.YAMLError:
        return _tolerant_parse(fm_raw), body
    if not isinstance(fm, dict):
        return {}, markdown
    return fm, body


def join_frontmatter(fm: dict, body: str) -> str:
    if not fm:
        return body
    return "---\n" + yaml.safe_dump(fm, sort_keys=False).rstrip() + "\n---\n" + body


def write_yaml(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def read_yaml(path: Path) -> dict | list:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------

def which(cmd: str) -> str | None:
    """Return absolute path of `cmd` or None. Handles 'cmd arg' by checking first token."""
    first = cmd.split()[0] if cmd else ""
    return shutil.which(first)


def run(
    args: list[str],
    cwd: str | Path | None = None,
    check: bool = False,
    timeout: int | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=check,
        timeout=timeout,
        text=True,
        capture_output=capture,
    )


# ---------------------------------------------------------------------------
# Resilient file IO
# ---------------------------------------------------------------------------

# APFS (and some networked filesystems) can surface a transient OSError when
# many worker processes race to read the same catalog/config file: errno 11
# (EDEADLK, "Resource deadlock avoided") and errno 35 (EAGAIN). These are not
# real corruption — the next attempt almost always succeeds — so we retry with
# an attempt-based (deterministic, non-random) exponential backoff + jitter.
_IO_RETRY_ERRNOS = (errno.EDEADLK, errno.EAGAIN)  # 11, 35


def read_text_retry(
    path: Path,
    *,
    retries: int = 6,
    base_sleep: float = 0.05,
    encoding: str = "utf-8",
) -> str:
    """Path.read_text() with retry on EDEADLK/EAGAIN races.

    Backoff is exponential with an attempt-derived (not random) jitter so the
    behaviour stays deterministic. Re-raises the last error after exhausting
    `retries`.
    """
    last: OSError | None = None
    for attempt in range(retries + 1):
        try:
            return Path(path).read_text(encoding=encoding)
        except OSError as e:
            if e.errno not in _IO_RETRY_ERRNOS:
                raise
            last = e
            if attempt >= retries:
                break
            # attempt-based jitter: vary by attempt index, never random.
            jitter = base_sleep * 0.1 * (attempt + 1)
            time.sleep(base_sleep * (2 ** attempt) + jitter)
    assert last is not None
    raise last


def write_text_retry(
    path: Path,
    content: str,
    *,
    overwrite: bool = False,
    retries: int = 6,
    base_sleep: float = 0.05,
    encoding: str = "utf-8",
) -> Path:
    """write_text() with retry on EDEADLK/EAGAIN races. Mirrors `write_text`."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and not overwrite:
        return p
    last: OSError | None = None
    for attempt in range(retries + 1):
        try:
            p.write_text(content, encoding=encoding)
            return p
        except OSError as e:
            if e.errno not in _IO_RETRY_ERRNOS:
                raise
            last = e
            if attempt >= retries:
                break
            jitter = base_sleep * 0.1 * (attempt + 1)
            time.sleep(base_sleep * (2 ** attempt) + jitter)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str, *, overwrite: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return path
    path.write_text(content, encoding="utf-8")
    return path


def iter_markdown_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.md"):
        if p.is_file():
            yield p


def package_root() -> Path:
    """Return the package root (project root, not the /johnstudio module)."""
    return Path(__file__).resolve().parent.parent
