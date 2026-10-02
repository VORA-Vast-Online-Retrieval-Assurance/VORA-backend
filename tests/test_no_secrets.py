"""API keys belong in .env (git-ignored), never in files the repository holds."""

import os
import re
import subprocess
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]

KEY_PATTERNS = {
    "NVIDIA key": re.compile(r"nvapi-[A-Za-z0-9_-]{20,}"),
    "Google key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    "Brave key": re.compile(r"\bBSA[A-Za-z0-9_-]{20,}"),
    "Bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._-]{25,}"),
    "hard-coded key": re.compile(
        r"""(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*["'][A-Za-z0-9_\-]{16,}["']"""),
}
SECRET_SETTING = re.compile(r"^([A-Z_]*(?:KEY|TOKEN|SECRET|CX))=(.*)$")


def repository_files() -> list[Path]:
    """Files git would commit: tracked plus untracked-but-not-ignored."""
    try:
        listing = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT.as_posix()}", "-C", str(ROOT), "ls-files", "--cached",
             "--others", "--exclude-standard"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        # No repository yet (or no git): every file the .gitignore would not exclude.
        skip = {".venv", ".runtime", "__pycache__", ".pytest_cache", "node_modules", "dist", "build", ".idea", ".vscode"}
        names = []
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in skip and not d.endswith(".egg-info")]
            names += [str((Path(folder) / f).relative_to(ROOT)) for f in files if not f.endswith((".db", ".log", ".zip"))]
        listing = chr(10).join(names)
    return [ROOT / name for name in listing.splitlines() if name]


class NoSecretsTests(TestCase):
    def test_no_api_keys_in_repository_files(self) -> None:
        found = []
        for path in repository_files():
            if path.name == ".env" or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            found += [f"{path.relative_to(ROOT)}: {label}" for label, pattern in KEY_PATTERNS.items()
                      if pattern.search(text)]
        self.assertEqual(found, [], "Move these keys to .env")

    def test_env_example_has_no_key_values(self) -> None:
        filled = [match.group(1) for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
                  if (match := SECRET_SETTING.match(line.strip())) and match.group(2).strip()]
        self.assertEqual(filled, [])

    def test_env_is_ignored_by_git(self) -> None:
        self.assertIn(".env", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
