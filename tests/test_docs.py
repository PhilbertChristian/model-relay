import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DocsTest(unittest.TestCase):
    def _check(self, script: str) -> None:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / script)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_docs_follow_standards(self):
        self._check("check_docs_md.py")

    def test_no_duplicated_prose(self):
        self._check("check_duplication.py")


if __name__ == "__main__":
    unittest.main()
