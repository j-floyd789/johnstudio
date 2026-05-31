"""Local embedding client for Ollama.

Stdlib-only HTTP client against the Ollama daemon's ``/api/embeddings``
endpoint at ``http://127.0.0.1:11434``. The default model is
``nomic-embed-text`` (768-d). Embeddings are cached on disk by
sha256(text) in ``<JOHNSTUDIO_HOME>/embed_cache.sqlite`` so a re-embed
of the same string is free and fully offline.

NO paid APIs. The only network call this module makes is to Ollama on
loopback. If the daemon is not running, ``embed`` raises ``OllamaUnavailable``
with a clear, actionable message — callers should propagate, never fall
back to a paid provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

from . import config

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "nomic-embed-text"
_TIMEOUT_S = 30

# nomic-embed-text emits 768-d vectors. We pin this as the expected
# dimension for the default model so a silently-misconfigured/swapped
# model (or a truncated response) is rejected rather than cached and
# poisoning ReasoningBank retrieval.
NOMIC_EMBED_DIM = 768
_EXPECTED_DIM_BY_MODEL = {DEFAULT_MODEL: NOMIC_EMBED_DIM}

# A vector whose L2 norm is below this is treated as the "all-zeros"
# degenerate response Ollama sometimes returns when the model is still
# loading / the prompt was dropped. Such a vector carries no signal and
# must never be cached.
_MIN_NORM = 1e-8


class OllamaUnavailable(RuntimeError):
    """Raised when the local Ollama daemon cannot be reached."""


def expected_dim(model: str = DEFAULT_MODEL) -> int | None:
    """Return the known embedding dimension for ``model``, or ``None``.

    For the default ``nomic-embed-text`` this is :data:`NOMIC_EMBED_DIM`
    (768). For models we've already seen this session, it's the dimension
    of their first successful embed (see :func:`_remember_dim`).
    """
    return _EXPECTED_DIM_BY_MODEL.get(model)


def _remember_dim(model: str, dim: int) -> None:
    """Record the dimension observed for ``model`` on first success.

    Subsequent embeds for the same model are asserted against it. The
    default model is seeded with the known constant, so this only learns
    dims for non-default models.
    """
    _EXPECTED_DIM_BY_MODEL.setdefault(model, dim)


def verify_embedding(vec, *, expected_dim: int | None = None) -> bool:
    """Return ``True`` iff ``vec`` is a usable embedding.

    Rejects (returns ``False`` for) the failure modes Ollama exhibits when
    a model is mid-load, swapped, or the prompt was dropped:

    * not a non-empty list/sequence of finite floats;
    * all-zeros / near-zero L2 norm (no signal);
    * a length mismatch against ``expected_dim`` when one is known.

    ``expected_dim=None`` skips the dimension check (used when we don't yet
    know the model's dimension — the first successful vector establishes it).
    """
    if not isinstance(vec, (list, tuple)) or len(vec) == 0:
        return False
    norm_sq = 0.0
    for x in vec:
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            return False
        xf = float(x)
        if xf != xf or xf in (float("inf"), float("-inf")):  # NaN / inf
            return False
        norm_sq += xf * xf
    if norm_sq <= _MIN_NORM * _MIN_NORM:
        return False
    if expected_dim is not None and len(vec) != expected_dim:
        return False
    return True


def _cache_path() -> Path:
    home = config.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    return home / "embed_cache.sqlite"


def _connect(path=None) -> sqlite3.Connection:
    db = sqlite3.connect(str(path or _cache_path()))
    db.execute(
        "CREATE TABLE IF NOT EXISTS embed_cache ("
        "  sha256 TEXT PRIMARY KEY,"
        "  model TEXT NOT NULL,"
        "  dim INTEGER NOT NULL,"
        "  vector_json TEXT NOT NULL,"
        "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    return db


def _key(text: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _cache_get(text: str, model: str) -> list[float] | None:
    db = _connect()
    try:
        row = db.execute(
            "SELECT vector_json FROM embed_cache WHERE sha256 = ?",
            (_key(text, model),),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    return list(json.loads(row[0]))


def _cache_put(text: str, model: str, vector: list[float]) -> None:
    db = _connect()
    try:
        db.execute(
            "INSERT OR REPLACE INTO embed_cache (sha256, model, dim, vector_json) "
            "VALUES (?, ?, ?, ?)",
            (_key(text, model), model, len(vector), json.dumps(vector)),
        )
        db.commit()
    finally:
        db.close()


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", DEFAULT_HOST).rstrip("/")


def _post_embed(text: str, model: str) -> list[float]:
    """Call Ollama's embeddings endpoint and return the raw vector."""
    url = f"{_ollama_host()}/api/embeddings"
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            body = resp.read()
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise OllamaUnavailable(
            f"could not reach Ollama at {_ollama_host()} ({e!r}). "
            "Start the daemon with `ollama serve` and pull the model with "
            f"`ollama pull {model}`. Embeddings are LOCAL-ONLY by policy — "
            "no paid API fallback."
        ) from e
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise OllamaUnavailable(f"invalid JSON from Ollama: {e!r}") from e
    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise OllamaUnavailable(
            f"Ollama returned no embedding for model {model!r}. "
            f"Pull it with `ollama pull {model}`. Raw response: {data!r}"
        )
    return [float(x) for x in vec]


def embed(text: str, *, model: str = DEFAULT_MODEL) -> list[float]:
    """Return the embedding vector for ``text``.

    Cached by sha256(text+model) in the JohnStudio embed cache. Raises
    :class:`OllamaUnavailable` if the local daemon is not running — by
    project rule, callers must NEVER fall back to a paid provider.
    """
    if not isinstance(text, str):
        raise TypeError(f"embed() requires str, got {type(text).__name__}")
    if not text.strip():
        raise ValueError("embed() requires non-empty text")
    cached = _cache_get(text, model)
    if cached is not None:
        return cached
    vec = _post_embed(text, model)
    # Sentinel: reject zero-vec / wrong-dim before it poisons the cache and
    # ReasoningBank retrieval. We treat a bad vector as an OllamaUnavailable
    # (the daemon answered, but with garbage — usually mid-load) so callers
    # take the same NO-paid-fallback path and never cache the bad row.
    want = expected_dim(model)
    if not verify_embedding(vec, expected_dim=want):
        raise OllamaUnavailable(
            f"Ollama returned an invalid embedding for model {model!r} "
            f"(len={len(vec) if isinstance(vec, (list, tuple)) else 'n/a'}, "
            f"expected_dim={want}): empty, all-zeros, or wrong dimension. "
            "The model may still be loading — retry, or "
            f"`ollama pull {model}`. Not caching this vector."
        )
    _remember_dim(model, len(vec))
    _cache_put(text, model, vec)
    return vec


def embed_many(texts: Iterable[str], *, model: str = DEFAULT_MODEL) -> list[list[float]]:
    """Vectorise an iterable of strings, reusing the cache row-by-row."""
    return [embed(t, model=model) for t in texts]
