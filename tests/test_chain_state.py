from __future__ import annotations

import pytest

from johnstudio.chain import (
    Phase,
    PhaseRow,
    Verdict,
    decide_next,
    parse_verdict,
)


def _row(phase: Phase, round: int = 0) -> PhaseRow:
    return PhaseRow(
        id=1, task_id=1, phase=phase, round=round, status="running",
        artifact_path=None, verdict=None, notes=None,
        started_at=None, completed_at=None,
    )


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("md,expected", [
    ("## Verdict: approve\n", Verdict.APPROVE),
    ("## Verdict: APPROVE\n", Verdict.APPROVE),
    ("## Verdict: needs-changes\n", Verdict.NEEDS_CHANGES),
    ("## Verdict: needs changes\n", Verdict.NEEDS_CHANGES),
    ("## Verdict: NeedsChanges\n", Verdict.NEEDS_CHANGES),
    ("## Verdict: reject\n", Verdict.REJECT),
    ("## verdict approve\n", Verdict.APPROVE),  # missing colon, ok
    ("no verdict here\n", None),
    ("", None),
])
def test_parse_verdict(md, expected):
    assert parse_verdict(md) == expected


def test_parse_verdict_takes_first():
    md = "## Verdict: needs-changes\n\nlater\n\n## Verdict: approve\n"
    assert parse_verdict(md) == Verdict.NEEDS_CHANGES


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def test_rfc_drafting_waits_then_advances():
    cur = _row(Phase.RFC_DRAFTING)
    assert decide_next(cur, artifact_exists=False, artifact_verdict=None).next_phase is None
    t = decide_next(cur, artifact_exists=True, artifact_verdict=None)
    assert t.next_phase == Phase.RFC_REVIEW
    assert t.human_gate is False


def test_rfc_review_to_pending_approval():
    cur = _row(Phase.RFC_REVIEW)
    # Artifact exists but no verdict yet → stay
    assert decide_next(cur, artifact_exists=True, artifact_verdict=None).next_phase is None
    t = decide_next(cur, artifact_exists=True, artifact_verdict=Verdict.APPROVE)
    assert t.next_phase == Phase.RFC_PENDING_APPROVAL
    assert t.human_gate is True


def test_rfc_pending_approval_is_human_only():
    cur = _row(Phase.RFC_PENDING_APPROVAL)
    t = decide_next(cur, artifact_exists=True, artifact_verdict=Verdict.APPROVE)
    assert t.next_phase is None
    assert t.human_gate is True


def test_implementing_to_review_round_1():
    cur = _row(Phase.IMPLEMENTING, round=0)
    t = decide_next(cur, artifact_exists=True, artifact_verdict=None)
    assert t.next_phase == Phase.REVIEWING
    assert t.round == 1


def test_review_approve_to_pending_merge():
    cur = _row(Phase.REVIEWING, round=1)
    t = decide_next(cur, artifact_exists=True, artifact_verdict=Verdict.APPROVE)
    assert t.next_phase == Phase.PENDING_MERGE
    assert t.human_gate is True


def test_review_needs_changes_goes_to_revising():
    cur = _row(Phase.REVIEWING, round=1)
    t = decide_next(cur, artifact_exists=True, artifact_verdict=Verdict.NEEDS_CHANGES,
                    max_revise_rounds=2)
    assert t.next_phase == Phase.REVISING
    assert t.round == 1  # revising same round; round increments after revising finishes


def test_revising_back_to_review_round_increments():
    cur = _row(Phase.REVISING, round=1)
    t = decide_next(cur, artifact_exists=True, artifact_verdict=None)
    assert t.next_phase == Phase.REVIEWING
    assert t.round == 2


def test_review_needs_changes_at_max_goes_to_conflict():
    cur = _row(Phase.REVIEWING, round=2)
    t = decide_next(cur, artifact_exists=True, artifact_verdict=Verdict.NEEDS_CHANGES,
                    max_revise_rounds=2)
    assert t.next_phase == Phase.CONFLICT
    assert t.human_gate is True


def test_review_reject_terminates():
    cur = _row(Phase.REVIEWING, round=1)
    t = decide_next(cur, artifact_exists=True, artifact_verdict=Verdict.REJECT)
    assert t.next_phase == Phase.REJECTED


def test_terminals_dont_transition():
    for p in (Phase.MERGED, Phase.REJECTED):
        cur = _row(p)
        t = decide_next(cur, artifact_exists=True, artifact_verdict=Verdict.APPROVE)
        assert t.next_phase is None
