from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from johnstudio import skill_importer as si


# ---------------------------------------------------------------------------
# Fixtures: synthesized mini-repos in three upstream layouts
# ---------------------------------------------------------------------------

@pytest.fixture
def voltagent_repo(tmp_path):
    root = tmp_path / "voltagent"
    cat = root / "categories" / "02-language-specialists"
    cat.mkdir(parents=True)
    (cat / "react-specialist.md").write_text(
        "---\n"
        "name: react-specialist\n"
        "description: React 18+ patterns\n"
        "tools: Read, Write, Edit\n"
        "model: sonnet\n"
        "---\n\n"
        "# React Specialist\n\n## When to Use\nReact tasks.\n\n## Checklist\n- must use Server Components\n"
    )
    return root


@pytest.fixture
def ecc_repo(tmp_path):
    root = tmp_path / "ecc"
    sk = root / "skills" / "tdd-workflow"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\n"
        "name: tdd-workflow\n"
        "description: Red-green-refactor\n"
        "tags: [testing, tdd]\n"
        "confidence: high\n"
        "---\n\n"
        "# TDD\n\n## Workflow\n1. Red\n2. Green\n3. Refactor\n"
    )
    ag = root / "agents"
    ag.mkdir(parents=True)
    (ag / "code-reviewer.md").write_text(
        "---\nname: code-reviewer\ndescription: Reviews diffs\n---\n\n# Code Reviewer\n"
    )
    rules = root / "rules" / "typescript"
    rules.mkdir(parents=True)
    (rules / "no-any.md").write_text(
        "---\ndescription: Forbid any\nglobs: ['**/*.ts','**/*.tsx']\nalwaysApply: true\n---\n"
        "# No Any\n\n- never use `any`\n"
    )
    return root


@pytest.fixture
def alirezarezvani_repo(tmp_path):
    root = tmp_path / "ali"
    sk = root / "engineering" / "git-worktree-manager"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\n"
        "name: Git Worktree Manager\n"
        "description: Lifecycle of git worktrees\n"
        "domain: engineering\n"
        "tier: advanced\n"
        "---\n\n"
        "# Git Worktree Manager\n\n## When to Use\nParallel agent work.\n"
    )
    pers = root / "agents" / "personas"
    pers.mkdir(parents=True)
    (pers / "startup-cto.md").write_text(
        "---\nname: Startup CTO\ndescription: Persona\n---\n\n# Startup CTO\n"
    )
    return root


# ---------------------------------------------------------------------------
# Unit-level
# ---------------------------------------------------------------------------

def test_detect_type():
    assert si.detect_type(Path("foo/CLAUDE.md")) == "rule"
    assert si.detect_type(Path("foo/AGENTS.md")) == "rule"
    assert si.detect_type(Path("foo/bar.mdc")) == "rule"
    assert si.detect_type(Path("skills/x/SKILL.md")) == "skill"
    assert si.detect_type(Path("agents/x.md")) == "agent"
    assert si.detect_type(Path("categories/01-core/api-designer.md")) == "agent"
    assert si.detect_type(Path("commands/plan.md")) == "command"
    assert si.detect_type(Path("hooks/pretool.md")) == "hook"
    assert si.detect_type(Path("anywhere/else.md")) == "skill"


def test_as_str_list_handles_shapes():
    assert si._as_str_list(None) == []
    assert si._as_str_list("a, b , c") == ["a", "b", "c"]
    assert si._as_str_list(["a", "b"]) == ["a", "b"]
    assert si._as_str_list([{"name": "alpha"}, "beta"]) == ["alpha", "beta"]


def test_distill_keeps_imperative_bullets():
    src = (
        "## When to Use\nFoo.\n\n"
        "## Sponsor\nBuy stickers!\n\n"
        "## Random\nbla bla bla\n- must always do X\n- prefer Y\n- just a fact\n"
    )
    out = si.distill_deterministic(src)
    assert "When to Use" in out
    assert "Sponsor" not in out
    assert "must always do X" in out
    assert "prefer Y" in out
    assert "just a fact" not in out


def test_summarize_lead_with_purpose():
    src = "## Purpose\nDoes the thing.\n\n## Other\nirrelevant " + "filler " * 200
    out = si.summarize(src, target_words=20)
    assert out.lower().startswith("does the thing")


# ---------------------------------------------------------------------------
# Integration: import_dir against three layouts
# ---------------------------------------------------------------------------

def _read_registry_meta(jh_home, skill_id) -> dict:
    p = jh_home / "skill-registry" / "skills" / skill_id / "metadata.yaml"
    return yaml.safe_load(p.read_text())


def test_import_voltagent_layout(jh_home, voltagent_repo):
    imported = si.import_dir(voltagent_repo, source_repo="VoltAgent/sub")
    assert any(m.id == "react-specialist" for m in imported)
    meta = _read_registry_meta(jh_home, "react-specialist")
    assert meta["type"] == "agent"
    assert meta["category"] == "frontend"
    assert meta["enabled"] is False
    assert meta["trust_level"] == "unreviewed"
    # original.md preserved
    orig = jh_home / "skill-registry" / "skills" / "react-specialist" / "original.md"
    assert "name: react-specialist" in orig.read_text()


def test_import_ecc_layout(jh_home, ecc_repo):
    imported = si.import_dir(ecc_repo, source_repo="affaan-m/ECC")
    ids = {m.id for m in imported}
    assert {"tdd-workflow", "code-reviewer", "no-any"}.issubset(ids)
    tdd = _read_registry_meta(jh_home, "tdd-workflow")
    assert tdd["type"] == "skill"
    assert tdd["category"] == "testing"
    rev = _read_registry_meta(jh_home, "code-reviewer")
    assert rev["type"] == "agent"
    rule = _read_registry_meta(jh_home, "no-any")
    assert rule["type"] == "rule"
    assert rule["file_patterns"] == ["**/*.ts", "**/*.tsx"]


def test_import_alirezarezvani_layout(jh_home, alirezarezvani_repo):
    imported = si.import_dir(alirezarezvani_repo, source_repo="alirezarezvani/claude-skills")
    ids = {m.id for m in imported}
    assert "git-worktree-manager" in ids
    assert "startup-cto" in ids
    cto = _read_registry_meta(jh_home, "startup-cto")
    assert cto["type"] == "agent"
    assert cto["category"] in ("product-business", "general-guidance", "agent-orchestration")


def test_import_one_preserves_original_on_reimport(jh_home, voltagent_repo):
    src = voltagent_repo / "categories" / "02-language-specialists" / "react-specialist.md"
    si.import_one(src, source_repo="r", trust_level="unreviewed")
    orig = jh_home / "skill-registry" / "skills" / "react-specialist" / "original.md"
    sentinel = "EDITED-BY-USER"
    orig.write_text(orig.read_text() + f"\n<!-- {sentinel} -->\n")
    si.import_one(src, source_repo="r", trust_level="unreviewed")
    assert sentinel in orig.read_text()


def test_import_seeds(jh_home):
    imported = si.import_seeds()
    ids = {m.id for m in imported}
    assert "terminal-stub" in ids
    assert "security-auditor" in ids
    assert "test-automator" in ids
    # Seeds should be enabled + local-curated.
    sec = _read_registry_meta(jh_home, "security-auditor")
    assert sec["enabled"] is True
    assert sec["trust_level"] == "local-curated"
