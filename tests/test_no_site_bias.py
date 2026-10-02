"""No site, portal, selector or identifier format is named in the program's logic.

Sites are learned or listed in data files; nothing in the program, its comments or its prompts names one.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ["core", "llm", "services", "api", "config", "utils", "extraction", "models", "extractors"]
# Names that belong to specific sites this project has been tried on: they must not steer any code path.
BANNED = re.compile(r"sansad|egazette|e-gazette|gvgazette|writereaddata|rbi\.org|rajyasabha|loksabha|neva\.gov|"
                    r"skysports|premierleague|usgs|kernel\.org|fda\.gov|sebi\.gov|zenodo|scribd|wikipedia", re.I)


def logic_strings(path: Path):
    """String constants that are not docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


class NoSiteBiasTests(unittest.TestCase):
    def test_no_site_name_in_program_strings(self) -> None:
        found = []
        for package in PACKAGES:
            for path in sorted((ROOT / package).rglob("*.py")):
                for line, text in logic_strings(path):
                    if BANNED.search(text):
                        found.append(f"{path.relative_to(ROOT)}:{line}: {text[:60]!r}")
        self.assertEqual(found, [])

    def test_no_site_name_in_comments_or_docstrings_either(self) -> None:
        found = []
        for package in PACKAGES:
            for path in sorted((ROOT / package).rglob("*.py")):
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if BANNED.search(line):
                        found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:70]!r}")
        self.assertEqual(found, [])

    def test_the_shipped_registry_names_no_site(self) -> None:
        import json

        entries = json.loads((ROOT / "data" / "sources.json").read_text(encoding="utf-8")).get("sources", [])
        self.assertEqual([e["id"] for e in entries if not str(e.get("id", "")).startswith("auto-")], [],
                         "only generated entries may be in the shipped registry")


if __name__ == "__main__":
    unittest.main()
