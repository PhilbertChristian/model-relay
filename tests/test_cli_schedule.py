"""CLI wiring for `relay burn auto`, `relay windows` and `relay schedule`."""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from relay.__main__ import main

EXAMPLE = str(Path(__file__).resolve().parent.parent / "examples" / "burner-week.json")


def run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue()


class ScheduleCliTest(unittest.TestCase):
    def test_burn_auto_dispatches_to_autoburn(self):
        with mock.patch("relay.autoburn.run_cli", return_value=0) as run_cli:
            code, _ = run(["burn", "auto", "--dry-run", "-b", EXAMPLE])
        self.assertEqual(code, 0)
        args = run_cli.call_args.args[0]
        self.assertTrue(args.dry_run)
        self.assertEqual(args.burner, EXAMPLE)

    def test_burn_auto_falls_back_to_the_example_config(self):
        with mock.patch("relay.autoburn.run_cli", return_value=0) as run_cli, \
                mock.patch("relay.__main__.os.path.exists", return_value=False):
            code, out = run(["burn", "auto", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("using the example", out)
        self.assertTrue(run_cli.call_args.args[0].burner.endswith(os.path.join("examples", "burner-week.json")))

    def test_windows_plan_staggers_resets_per_plan(self):
        code, out = run(["windows", "plan", "-b", EXAMPLE])
        self.assertEqual(code, 0)
        self.assertIn("claude-max", out)
        self.assertIn("codex", out)

    def test_schedule_shows_the_reserve(self):
        code, out = run(["schedule", "-b", EXAMPLE])
        self.assertEqual(code, 0)
        self.assertIn("reserve", out)

    def test_autoburn_flags_are_rejected_on_other_burn_actions(self):
        code, out = run(["burn", "week", "--check"])
        self.assertEqual(code, 2)
        self.assertIn("doesn't take --check", out)


if __name__ == "__main__":
    unittest.main()
