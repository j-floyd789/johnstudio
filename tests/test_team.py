"""Tests for team-mode catalog + plan parsing."""
from __future__ import annotations

import pytest

from johnstudio.team import (
    PlanError,
    SEEDS_ROLES_DIR,
    load_role_catalog,
    parse_team_plan,
    roles_by_vp,
)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_catalog_loads_all_28_roles():
    catalog = load_role_catalog()
    assert len(catalog) == 28, f"expected 28 roles, got {len(catalog)}"
    by_vp = roles_by_vp(catalog)
    assert len(by_vp["claude_vp"]) == 13
    assert len(by_vp["codex_vp"]) == 8
    assert len(by_vp["gemini_vp"]) == 7


def test_lead_planner_is_in_gemini_vp_and_readonly():
    catalog = load_role_catalog()
    assert "lead-planner" in catalog
    r = catalog["lead-planner"]
    assert r.vp == "gemini_vp"
    assert r.provider == "gemini"
    assert r.can_edit is False
    # Body should mention TEAM_PLAN.md
    assert "TEAM_PLAN.md" in r.system_prompt


def test_every_role_has_a_nontrivial_system_prompt():
    catalog = load_role_catalog()
    for name, role in catalog.items():
        assert len(role.system_prompt) > 200, f"{name} system prompt too short"
        assert role.description, f"{name} missing description"


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------

VALID_PLAN = """\
# Team plan for task-0001

## Summary
Build a small whiteboard demo backend and tests.

## Team
```yaml
claude_vp:
  - role: backend-developer
    brief: "Implement /api/rooms CRUD against FastAPI."
    output: "app.py + RESULT.md on ai/task-0001/backend"
  - role: code-reviewer
    brief: "Review the backend implementer's diff for input-validation gaps."
    output: "REVIEW_backend.md"
codex_vp:
  - role: test-automator
    brief: "Write pytest tests for /api/rooms happy path and 422 errors."
    output: "tests/test_rooms.py"
  - role: security-auditor
    brief: "Threat-model the new endpoints."
    output: "SECURITY_REVIEW.md"
gemini_vp:
  - role: technical-writer
    brief: "Update README with the new endpoints."
    output: "README.md"
```

## Cross-team review
```yaml
- reviewer: code-reviewer (claude_vp)
  reads: ["SECURITY_REVIEW.md", "tests/test_rooms.py"]
```

## Acceptance criteria
- pytest passes
- README mentions /api/rooms
- no Sev-1 findings in SECURITY_REVIEW.md
"""


def test_parse_valid_plan():
    catalog = load_role_catalog()
    plan = parse_team_plan(VALID_PLAN, catalog=catalog)
    assert plan.summary.startswith("Build a small whiteboard")
    assert len(plan.assignments) == 5
    by_vp = plan.by_vp()
    assert len(by_vp["claude_vp"]) == 2
    assert len(by_vp["codex_vp"]) == 2
    assert len(by_vp["gemini_vp"]) == 1
    assert plan.assignments[0].role == "backend-developer"
    assert "/api/rooms" in plan.assignments[0].brief
    assert len(plan.cross_review) == 1
    assert plan.cross_review[0].reviewer == "code-reviewer (claude_vp)"
    assert set(plan.cross_review[0].reads) == {"SECURITY_REVIEW.md", "tests/test_rooms.py"}
    assert "pytest passes" in plan.acceptance_criteria


def test_plan_validates_role_exists():
    catalog = load_role_catalog()
    bad = VALID_PLAN.replace("backend-developer", "totally-fake-role")
    with pytest.raises(PlanError) as ei:
        parse_team_plan(bad, catalog=catalog)
    assert "totally-fake-role" in str(ei.value)


def test_plan_validates_vp_matches_role():
    catalog = load_role_catalog()
    # Put a claude_vp role (backend-developer) under codex_vp.
    bad = """# Plan
## Summary
Misplaced role.
## Team
```yaml
codex_vp:
  - role: backend-developer
    brief: "Wrong VP."
    output: "out.md"
```
"""
    # A role under the wrong VP is now AUTO-CORRECTED (names are globally
    # unique), not rejected — so the plan parses and the assignment carries the
    # role's true catalog VP.
    plan = parse_team_plan(bad, catalog=catalog)
    bd = [a for a in plan.assignments if a.role == "backend-developer"]
    assert bd, "backend-developer assignment should survive auto-correction"
    assert bd[0].vp == catalog["backend-developer"].vp == "claude_vp"


def test_plan_rejects_output_collisions():
    catalog = load_role_catalog()
    bad = VALID_PLAN.replace(
        'output: "REVIEW_backend.md"',
        'output: "app.py + RESULT.md on ai/task-0001/backend"',
    )
    with pytest.raises(PlanError) as ei:
        parse_team_plan(bad, catalog=catalog)
    assert "collides" in str(ei.value).lower()


def test_plan_requires_summary():
    bad = VALID_PLAN.replace("## Summary\nBuild a small whiteboard demo backend and tests.\n", "")
    with pytest.raises(PlanError) as ei:
        parse_team_plan(bad)
    assert "summary" in str(ei.value).lower()


def test_plan_requires_team_yaml_block():
    bad = VALID_PLAN.replace("```yaml\nclaude_vp:", "claude_vp:").replace("```\n\n## Cross-team", "\n\n## Cross-team")
    with pytest.raises(PlanError):
        parse_team_plan(bad)


