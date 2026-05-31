"""Pure safety scans. No side effects.

These functions are testable in isolation and used by the collector and reviewer.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------

def _normalize_pattern(p: str) -> str:
    return p.strip().rstrip("/")


def path_is_protected(file_path: str, blocked_patterns: list[str]) -> bool:
    """Return True if `file_path` matches any blocked pattern.

    Patterns may include `~/...` (expanded), `**/*.pem`, or plain `.env`.
    Matching uses fnmatch semantics; pattern `.env` also matches a literal
    component named `.env` at any depth.
    """
    f = Path(file_path).expanduser()
    f_str = str(f)
    parts = set(f.parts)
    for raw in blocked_patterns:
        pat = _normalize_pattern(raw)
        expanded = str(Path(pat).expanduser())
        # Direct fnmatch (handles **/*.pem etc.)
        if fnmatch.fnmatch(f_str, expanded):
            return True
        if fnmatch.fnmatch(f.name, pat):
            return True
        # Plain component (e.g. ".env") matches at any depth.
        if pat in parts:
            return True
    return False


def scan_protected_paths_in_files(file_paths: list[str], blocked_patterns: list[str]) -> list[str]:
    return [f for f in file_paths if path_is_protected(f, blocked_patterns)]


def extract_changed_files_from_diff(diff_text: str) -> list[str]:
    """Return the list of paths from `diff --git a/x b/x` headers."""
    out: list[str] = []
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", diff_text, re.MULTILINE):
        out.append(m.group(2))
    return sorted(set(out))


# ---------------------------------------------------------------------------
# Dangerous / approval commands
# ---------------------------------------------------------------------------

def scan_text_for_dangerous_commands(text: str, dangerous_commands: list[str]) -> list[str]:
    found: list[str] = []
    low = text.lower()
    for cmd in dangerous_commands:
        if cmd.lower() in low:
            found.append(cmd)
    return found


def scan_text_for_approval_commands(text: str, approval_commands: list[str]) -> list[str]:
    found: list[str] = []
    low = text.lower()
    for cmd in approval_commands:
        if cmd.lower() in low:
            found.append(cmd)
    return found
