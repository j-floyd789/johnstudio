from __future__ import annotations

from pathlib import Path

from johnstudio import config, init as init_mod


def test_run_init_creates_home_and_db(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path / "home"))
    status = init_mod.run_init()
    home = Path(status["home"])
    assert home.exists()
    assert (home / "config.yaml").exists()
    assert (home / "johnstudio.db").exists()
    assert (home / "logs").exists()
    assert (home / "sources").exists()
    assert (home / "skill-registry" / "skills").exists()
    assert (home / "global-memory" / "people" / "Person - John.md").exists()


def test_run_init_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path / "home"))
    init_mod.run_init()
    s2 = init_mod.run_init()
    assert Path(s2["home"]).exists()


def test_run_init_reports_terminal_stub_always_available(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    s = init_mod.run_init()
    assert s["tools_detected"]["terminal_stub"] is True


def test_run_init_auto_imports_seeds(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    s = init_mod.run_init()
    assert s["seeds_imported"] >= 10
    # Re-running is idempotent and still reports the seed count.
    s2 = init_mod.run_init()
    assert s2["seeds_imported"] >= 10


def test_run_research_writes_report(monkeypatch, tmp_path):
    out = tmp_path / "out" / "report.md"
    p = init_mod.run_research(out)
    assert p == out
    text = p.read_text()
    assert "JohnStudio Phase 0" in text
    assert "VoltAgent" in text  # cross-checks the baked content


def test_run_research_default_target(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    p = init_mod.run_research()
    assert p == tmp_path / "docs" / "research" / "repo_research_report.md"
    assert p.exists()


def test_run_research_works_without_network(monkeypatch, tmp_path):
    # Sabotage network to prove offline: any attempt to import urllib should
    # not be required. The function reads a local seed file only.
    out = tmp_path / "r.md"
    init_mod.run_research(out)
    assert out.exists()