def test_plan_rejects_unknown_vp():
    bad = VALID_PLAN.replace("claude_vp:", "totally_fake_vp:")
    with pytest.raises(PlanError) as ei:
        parse_team_plan(bad)
    assert "totally_fake_vp" in str(ei.value)


# ---------------------------------------------------------------------------
# Standing rules (deterministic plan augmentation)
# ---------------------------------------------------------------------------

from johnstudio.team import apply_standing_rules, load_standing_rules


def test_standing_rules_file_loads():
    rules = load_standing_rules()
    assert len(rules) >= 1
    assert all("trigger" in r and "add" in r for r in rules)


def test_apply_standing_rules_does_not_duplicate():
    """If a rule's role is already in the plan, it's not added again."""
    from johnstudio.team import parse_team_plan, load_role_catalog
    catalog = load_role_catalog()
    # VALID_PLAN already has code-reviewer; the "always" rule shouldn't add another.
    plan = parse_team_plan(VALID_PLAN, catalog=catalog)
    augmented = apply_standing_rules(plan, task_text="x", catalog=catalog)
    code_reviewer_count = sum(1 for a in augmented.assignments if a.role == "code-reviewer")
    assert code_reviewer_count == 1


def test_apply_standing_rules_no_duplicate_roles_overall():
    """No role identity appears twice after augmentation, even when the
    plan already contains a role a file/keyword-triggered rule would add.

    Reproduces the off-by-one in the bug report: a plan that already
    names `test-automator` AND has a *.py output (which the
    `tests-for-python` rule keys on) must NOT yield two test-automators.
    """
    from collections import Counter
    from johnstudio.team import parse_team_plan, load_role_catalog
    catalog = load_role_catalog()
    plan_md = """# Plan
## Summary
Add a python feature; the planner already scheduled tests.
## Team
```yaml
claude_vp:
  - role: backend-developer
    brief: "Implement app.py"
    output: "app.py"
codex_vp:
  - role: test-automator
    brief: "Write pytest tests for app.py"
    output: "tests/test_app.py"
```
"""
    plan = parse_team_plan(plan_md, catalog=catalog)
    pre_count = len(plan.assignments)
    augmented = apply_standing_rules(
        plan, task_text="add a new feature", catalog=catalog,
    )
    counts = Counter(a.role for a in augmented.assignments)
    # test-automator was already present (matches the *.py rule) — not re-added.
    assert counts["test-automator"] == 1, counts
    # Every role is unique: idempotent augmentation never duplicates a role.
    assert all(n == 1 for n in counts.values()), f"duplicate role(s): {counts}"
    # Augmentation is additive-or-noop: at least the always-on code-reviewer
    # got added (it wasn't in the plan), but test-automator did not.
    assert counts["code-reviewer"] == 1
    assert len(augmented.assignments) == pre_count + (len(counts) - pre_count)


def test_apply_standing_rules_is_repeatedly_idempotent():
    """Applying the rules twice yields the same assignment set (no growth)."""
    from johnstudio.team import parse_team_plan, load_role_catalog
    catalog = load_role_catalog()
    plan = parse_team_plan(VALID_PLAN, catalog=catalog)
    once = apply_standing_rules(plan, task_text="add a new endpoint", catalog=catalog)
    twice = apply_standing_rules(once, task_text="add a new endpoint", catalog=catalog)
    assert [a.role for a in once.assignments] == [a.role for a in twice.assignments]


def test_standing_rule_mentions_in_task_adds_security_auditor():
    """A user task that mentions 'auth' should auto-add security-auditor."""
    from johnstudio.team import parse_team_plan, load_role_catalog
    catalog = load_role_catalog()
    minimal_plan = """# Plan
## Summary
Add a login form.
## Team
```yaml
claude_vp:
  - role: backend-developer
    brief: "Add login route."
    output: "login.py"
```
"""
    plan = parse_team_plan(minimal_plan, catalog=catalog)
    augmented = apply_standing_rules(
        plan, task_text="Add user authentication with passwords.", catalog=catalog,
    )
    role_names = {a.role for a in augmented.assignments}
    assert "security-auditor" in role_names


def test_standing_rule_file_pattern_adds_test_automator():
    """A plan with *.py outputs should auto-add test-automator."""
    from johnstudio.team import parse_team_plan, load_role_catalog
    catalog = load_role_catalog()
    minimal_plan = """# Plan
## Summary
Add a feature.
## Team
```yaml
claude_vp:
  - role: backend-developer
    brief: "Implement app.py."
    output: "app.py"
```
"""
    plan = parse_team_plan(minimal_plan, catalog=catalog)
    augmented = apply_standing_rules(
        plan, task_text="add something", catalog=catalog,
    )
    role_names = {a.role for a in augmented.assignments}
    assert "test-automator" in role_names


def test_standing_rule_with_no_trigger_match_is_noop():
    """If nothing in the task or files triggers, only `always:true` rules fire."""
    from johnstudio.team import parse_team_plan, load_role_catalog
    catalog = load_role_catalog()
    minimal_plan = """# Plan
## Summary
A tiny markdown-only change.
## Team
```yaml
gemini_vp:
  - role: technical-writer
    brief: "Update one paragraph."
    output: "GUIDE.md"
```
"""
    plan = parse_team_plan(minimal_plan, catalog=catalog)
    augmented = apply_standing_rules(plan, task_text="fix typo", catalog=catalog)
    role_names = {a.role for a in augmented.assignments}
    # always-have-code-reviewer should still fire
    assert "code-reviewer" in role_names
    # security-auditor shouldn't (no auth/payment keywords, no sensitive files)
    assert "security-auditor" not in role_names
