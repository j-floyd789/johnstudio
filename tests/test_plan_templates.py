"""Item 18 — plan-template library.

Save a couple of successful plan shapes keyed by task type, then retrieve
the closest ones. Embeddings are disabled in these tests (use_embeddings=
False) so retrieval is deterministic keyword/label matching with no Ollama
dependency.
"""
from __future__ import annotations

import pytest

from johnstudio import plan_templates as pt


def test_task_type_key_normalises():
    assert pt.task_type_key("Backtest the Kalshi NYC weather strategy") == (
        "backtest-kalshi-nyc-weather-strategy"
    )
    # already-clean label round-trips (lightly normalised)
    assert pt.task_type_key("kalshi-weather") == "kalshi-weather"
    assert pt.task_type_key("   ") == ""


def test_save_and_retrieve_by_type(jh_home):
    pt.save_plan_template(
        "Backtest Kalshi NYC weather",
        {"quant-researcher": "claude_vp", "backtester": "gemini_vp"},
        outcome_score=0.9,
    )
    pt.save_plan_template(
        "Write a marketing landing page",
        {"copywriter": "claude_vp", "designer": "gemini_vp"},
        outcome_score=0.8,
    )

    hits = pt.suggest_plan_templates(
        "Backtest the Kalshi weather edge", k=3, use_embeddings=False
    )
    assert hits, "expected at least one match"
    top = hits[0]
    assert "quant-researcher" in top.plan_shape
    assert top.plan_shape["backtester"] == "gemini_vp"
    # the unrelated marketing template should rank below (or be excluded)
    assert top.task_type.startswith("backtest-kalshi")


def test_exact_type_match_wins(jh_home):
    pt.save_plan_template(
        "kalshi-weather",
        {"a": "claude_vp"},
        outcome_score=0.5,
    )
    pt.save_plan_template(
        "kalshi weather forecast model",
        {"b": "gemini_vp"},
        outcome_score=0.99,
    )
    hits = pt.suggest_plan_templates("kalshi-weather", k=2, use_embeddings=False)
    assert hits[0].task_type == "kalshi-weather"  # exact label beats higher score


def test_idempotent_upsert_bumps_use_count(jh_home):
    rid1 = pt.save_plan_template("foo bar", {"r": "vp1"}, outcome_score=0.3)
    rid2 = pt.save_plan_template("foo bar", {"r": "vp1"}, outcome_score=0.7)
    assert rid1 == rid2
    hits = pt.suggest_plan_templates("foo bar", use_embeddings=False)
    assert hits[0].use_count == 2
    assert hits[0].outcome_score == 0.7  # keeps the better score


def test_shape_order_independent(jh_home):
    rid1 = pt.save_plan_template("alpha beta", {"x": "vp1", "y": "vp2"})
    rid2 = pt.save_plan_template("alpha beta", {"y": "vp2", "x": "vp1"})
    assert rid1 == rid2  # same normalised shape -> same row


def test_empty_shape_rejected(jh_home):
    with pytest.raises(ValueError):
        pt.save_plan_template("some task", {})


def test_no_match_returns_empty(jh_home):
    pt.save_plan_template("quantum chromodynamics", {"r": "vp1"})
    hits = pt.suggest_plan_templates("knitting patterns", use_embeddings=False)
    assert hits == []
