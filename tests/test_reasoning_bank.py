"""Tests for the ReasoningBank — semantic recall over past task trajectories.

Ollama is stubbed in the same way as ``test_vector_store``: we
monkeypatch ``johnstudio.embed.embed`` to a deterministic vector keyed by
``hash(text)``, so the suite runs offline.
"""
from __future__ import annotations

import hashlib
import math

import pytest

from johnstudio import embed as embed_mod
from johnstudio import reasoning_bank


def _det_vec(text: str, dim: int = 16) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [(digest[i % len(digest)] / 255.0) - 0.5 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


@pytest.fixture(autouse=True)
def _stub_embed(monkeypatch, jh_home):
    def fake_embed(text, *, model="nomic-embed-text"):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("embed() requires non-empty text")
        return _det_vec(text)
    monkeypatch.setattr(embed_mod, "embed", fake_embed)
    yield


def test_record_and_get(jh_home):
    rb = reasoning_bank.ReasoningBank(project_id=2)
    try:
        rb.record_task(
            task_number=42,
            goal="hunt edge in DFW high",
            outcome="edge_found=false",
            approach_summary="GEFS ensemble vs market — no calibration gap.",
            tags=["dfw", "arc:gefs-true"],
        )
        got = rb.get(42)
        assert got is not None
        assert got.task_number == 42
        assert got.project_id == 2
        assert got.outcome == "edge_found=false"
        assert "dfw" in got.tags
    finally:
        rb.close()


def test_record_is_idempotent(jh_home):
    rb = reasoning_bank.ReasoningBank(project_id=2)
    try:
        rb.record_task(
            task_number=7, goal="g", outcome="o1",
            approach_summary="a1", tags=["x"],
        )
        rb.record_task(
            task_number=7, goal="g", outcome="o2",
            approach_summary="a2", tags=["y"],
        )
        got = rb.get(7)
        assert got is not None
        assert got.outcome == "o2"
        assert got.approach_summary == "a2"
        assert got.tags == ["y"]
        # Only one row, so list_all returns one element.
        assert len(rb.list_all()) == 1
    finally:
        rb.close()


def test_find_priors_returns_semantic_match(jh_home):
    rb = reasoning_bank.ReasoningBank(project_id=2)
    try:
        rb.record_task(
            task_number=1,
            goal="GEFS ensemble edge probe on DFW",
            outcome="edge_found=false",
            approach_summary="Compared 31-member GEFS vs market — Brier worse than market.",
            tags=["dfw"],
        )
        rb.record_task(
            task_number=2,
            goal="Frontend redesign of dashboard",
            outcome="merged",
            approach_summary="React + Tailwind rewrite.",
            tags=["ui"],
        )
        # The embed text for task 1 includes "GEFS ensemble edge probe on DFW"
        # verbatim, so a query that is exactly the same canonical text scores 1.0.
        canon = (
            "Goal: GEFS ensemble edge probe on DFW\n"
            "Outcome: edge_found=false\n"
            "Approach: Compared 31-member GEFS vs market — Brier worse than market."
        )
        priors = rb.find_priors(canon, k=2)
        assert priors, "expected at least one prior"
        assert priors[0].task_number == 1
        assert priors[0].score == pytest.approx(1.0, abs=1e-6)
        # The frontend task is in a different vector neighbourhood; even if
        # it's returned, it must not outrank the exact match.
        if len(priors) > 1:
            assert priors[1].score <= priors[0].score
    finally:
        rb.close()


def test_find_priors_filters_by_project(jh_home):
    rb = reasoning_bank.ReasoningBank()  # no default project — must pass explicitly
    try:
        rb.record_task(
            project_id=1, task_number=10,
            goal="probe A", outcome="x", approach_summary="aa",
        )
        rb.record_task(
            project_id=2, task_number=11,
            goal="probe A", outcome="x", approach_summary="aa",
        )
        priors_all = rb.find_priors("probe A", k=5)
        assert len(priors_all) >= 2
        priors_p1 = rb.find_priors("probe A", k=5, project_id=1)
        assert all(p.project_id == 1 for p in priors_p1)
        priors_p2 = rb.find_priors("probe A", k=5, project_id=2)
        assert all(p.project_id == 2 for p in priors_p2)
    finally:
        rb.close()


def test_find_priors_empty_goal(jh_home):
    rb = reasoning_bank.ReasoningBank(project_id=1)
    try:
        assert rb.find_priors("", k=5) == []
        assert rb.find_priors("   ", k=5) == []
    finally:
        rb.close()


def test_record_rejects_missing_project_id(jh_home):
    rb = reasoning_bank.ReasoningBank()  # no default
    try:
        with pytest.raises(ValueError):
            rb.record_task(
                task_number=1, goal="g", outcome="o", approach_summary="a",
            )
    finally:
        rb.close()


def test_record_rejects_empty_fields(jh_home):
    rb = reasoning_bank.ReasoningBank(project_id=1)
    try:
        with pytest.raises(ValueError):
            rb.record_task(task_number=1, goal="", outcome="o", approach_summary="a")
        with pytest.raises(ValueError):
            rb.record_task(task_number=1, goal="g", outcome="", approach_summary="a")
        with pytest.raises(ValueError):
            rb.record_task(task_number=1, goal="g", outcome="o", approach_summary="")
    finally:
        rb.close()


def test_render_priors_section_empty():
    assert reasoning_bank.render_priors_section([]) == ""


def test_render_priors_section_capped():
    priors = [
        reasoning_bank.Prior(
            task_number=i, project_id=1, goal=f"goal {i}",
            outcome="o", approach_summary="approach line",
            tags=["t"], score=0.5,
        )
        for i in range(100)
    ]
    rendered = reasoning_bank.render_priors_section(priors, max_lines=10)
    assert rendered.count("\n") <= 9  # 10 lines -> 9 newlines
    assert rendered.startswith("## Prior similar tasks")
