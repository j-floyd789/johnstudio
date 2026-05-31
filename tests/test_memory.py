from __future__ import annotations

from johnstudio import memory


def test_init_vault_creates_layout(tmp_path):
    root = memory.init_vault(tmp_path)
    assert (root / "00_index.md").exists()
    assert (root / "project_brief.md").exists()
    assert (root / "decisions").is_dir()
    assert (root / "graph" / "projects").is_dir()
    assert (root / "graph" / "people").is_dir()


def test_init_vault_idempotent(tmp_path):
    memory.init_vault(tmp_path)
    # Modify a file, re-init should not overwrite.
    (memory.memory_root(tmp_path) / "project_brief.md").write_text("custom")
    memory.init_vault(tmp_path)
    assert (memory.memory_root(tmp_path) / "project_brief.md").read_text() == "custom"


def test_write_run_summary(tmp_path):
    memory.init_vault(tmp_path)
    p = memory.write_run_summary(tmp_path, 7, "# Run 7\nok")
    assert p.exists() and "task-0007" in p.name


def test_append_lesson(tmp_path):
    memory.init_vault(tmp_path)
    memory.append_lesson(tmp_path, "claude_backend", "Avoid touching env vars.")
    memory.append_lesson(tmp_path, "claude_backend", "Always run tests first.")
    p = memory.memory_root(tmp_path) / "agent_lessons" / "claude_backend.md"
    text = p.read_text()
    assert "Avoid touching env vars" in text
    assert "Always run tests first" in text
