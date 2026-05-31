"""Import skills/agents/rules from various upstream layouts into JohnStudio's
normalized registry directory.

Normalized layout under `~/.johnstudio/skill-registry/skills/<skill_id>/`:
    original.md          — exact source bytes; never overwritten
    distilled.md         — deterministic distillation
    summary.md           — first 200–400 useful words
    metadata.yaml        — normalized SkillMetadata
    source.json          — origin info
    score.json           — routing prior
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

import yaml

from . import config, db, utils
from .models import SkillMetadata

# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

ROOT_RULE_FILES = {"CLAUDE.md", "AGENTS.md", "GEMINI.md"}


def detect_type(path: Path) -> str:
    """Return one of: skill, agent, rule, command, hook, mcp."""
    name = path.name
    parts_lower = [p.lower() for p in path.parts]
    if name in ROOT_RULE_FILES or name.endswith(".mdc"):
        return "rule"
    if name == "SKILL.md":
        return "skill"
    if "hooks" in parts_lower or name.endswith(".hook.md"):
        return "hook"
    if "commands" in parts_lower:
        return "command"
    if "mcp-configs" in parts_lower or name.endswith(".mcp.json"):
        return "mcp"
    if "rules" in parts_lower:
        return "rule"
    if "agents" in parts_lower or "categories" in parts_lower or "personas" in parts_lower:
        return "agent"
    return "skill"


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "frontend": ["frontend", "react", "nextjs", "vue", "svelte", "ui", "css", "tailwind"],
    "backend": ["backend", "api", "rest", "graphql", "fastapi", "django", "express", "microservice"],
    "database": ["database", "postgres", "sql", "schema", "prisma", "mongo"],
    "testing": ["test", "qa", "playwright", "pytest", "jest", "tdd"],
    "debugging": ["debug", "error", "bug"],
    "security": ["security", "audit", "owasp", "penetration", "secret", "compliance"],
    "devops": ["devops", "docker", "kubernetes", "terraform", "sre", "deploy", "ci"],
    "ui-ux": ["ui", "ux", "accessibility", "design"],
    "documentation": ["docs", "documentation", "writer"],
    "agent-orchestration": ["orchestrator", "coordinator", "agent", "multi-agent", "handoff"],
    "memory-context": ["memory", "context", "synthesizer"],
    "product-business": ["product", "manager", "cto", "startup", "founder"],
    "compliance-privacy": ["gdpr", "hipaa", "soc2", "compliance", "privacy"],
}


# Normalize various frontmatter list shapes (str, comma-string, list[str], list[dict]).
def _as_str_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [t.strip() for t in v.split(",") if t.strip()]
    if isinstance(v, list):
        out = []
        for item in v:
            if isinstance(item, str):
                out.append(item.strip())
            elif isinstance(item, dict) and "name" in item:
                out.append(str(item["name"]))
        return [s for s in out if s]
    return [str(v)]


def parse_markdown_with_frontmatter(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return utils.split_frontmatter(raw)


_WB_RE_CACHE: dict[str, re.Pattern] = {}


def _wb(kw: str) -> re.Pattern:
    if kw not in _WB_RE_CACHE:
        _WB_RE_CACHE[kw] = re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", re.IGNORECASE)
    return _WB_RE_CACHE[kw]


def _infer_category(folder_parts: list[str], filename: str, fm: dict, body: str) -> str:
    """Derive canonical category from folder structure, frontmatter, and content.

    Folder text is intentionally scoped to the *last 3* path components so absolute
    pytest tmp paths (containing words like 'test_') don't poison classification.
    Matching uses word boundaries to avoid substring collisions ('test' vs 'pytest').
    """
    nearby = [p.lower() for p in folder_parts[-3:]]
    folder_text = " ".join(nearby)
    domain = (fm.get("domain") or fm.get("category") or "").lower()
    tag_text = " ".join(_as_str_list(fm.get("tags"))).lower()
    haystacks = [domain, folder_text, filename.lower(), tag_text]
    body_snippet = body[:500].lower()

    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            pat = _wb(kw)
            for hs in haystacks:
                if pat.search(hs):
                    return cat
            if pat.search(body_snippet):
                return cat
    return "general-guidance"


def _derive_id(fm: dict, path: Path) -> str:
    raw = fm.get("id") or fm.get("name") or path.stem
    return utils.slugify(str(raw))


def extract_metadata(
    fm: dict,
    path: Path,
    *,
    source_repo: str | None,
    source_path: str | None,
    body: str,
    trust_level: str = "unreviewed",
    enabled: bool = False,
) -> SkillMetadata:
    folder_parts = list(path.parts[:-1])
    tags = sorted(set(
        _as_str_list(fm.get("tags")) +
        _as_str_list(fm.get("keywords"))
    ))
    return SkillMetadata(
        id=_derive_id(fm, path),
        name=str(fm.get("name") or path.stem),
        type=detect_type(path),
        source_repo=source_repo,
        source_path=source_path,
        category=_infer_category(folder_parts, path.name, fm, body),
        description=str(fm.get("description") or "").strip(),
        tags=tags,
        languages=_as_str_list(fm.get("languages")),
        frameworks=_as_str_list(fm.get("frameworks")),
        agent_roles=_as_str_list(fm.get("agent_roles")),
        file_patterns=_as_str_list(fm.get("file_patterns") or fm.get("globs")),
        dependencies=_as_str_list(fm.get("dependencies")),
        priority=(fm.get("priority") or "medium") if fm.get("priority") in ("low", "medium", "high") else "medium",
        max_context_tokens=int(fm.get("max_context_tokens") or 2500),
        trust_level=trust_level,
        enabled=enabled,
        created_at=datetime.utcnow().isoformat(timespec="seconds"),
        updated_at=datetime.utcnow().isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# Distillation
# ---------------------------------------------------------------------------

KEEP_HEADING_RE = re.compile(r"^(##|###)\s+(.+?)\s*$", re.MULTILINE)
KEEP_BULLET_RE = re.compile(r"^[\-\*]\s+.*\b(must|should|never|avoid|always|prefer|forbid)\b.*$", re.IGNORECASE)
CHECKLIST_RE = re.compile(r"^[\-\*]\s+\[[ xX]\]\s+.+$")
CODE_FENCE_RE = re.compile(r"```")
SECTION_KEEP_TITLES = {
    "when to use", "when to activate", "checklist", "anti-patterns", "examples",
    "rules", "workflow", "must / never", "principles", "purpose",
}
DROP_SECTION_TITLES = {"install", "installation", "sponsor", "sponsors", "license"}


def _section_iter(markdown: str) -> Iterable[tuple[str, str]]:
    """Yield (title, body) for each `##`/`###` section. Body excludes the header."""
    parts = re.split(r"^(##+\s+.+)$", markdown, flags=re.MULTILINE)
    current_title = ""
    buf: list[str] = []
    for chunk in parts:
        if re.match(r"^##+\s+", chunk):
            if current_title or buf:
                yield current_title, "\n".join(buf).strip("\n")
            current_title = re.sub(r"^##+\s+", "", chunk).strip()
            buf = []
        else:
            buf.append(chunk)
    if current_title or buf:
        yield current_title, "\n".join(buf).strip("\n")


def distill_deterministic(markdown: str) -> str:
    """Pure deterministic distillation. No LLM. Keeps imperative content; drops fluff."""
    out_lines: list[str] = []
    in_code = False

    for title, body in _section_iter(markdown):
        t = title.strip().lower()
        if t in DROP_SECTION_TITLES:
            continue
        keep_section = (t in SECTION_KEEP_TITLES) or (not title)
        out_lines.append(f"## {title}" if title else "")
        for line in body.splitlines():
            if CODE_FENCE_RE.match(line):
                in_code = not in_code
                out_lines.append(line)
                continue
            if in_code:
                out_lines.append(line)
                continue
            if keep_section:
                out_lines.append(line)
                continue
            # In other sections, keep only imperative bullets, checklists, headings.
            if CHECKLIST_RE.match(line) or KEEP_BULLET_RE.match(line):
                out_lines.append(line)
            elif line.startswith("### "):
                out_lines.append(line)

    # Collapse 3+ blank lines.
    text = "\n".join(out_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def summarize(markdown: str, *, target_words: int = 300) -> str:
    """First N useful words, leading with purpose/activation if findable."""
    # Strip frontmatter remnants
    body = markdown
    # Prefer 'Purpose' or 'When to' sections first.
    preferred = ""
    for title, sect in _section_iter(body):
        t = title.strip().lower()
        if t in ("purpose", "when to use", "when to activate"):
            preferred = sect.strip() + "\n\n"
            break
    rest = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    rest = re.sub(r"^#.*$", "", rest, flags=re.MULTILINE)
    words = (preferred + rest).split()
    if len(words) <= target_words:
        return " ".join(words).strip() + "\n"
    return " ".join(words[:target_words]).strip() + "…\n"


# ---------------------------------------------------------------------------
# Import (single + dir)
# ---------------------------------------------------------------------------

def registry_root() -> Path:
    return config.home_dir() / "skill-registry" / "skills"


def import_one(
    source_path: Path,
    *,
    source_repo: str | None = None,
    source_id: int | None = None,
    trust_level: str = "unreviewed",
    enabled: bool = False,
) -> SkillMetadata:
    """Import a single source file into the normalized registry layout.

    `original.md` is written once and never overwritten on re-import.
    """
    source_path = Path(source_path).expanduser().resolve()
    raw = source_path.read_text(encoding="utf-8", errors="replace")
    fm, body = utils.split_frontmatter(raw)
    meta = extract_metadata(
        fm,
        source_path,
        source_repo=source_repo,
        source_path=str(source_path),
        body=body,
        trust_level=trust_level,
        enabled=enabled,
    )

    dst = registry_root() / meta.id
    dst.mkdir(parents=True, exist_ok=True)

    original = dst / "original.md"
    if not original.exists():
        original.write_text(raw, encoding="utf-8")

    (dst / "distilled.md").write_text(distill_deterministic(body), encoding="utf-8")
    (dst / "summary.md").write_text(summarize(body), encoding="utf-8")
    utils.write_yaml(dst / "metadata.yaml", meta.model_dump(mode="json"))
    (dst / "source.json").write_text(json.dumps({
        "source_repo": source_repo,
        "source_path": str(source_path),
        "imported_at": datetime.utcnow().isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    if not (dst / "score.json").exists():
        (dst / "score.json").write_text(json.dumps({
            "priority": meta.priority,
            "useful_count": 0,
            "not_useful_count": 0,
        }, indent=2), encoding="utf-8")

    # DB upsert
    conn = db.connect()
    db.init_schema(conn)
    conn.execute(
        """INSERT INTO skills
            (source_id, skill_id, type, name, description, category, tags_json, metadata_json,
             original_path, distilled_path, summary_path, enabled, trust_level)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(skill_id) DO UPDATE SET
                type = excluded.type, name = excluded.name, description = excluded.description,
                category = excluded.category, tags_json = excluded.tags_json,
                metadata_json = excluded.metadata_json,
                original_path = excluded.original_path,
                distilled_path = excluded.distilled_path,
                summary_path = excluded.summary_path,
                trust_level = excluded.trust_level,
                updated_at = CURRENT_TIMESTAMP""",
        (
            source_id, meta.id, meta.type, meta.name, meta.description, meta.category,
            json.dumps(meta.tags), json.dumps(meta.model_dump(mode="json")),
            str(original), str(dst / "distilled.md"), str(dst / "summary.md"),
            1 if enabled else 0, trust_level,
        ),
    )
    conn.commit()
    conn.close()
    return meta


def import_dir(
    source_dir: Path,
    *,
    source_repo: str | None = None,
    source_id: int | None = None,
    trust_level: str = "unreviewed",
    enabled: bool = False,
) -> list[SkillMetadata]:
    """Walk a directory and import every markdown file (SKILL.md preferred, plus flat .md)."""
    source_dir = Path(source_dir).expanduser().resolve()
    imported: list[SkillMetadata] = []
    seen: set[Path] = set()

    # First: SKILL.md anywhere (ECC / alirezarezvani per-folder skills).
    for p in source_dir.rglob("SKILL.md"):
        if p in seen:
            continue
        seen.add(p)
        imported.append(import_one(
            p, source_repo=source_repo, source_id=source_id,
            trust_level=trust_level, enabled=enabled,
        ))

    # Then: any other markdown files (VoltAgent flat agents, root rules, etc.).
    for p in source_dir.rglob("*.md"):
        if p in seen or p.name in {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE.md"}:
            continue
        seen.add(p)
        imported.append(import_one(
            p, source_repo=source_repo, source_id=source_id,
            trust_level=trust_level, enabled=enabled,
        ))

    return imported


def import_seeds() -> list[SkillMetadata]:
    """Import the bundled seed skills with local-curated trust + enabled by default."""
    seeds = utils.package_root() / "seeds" / "seed_skills"
    return import_dir(
        seeds,
        source_repo="johnstudio:seeds",
        trust_level="local-curated",
        enabled=True,
    )
