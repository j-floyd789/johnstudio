"""Item 19 — embedding-validation sentinel.

Ollama occasionally answers with a zero-vector or a wrong-dimension vector
(model mid-load / swapped). `embed.verify_embedding` must reject those, and
`embed.embed` must NOT cache a bad vector — it raises OllamaUnavailable so
callers take the no-paid-fallback path.
"""
from __future__ import annotations

import pytest

from johnstudio import embed


def test_verify_good_vector():
    good = [0.1] * embed.NOMIC_EMBED_DIM
    assert embed.verify_embedding(good, expected_dim=embed.NOMIC_EMBED_DIM) is True
    # dim check skipped when expected_dim is None
    assert embed.verify_embedding([0.0, 1.0]) is True


def test_verify_rejects_empty():
    assert embed.verify_embedding([]) is False
    assert embed.verify_embedding(None) is False
    assert embed.verify_embedding("not a vector") is False


def test_verify_rejects_zero_vector():
    assert embed.verify_embedding([0.0] * 768) is False
    # near-zero norm is also rejected
    assert embed.verify_embedding([1e-12, -1e-12]) is False


def test_verify_rejects_wrong_dim():
    short = [0.5] * 100
    assert embed.verify_embedding(short, expected_dim=768) is False
    assert embed.verify_embedding(short, expected_dim=100) is True


def test_verify_rejects_nan_and_inf():
    assert embed.verify_embedding([float("nan"), 1.0]) is False
    assert embed.verify_embedding([float("inf"), 1.0]) is False


def test_verify_rejects_non_numeric():
    assert embed.verify_embedding([True, False]) is False
    assert embed.verify_embedding(["a", "b"]) is False


def test_expected_dim_default_model():
    assert embed.expected_dim() == embed.NOMIC_EMBED_DIM


def test_embed_does_not_cache_bad_vector(jh_home, monkeypatch):
    """A zero-vec from Ollama must raise and never land in the cache."""
    calls = {"n": 0}

    def fake_post(text, model):
        calls["n"] += 1
        return [0.0] * 768  # degenerate response

    monkeypatch.setattr(embed, "_post_embed", fake_post)
    with pytest.raises(embed.OllamaUnavailable):
        embed.embed("hello world")
    # cache miss -> nothing stored, so a second call re-hits the (bad) backend
    with pytest.raises(embed.OllamaUnavailable):
        embed.embed("hello world")
    assert calls["n"] == 2


def test_embed_caches_good_vector(jh_home, monkeypatch):
    good = [0.01 * (i + 1) for i in range(768)]

    def fake_post(text, model):
        return list(good)

    monkeypatch.setattr(embed, "_post_embed", fake_post)
    v1 = embed.embed("kalshi weather")
    assert v1 == good
    # second call served from cache (backend not consulted)
    monkeypatch.setattr(
        embed, "_post_embed", lambda t, m: (_ for _ in ()).throw(AssertionError("should be cached"))
    )
    v2 = embed.embed("kalshi weather")
    assert v2 == good
