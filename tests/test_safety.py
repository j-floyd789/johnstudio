from __future__ import annotations

from johnstudio import safety


def test_path_is_protected_dotenv():
    assert safety.path_is_protected(".env", [".env"])
    assert safety.path_is_protected("src/.env", [".env"])
    assert safety.path_is_protected(".env.local", [".env.*"])
    assert not safety.path_is_protected("env.md", [".env"])


def test_path_is_protected_glob_pem():
    assert safety.path_is_protected("ssl/server.pem", ["**/*.pem"])
    assert safety.path_is_protected("keys/private.key", ["**/*.key"])


def test_scan_protected_paths_in_files():
    files = [".env", "src/app.tsx", "ssl/cert.pem", "README.md"]
    hits = safety.scan_protected_paths_in_files(files, [".env", "**/*.pem"])
    assert set(hits) == {".env", "ssl/cert.pem"}


def test_extract_changed_files_from_diff():
    diff = (
        "diff --git a/src/a.ts b/src/a.ts\n"
        "index 1..2 100644\n"
        "--- a/src/a.ts\n"
        "+++ b/src/a.ts\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
        "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n"
    )
    files = safety.extract_changed_files_from_diff(diff)
    assert files == [".env", "src/a.ts"]


def test_scan_dangerous_commands():
    text = "I will run rm -rf / and sudo poweroff just to be safe"
    hits = safety.scan_text_for_dangerous_commands(text, ["rm -rf", "sudo", "curl | bash"])
    assert "rm -rf" in hits and "sudo" in hits


def test_scan_approval_commands():
    text = "Plan: npm install react and pip install pyyaml"
    hits = safety.scan_text_for_approval_commands(text, ["npm install", "pip install", "brew install"])
    assert "npm install" in hits and "pip install" in hits
