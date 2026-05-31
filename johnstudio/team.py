"""Team-mode orchestration: role catalog + TEAM_PLAN parsing.

RFC 0001 reorganized JohnStudio's worker pool around three "VPs" — Claude
VP (Engineering), Codex VP (Quality), Gemini VP (Research/Strategy) — with
20 specialist roles distributed across them. The lead planner (a Gemini
VP role) reads the task and writes `TEAM_PLAN.md` listing which
specialists run, with what brief, producing what artifact. This module is
the deterministic half:

- `Role` and `load_role_catalog()` — read the markdown role files (YAML
  frontmatter + system-prompt body) from `seeds/roles/<vp>/<name>.md`.
- `TeamPlan`, `Assignment`, and `parse_team_plan()` — read the planner's
  output and validate it (role names exist, no path collisions, every VP
  named is real).

The plan format is documented in RFC 0001 and in
`seeds/roles/gemini_vp/lead-planner.md`. We keep YAML inside fenced
markdown so the planner can write a human-readable doc and the
orchestrator can still parse it deterministically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml


# ---------------------------------------------------------------------------
# Role catalog
# ---------------------------------------------------------------------------

VPs = ("claude_vp", "codex_vp", "gemini_vp")
VPLiteral = Literal["claude_vp", "codex_vp", "gemini_vp"]

SEEDS_ROLES_DIR = Path(__file__).resolve().parent.parent / "seeds" / "roles"


@dataclass
class Role:
    name: str
    vp: VPLiteral
    provider: str          # claude | codex | gemini
    description: str
    can_edit: bool
    model: str
    tools: list[str]
    system_prompt: str      # the markdown body, sans frontmatter
    path: Path
    # If true, this role's spawned CLI is allowed to invoke the `Task`
    # tool (Claude Code subagents like Explore, Plan, implementer,
    # verifier). Defaults False to preserve RFC 0001 §Non-goals; flip
    # to True per-role for complex implementer/researcher roles where
    # subagent decomposition is a real speedup. See team.py guard.
    can_spawn_subagents: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name, "vp": self.vp, "provider": self.provider,
            "description": self.description, "can_edit": self.can_edit,
            "model": self.model, "tools": self.tools,
            "can_spawn_subagents": self.can_spawn_subagents,
            "path": str(self.path),
        }


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _parse_role_file(p: Path) -> Role:
    raw = p.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(f"{p}: missing YAML frontmatter")
    meta_raw, body = m.groups()
    meta = yaml.safe_load(meta_raw) or {}
    required = ("name", "vp", "provider", "can_edit", "model")
    missing = [k for k in required if k not in meta]
    if missing:
        raise ValueError(f"{p}: missing frontmatter keys {missing}")
    if meta["vp"] not in VPs:
        raise ValueError(f"{p}: vp must be one of {VPs}, got {meta['vp']!r}")
    # An empty `model:` field is YAML null → Python None. Stringifying that
    # gives "None" (the literal four-letter word), which Codex/Gemini will
    # then pass as `-m None` and 400. Use empty-string instead so the
    # worker's `if cfg.model` guard skips the `-m` flag entirely and the
    # underlying CLI picks its account-default model.
    model_val = meta["model"]
    model_str = "" if model_val is None else str(model_val)
    return Role(
        name=str(meta["name"]),
        vp=str(meta["vp"]),
        provider=str(meta["provider"]),
        description=str(meta.get("description") or ""),
        can_edit=bool(meta["can_edit"]),
        model=model_str,
        tools=list(meta.get("tools") or []),
        can_spawn_subagents=bool(meta.get("can_spawn_subagents", False)),
        system_prompt=body.lstrip(),
        path=p,
    )


def load_role_catalog(root: Path | None = None) -> dict[str, Role]:
    """Read every `seeds/roles/<vp>/*.md` and return a {name: Role} map.

    Names are unique across all VPs — duplicates raise.

    **Subagent guard:** a role may declare `Task` in its tools list ONLY
    if it also sets `can_spawn_subagents: true`. RFC 0001 §Non-goals
    keeps the default closed (nested LLM orchestration collapses in
    research). The opt-in is per-role so we can enable subagent
    decomposition for specific implementer/researcher roles where it's
    a clear win, without opening the floodgates for the whole catalog.
    """
    root = root or SEEDS_ROLES_DIR
    if not root.exists():
        return {}
    catalog: dict[str, Role] = {}
    for vp in VPs:
        vp_dir = root / vp
        if not vp_dir.exists():
            continue
        for f in sorted(vp_dir.glob("*.md")):
            role = _parse_role_file(f)
            if role.vp != vp:
                raise ValueError(
                    f"{f}: frontmatter vp={role.vp!r} doesn't match folder {vp!r}"
                )
            if role.name in catalog:
                prev = catalog[role.name].path
                raise ValueError(f"duplicate role name {role.name!r}: {prev} and {f}")
            # Subagent guard: Task tool requires explicit opt-in.
            forbidden = {"Task", "Agent"}
            declared_forbidden = forbidden.intersection(role.tools or [])
            if declared_forbidden and not role.can_spawn_subagents:
                raise ValueError(
                    f"{f}: role {role.name!r} declares {sorted(declared_forbidden)!r} "
                    f"but `can_spawn_subagents` is false. Set "
                    f"`can_spawn_subagents: true` in frontmatter to opt in."
                )
            catalog[role.name] = role
    return catalog


def roles_by_vp(catalog: dict[str, Role]) -> dict[str, list[Role]]:
    out: dict[str, list[Role]] = {vp: [] for vp in VPs}
    for r in catalog.values():
        out[r.vp].append(r)
    return out


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------

@dataclass
class Assignment:
    """One specialist named in a TEAM_PLAN."""
    role: str       # role name (must exist in catalog)
    vp: VPLiteral   # which VP this assignment lives under
    brief: str      # one-sentence task description
    output: str     # the artifact path this assignment is expected to produce

    def to_dict(self) -> dict:
        return {"role": self.role, "vp": self.vp, "brief": self.brief, "output": self.output}


@dataclass
class CrossReview:
    """Optional cross-VP review entry from the plan."""
    reviewer: str         # role name (and parenthesized vp)
    reads: list[str]      # output paths the reviewer should read

    def to_dict(self) -> dict:
        return {"reviewer": self.reviewer, "reads": list(self.reads)}


@dataclass
class TeamPlan:
    summary: str
    assignments: list[Assignment] = field(default_factory=list)
    cross_review: list[CrossReview] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    source_path: Path | None = None

    def by_vp(self) -> dict[str, list[Assignment]]:
        out: dict[str, list[Assignment]] = {vp: [] for vp in VPs}
        for a in self.assignments:
            out[a.vp].append(a)
        return out

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "assignments": [a.to_dict() for a in self.assignments],
            "cross_review": [c.to_dict() for c in self.cross_review],
            "acceptance_criteria": list(self.acceptance_criteria),
            "source_path": str(self.source_path) if self.source_path else None,
        }


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Section headers we look for. Match `## Foo` and `## foo` and `## Foo:` etc.
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# Fenced YAML block. Accepts ```yaml or ``` followed by yaml. Captures body.
_YAML_FENCE_RE = re.compile(
    r"```(?:yaml|yml)?\s*\n(.*?)\n```", re.DOTALL,
)


class PlanError(ValueError):
    """Raised when TEAM_PLAN.md is malformed or references unknown roles."""


def parse_team_plan(
    md: str, *, catalog: dict[str, Role] | None = None, source_path: Path | None = None
) -> TeamPlan:
    """Parse a TEAM_PLAN.md document.

    Validates against `catalog` if provided:
    - Every assigned role name must exist.
    - Each assignment's `vp` must match the role's catalog vp.
    - No two assignments may share the same `output` path.

    Raises PlanError on any of the above.
    """
    sections = _split_sections(md)

    summary = sections.get("summary", "").strip()
    if not summary:
        raise PlanError("plan is missing a '## Summary' section")

    # Team block — YAML grouped by VP.
    team_md = sections.get("team", "")
    yaml_match = _YAML_FENCE_RE.search(team_md)
    if not yaml_match:
        raise PlanError("plan is missing a fenced ```yaml``` block under '## Team'")
    try:
        team_yaml = yaml.safe_load(yaml_match.group(1)) or {}
    except yaml.YAMLError as e:
        raise PlanError(f"team YAML failed to parse: {e}")
    if not isinstance(team_yaml, dict):
        raise PlanError("team YAML must be a mapping {vp: [assignments...]}")

    assignments: list[Assignment] = []
    seen_outputs: set[str] = set()
    for vp, entries in team_yaml.items():
        if vp not in VPs:
            raise PlanError(f"unknown vp {vp!r} (must be one of {VPs})")
        if not isinstance(entries, list):
            raise PlanError(f"{vp}: expected a list of assignments, got {type(entries).__name__}")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise PlanError(f"{vp}[{i}]: expected a mapping, got {type(entry).__name__}")
            for k in ("role", "brief", "output"):
                if k not in entry:
                    raise PlanError(f"{vp}[{i}]: missing '{k}'")
            a = Assignment(
                role=str(entry["role"]).strip(),
                vp=vp, brief=str(entry["brief"]).strip(),
                output=str(entry["output"]).strip(),
            )
            if catalog is not None:
                if a.role not in catalog:
                    raise PlanError(
                        f"{vp}[{i}]: role {a.role!r} is not in the catalog. "
                        f"Known: {sorted(catalog)}"
                    )
                if catalog[a.role].vp != a.vp:
                    # Auto-correct rather than reject: role names are globally
                    # unique, so a planner naming the wrong VP is a harmless typo.
                    # Forcing a full replan over it wasted a ~3-min planning cycle.
                    a = Assignment(
                        role=a.role, vp=catalog[a.role].vp,
                        brief=a.brief, output=a.output,
                    )
            if a.output in seen_outputs:
                raise PlanError(
                    f"{vp}[{i}]: output {a.output!r} collides with an earlier assignment"
                )
            seen_outputs.add(a.output)
            assignments.append(a)

    if not assignments:
        raise PlanError("plan has no assignments")

    # Cross-team review (optional).
    cross_review: list[CrossReview] = []
    cr_md = sections.get("cross-team review", sections.get("cross team review", ""))
    if cr_md:
        m = _YAML_FENCE_RE.search(cr_md)
        if m:
            try:
                cr_yaml = yaml.safe_load(m.group(1)) or []
            except yaml.YAMLError as e:
                raise PlanError(f"cross-team review YAML failed to parse: {e}")
            if not isinstance(cr_yaml, list):
                raise PlanError("cross-team review YAML must be a list")
            for i, entry in enumerate(cr_yaml):
                if not isinstance(entry, dict):
                    raise PlanError(f"cross-team review[{i}]: expected a mapping")
                cross_review.append(CrossReview(
                    reviewer=str(entry.get("reviewer", "")).strip(),
                    reads=[str(x).strip() for x in (entry.get("reads") or [])],
                ))

    # Acceptance criteria — bullet list.
    ac_md = sections.get("acceptance criteria", "")
    acceptance = [
        line.lstrip("-* ").strip()
        for line in ac_md.splitlines()
        if line.strip().startswith(("-", "*"))
    ]

    return TeamPlan(
        summary=summary,
        assignments=assignments,
        cross_review=cross_review,
        acceptance_criteria=acceptance,
        source_path=source_path,
    )


def _split_sections(md: str) -> dict[str, str]:
    """Return {lowercase-heading: body} for every `## …` section."""
    out: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(md))
    for i, m in enumerate(matches):
        title = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out[title] = md[start:end].strip()
    return out


# ---------------------------------------------------------------------------
# Standing rules — deterministic plan augmentation
# ---------------------------------------------------------------------------

STANDING_RULES_PATH = Path(__file__).resolve().parent.parent / "seeds" / "standing_rules.yaml"


def load_standing_rules(path: Path | None = None) -> list[dict]:
    """Read seeds/standing_rules.yaml. Returns empty list if the file
    doesn't exist (older installs)."""
    path = path or STANDING_RULES_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("rules") or [])


