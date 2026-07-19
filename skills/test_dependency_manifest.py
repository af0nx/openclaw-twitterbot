import re
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def declared_distributions() -> set[str]:
    requirements = REPOSITORY_ROOT.joinpath("requirements.txt").read_text(encoding="utf-8")
    return {
        re.split(r"[<>=!~\[]", line.split("#", 1)[0].strip(), maxsplit=1)[0]
        .lower()
        .replace("_", "-")
        for line in requirements.splitlines()
        if line.split("#", 1)[0].strip()
    }


class DependencyManifestTests(unittest.TestCase):
    def test_curl_cffi_runtime_import_is_declared(self):
        runtime_directories = (
            REPOSITORY_ROOT / "scripts" / "ingestion",
            REPOSITORY_ROOT / "scripts" / "output",
            REPOSITORY_ROOT / "scripts" / "processing",
        )
        importers = [
            path
            for directory in runtime_directories
            for path in directory.glob("*.py")
            if "curl_cffi" in path.read_text(encoding="utf-8")
        ]

        self.assertTrue(importers)
        self.assertIn("curl-cffi", declared_distributions())


if __name__ == "__main__":
    unittest.main()