def apply_standing_rules(
    plan: TeamPlan, *, task_text: str, catalog: dict[str, Role],
    rules: list[dict] | None = None,
) -> TeamPlan:
    """Augment a TeamPlan with any standing-rule assignments whose role
    isn't already covered. Idempotent: rules whose role is already in
    the plan are skipped.

    A rule matches if ANY of its trigger predicates matches:
    - `any_file_pattern`: at least one glob matches at least one
      existing assignment's `output` path.
    - `mentions_in_task`: at least one keyword (case-insensitive) is a
      substring of the user's task text.
    - `always: true`: matches unconditionally.
    """
    import fnmatch as _fn
    rules = rules if rules is not None else load_standing_rules()
    if not rules:
        return plan
    # Dedupe on the role NAME — the same identity the rest of the system
    # uses (Assignment.role, catalog keys, the planner's YAML). Normalize
    # whitespace so a planner-authored " test-automator " still matches a
    # rule's "test-automator" and we don't launch the role twice
    # (the off-by-one that yielded 10 specialists when 9 were expected).
    existing_roles = {a.role.strip() for a in plan.assignments}
    existing_outputs = {a.output for a in plan.assignments}
    task_lower = (task_text or "").lower()

    augmented = list(plan.assignments)
    for idx, rule in enumerate(rules):
        add = rule.get("add") or {}
        role_name = str(add.get("role") or "").strip()
        if not role_name or role_name not in catalog:
            continue
        if role_name in existing_roles:
            # Planner already scheduled this role — adding it again would
            # double-spec it. Skip (idempotent).
            continue
        trigger = rule.get("trigger") or {}
        matched = False
        if trigger.get("always") is True:
            matched = True
        if not matched:
            patterns = trigger.get("any_file_pattern") or []
            for pat in patterns:
                if any(_fn.fnmatch(a.output, pat) for a in plan.assignments):
                    matched = True
                    break
        if not matched:
            mentions = trigger.get("mentions_in_task") or []
            for kw in mentions:
                if str(kw).lower() in task_lower:
                    matched = True
                    break
        if not matched:
            continue
        role = catalog[role_name]
        brief = str(add.get("brief") or f"Standing rule: cover {role_name}.")
        output = str(add.get("output") or f"STANDING_{role_name}_{idx}.md")
        # Resolve `<i>` placeholder in output if present.
        output = output.replace("<i>", str(idx))
        # Don't collide with existing outputs.
        if output in existing_outputs:
            output = f"STANDING_{role_name}_{idx}.md"
        augmented.append(Assignment(
            role=role.name, vp=role.vp, brief=brief, output=output,
        ))
        existing_roles.add(role.name)
        existing_outputs.add(output)
    return TeamPlan(
        summary=plan.summary,
        assignments=augmented,
        cross_review=plan.cross_review,
        acceptance_criteria=plan.acceptance_criteria,
        source_path=plan.source_path,
    )
