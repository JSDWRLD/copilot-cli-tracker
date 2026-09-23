import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "copilot-cli-tracker.py"
spec = importlib.util.spec_from_file_location("tracker", SCRIPT)
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)


class LayoutTests(unittest.TestCase):
    def test_powerline_has_separator_glyph(self):
        self.assertEqual(
            tracker.render_segments(["A", "B"], "powerline", tracker.palette_for_theme("plain")),
            "A  B",
        )

    def test_capsule_wraps_each_segment(self):
        self.assertEqual(
            tracker.render_segments(["A", "B"], "capsule", tracker.palette_for_theme("plain")),
            "A  B ",
        )


class ContextGaugeTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(tracker.DEFAULT_CONFIG["tokens"], show_bar=True)
        self.plain = tracker.palette_for_theme("plain")

    def test_default_status_has_no_block_gauge(self):
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 50_000, "displayed_context_limit": 100_000,
             "current_context_used_percentage": 50},
            tracker.DEFAULT_CONFIG["tokens"], "plain", self.plain,
        )
        self.assertEqual(result, "Tokens: 50k/100k (50%)")

    def test_half_full_context_has_ten_cell_gauge(self):
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 50_000, "displayed_context_limit": 100_000,
             "current_context_used_percentage": 50.0},
            self.config, "plain", self.plain,
        )
        self.assertEqual(result, "Tokens: 50k/100k (50%) [█████░░░░░]")

    def test_gauge_derives_percentage_when_payload_omits_it(self):
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 25_000, "displayed_context_limit": 100_000},
            self.config, "plain", self.plain,
        )
        self.assertEqual(result, "Tokens: 25k/100k [███░░░░░░░]")

    def test_gauge_is_hidden_without_context_limit(self):
        self.assertEqual(
            tracker.render_tokens_segment({}, self.config, "plain", self.plain),
            "Tokens: 0/0",
        )

    def test_gauge_uses_warning_and_critical_colors(self):
        palette = tracker.palette_for_theme("github")
        warning = tracker.render_tokens_segment(
            {"current_context_tokens": 75_000, "displayed_context_limit": 100_000,
             "current_context_used_percentage": 75.0},
            self.config, "plain", palette,
        )
        critical = tracker.render_tokens_segment(
            {"current_context_tokens": 95_000, "displayed_context_limit": 100_000,
             "current_context_used_percentage": 95.0},
            self.config, "plain", palette,
        )
        self.assertIn(f"{palette['badge']}[████████░░]{palette['reset']}", warning)
        self.assertIn(f"{palette['tokens_alert']}[██████████]{palette['reset']}", critical)

    def test_gauge_can_be_disabled(self):
        config = dict(self.config, show_bar=False)
        self.assertEqual(
            tracker.render_tokens_segment(
                {"current_context_tokens": 50_000, "displayed_context_limit": 100_000,
                 "current_context_used_percentage": 50.0},
                config, "plain", self.plain,
            ),
            "Tokens: 50k/100k (50%)",
        )

    def test_large_window_at_ten_percent_is_not_an_alert(self):
        palette = tracker.palette_for_theme("github")
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 100_200, "displayed_context_limit": 1_000_000,
             "current_context_used_percentage": 10},
            self.config, "plain", palette,
        )
        self.assertIn(f"{palette['tokens_normal']}100k{palette['reset']}", result)
        self.assertNotIn("⚠", result)
        self.assertNotIn(palette["tokens_alert"], result)

    def test_nearly_full_context_uses_red_text_without_warning_emoji(self):
        palette = tracker.palette_for_theme("github")
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 95_000, "displayed_context_limit": 100_000,
             "current_context_used_percentage": 95},
            self.config, "plain", palette,
        )
        self.assertIn(f"{palette['tokens_alert']}95k{palette['reset']}", result)
        self.assertNotIn("⚠", result)

    def test_plain_theme_alert_has_no_warning_emoji(self):
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 95_000, "displayed_context_limit": 100_000,
             "current_context_used_percentage": 95},
            self.config, "plain", self.plain,
        )
        self.assertEqual(result, "Tokens: 95k/100k (95%) [██████████]")

    def test_inconsistent_context_displays_percentage_only(self):
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 1016, "displayed_context_limit": 1_000_000,
             "current_context_used_percentage": 101},
            tracker.DEFAULT_CONFIG["tokens"], "plain", self.plain,
        )
        self.assertEqual(result, "Context: 101%")

    def test_inconsistent_context_displays_red_at_high_usage(self):
        palette = tracker.palette_for_theme("github")
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 1016, "displayed_context_limit": 1_000_000,
             "current_context_used_percentage": 101},
            tracker.DEFAULT_CONFIG["tokens"], "plain", palette,
        )
        self.assertIn(f"{palette['tokens_alert']}101%{palette['reset']}", result)
        self.assertNotIn("1016", result)

    def test_small_rounding_difference_still_displays_counts(self):
        result = tracker.render_tokens_segment(
            {"current_context_tokens": 51_000, "displayed_context_limit": 100_000,
             "current_context_used_percentage": 50},
            tracker.DEFAULT_CONFIG["tokens"], "plain", self.plain,
        )
        self.assertEqual(result, "Tokens: 51k/100k (50%)")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.plain = tracker.palette_for_theme("plain")
        self.config = dict(tracker.DEFAULT_CONFIG["month_cost"], budget_usd=100)

    def test_budget_displays_limit_without_warning_below_threshold(self):
        self.assertEqual(
            tracker.render_month_cost_segment(7_900_000_000_000, self.config, "plain", self.plain),
            "Month: $79.00/$100.00",
        )

    def test_budget_warns_at_eighty_percent(self):
        self.assertEqual(
            tracker.render_month_cost_segment(8_000_000_000_000, self.config, "plain", self.plain),
            "Month: $80.00/$100.00",
        )

    def test_budget_warns_over_limit(self):
        self.assertEqual(
            tracker.render_month_cost_segment(10_100_000_000_000, self.config, "plain", self.plain),
            "Month: $101.00/$100.00",
        )

    def test_month_warning_uses_color_instead_of_emoji(self):
        palette = tracker.palette_for_theme("github")
        result = tracker.render_month_cost_segment(
            10_100_000_000_000, self.config, "plain", palette,
        )
        self.assertIn(f"{palette['tokens_alert']}$101.00/$100.00{palette['reset']}", result)
        self.assertNotIn("⚠", result)

    def test_budget_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "budget_usd"):
            tracker.render_month_cost_segment(0, dict(self.config, budget_usd=0), "plain", self.plain)

    def test_missing_database_does_not_masquerade_as_zero_month_spend(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".copilot").mkdir()
            (root / ".copilot" / "copilot-cli-tracker.json").write_text('{"month_cost":{"budget_usd":100}}')
            payload = {"session_id": "current", "ai_used": {"total_nano_aiu": 100_000_000_000}}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--theme", "plain"],
                input=json.dumps(payload), text=True, capture_output=True,
                env={**os.environ, "HOME": str(root)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Month: unavailable", result.stdout)
            self.assertNotIn("Month: $1.00/$100.00", result.stdout)

    def test_live_month_total_drives_budget_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copilot = root / ".copilot"
            copilot.mkdir()
            (copilot / "copilot-cli-tracker.json").write_text('{"month_cost":{"budget_usd":100}}')
            with sqlite3.connect(copilot / "session-store.db") as connection:
                connection.execute(
                    "CREATE TABLE assistant_usage_events "
                    "(session_id TEXT, total_nano_aiu INTEGER, created_at TEXT)"
                )
                connection.execute(
                    "INSERT INTO assistant_usage_events VALUES (?, ?, datetime('now'))",
                    ("other", 8_000_000_000_000),
                )
                connection.execute(
                    "INSERT INTO assistant_usage_events VALUES (?, ?, datetime('now'))",
                    ("current", 50_000_000_000),
                )
            payload = {"session_id": "current",
                       "context_window": {"current_context_tokens": 50_000,
                                          "displayed_context_limit": 100_000,
                                          "current_context_used_percentage": 50},
                       "ai_used": {"total_nano_aiu": 100_000_000_000}}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--theme", "plain"],
                input=json.dumps(payload), text=True, capture_output=True,
                env={**os.environ, "HOME": str(root)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Month: $81.00/$100.00", result.stdout)
            self.assertIn("Tokens: 50k/100k (50%)", result.stdout)
            self.assertNotIn("[", result.stdout)

    def test_session_spanning_month_uses_only_current_month_events_and_live_delta(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copilot = root / ".copilot"
            copilot.mkdir()
            (copilot / "copilot-cli-tracker.json").write_text('{"month_cost":{"budget_usd":100}}')
            with sqlite3.connect(copilot / "session-store.db") as connection:
                connection.execute(
                    "CREATE TABLE assistant_usage_events "
                    "(session_id TEXT, total_nano_aiu INTEGER, created_at TEXT)"
                )
                connection.execute(
                    "INSERT INTO assistant_usage_events VALUES (?, ?, datetime('now', '-1 month'))",
                    ("current", 9_000_000_000_000),
                )
                connection.execute(
                    "INSERT INTO assistant_usage_events VALUES (?, ?, datetime('now'))",
                    ("current", 2_000_000_000_000),
                )
                connection.execute(
                    "INSERT INTO assistant_usage_events VALUES (?, ?, datetime('now'))",
                    ("other", 500_000_000_000),
                )
            payload = {"session_id": "current",
                       "ai_used": {"total_nano_aiu": 11_100_000_000_000}}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--theme", "plain"],
                input=json.dumps(payload), text=True, capture_output=True,
                env={**os.environ, "HOME": str(root)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Month: $26.00/$100.00", result.stdout)
            self.assertNotIn("⚠️", result.stdout)

    def test_invalid_budget_is_reported_even_without_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".copilot").mkdir()
            (root / ".copilot" / "copilot-cli-tracker.json").write_text('{"month_cost":{"budget_usd":0}}')
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--theme", "plain"],
                input='{"session_id":"current"}', text=True, capture_output=True,
                env={**os.environ, "HOME": str(root)},
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("budget_usd", result.stderr)
            self.assertEqual(result.stdout, "")


class ConfigTests(unittest.TestCase):
    def test_init_writes_project_named_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--init"],
                text=True, capture_output=True, env={**os.environ, "HOME": str(root)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config_path = root / ".copilot" / "copilot-cli-tracker.json"
            self.assertTrue(config_path.exists())
            self.assertEqual(json.loads(config_path.read_text())["style"], "minimal")

    def test_malformed_budget_config_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".copilot").mkdir()
            (root / ".copilot" / "copilot-cli-tracker.json").write_text('{"month_cost":{"budget_usd":100')
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--theme", "plain"],
                input='{"session_id":"current"}', text=True, capture_output=True,
                env={**os.environ, "HOME": str(root)},
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("copilot-cli-tracker.json", result.stderr)
            self.assertEqual(result.stdout, "")


class PrCacheTests(unittest.TestCase):
    def test_unchanged_branch_uses_cache_and_branch_change_refreshes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "gh"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "from pathlib import Path\n"
                "if sys.argv[1:3] != ['pr', 'list']: sys.exit(9)\n"
                "Path(os.environ['GH_COUNT_FILE']).open('a').write('called\\n')\n"
                "print('[{\"number\": 12, \"url\": \"https://github.com/acme/app/pull/12\"}]')\n"
            )
            executable.chmod(0o755)
            repo = root / "repo"
            repo.mkdir()
            count = root / "calls"
            env = {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}", "GH_COUNT_FILE": str(count)}
            with patch.object(tracker, "COPILOT_DIR", root), patch.dict(os.environ, env):
                self.assertEqual(tracker.get_pr_info(repo, "main", 60)["number"], 12)
                self.assertEqual(tracker.get_pr_info(repo, "main", 60)["number"], 12)
                self.assertEqual(count.read_text().splitlines(), ["called"])
                self.assertEqual(tracker.get_pr_info(repo, "feature", 60)["number"], 12)
                self.assertEqual(count.read_text().splitlines(), ["called", "called"])

    def test_no_pr_result_is_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "gh"
            executable.write_text(
                "#!/bin/sh\n"
                "test \"$1\" = pr && test \"$2\" = list || exit 9\n"
                "printf 'called\\n' >> \"$GH_COUNT_FILE\"\n"
                "printf '[]\\n'\n"
            )
            executable.chmod(0o755)
            repo = root / "repo"
            repo.mkdir()
            count = root / "calls"
            env = {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}", "GH_COUNT_FILE": str(count)}
            with patch.object(tracker, "COPILOT_DIR", root), patch.dict(os.environ, env):
                self.assertIsNone(tracker.get_pr_info(repo, "main", 60))
                self.assertIsNone(tracker.get_pr_info(repo, "main", 60))
                self.assertEqual(count.read_text().splitlines(), ["called"])

    def test_corrupt_cached_pr_is_discarded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "gh"
            executable.write_text(
                "#!/bin/sh\n"
                "test \"$1\" = pr && test \"$2\" = list || exit 9\n"
                "printf '%s\\n' '[{\"number\": 12, \"url\": \"https://github.com/acme/app/pull/12\"}]'\n"
            )
            executable.chmod(0o755)
            repo = root / "repo"
            repo.mkdir()
            with patch.object(tracker, "COPILOT_DIR", root), patch.dict(
                os.environ, {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"}
            ):
                tracker.get_pr_info(repo, "main", 60)
                cache = next(root.glob(".copilot-cli-tracker-pr-*.json"))
                entry = json.loads(cache.read_text())
                entry["pr"]["number"] = "12\x1b]52;c;bad\x07"
                cache.write_text(json.dumps(entry))
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(tracker.get_pr_info(repo, "main", 60)["number"], 12)
                self.assertIn("invalid cached PR", errors.getvalue())

    def test_failed_cache_write_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "gh"
            executable.write_text(
                "#!/bin/sh\n"
                "test \"$1\" = pr && test \"$2\" = list || exit 9\n"
                "printf '%s\\n' '[{\"number\": 12, \"url\": \"https://github.com/acme/app/pull/12\"}]'\n"
            )
            executable.chmod(0o755)
            repo = root / "repo"
            repo.mkdir()
            with patch.object(tracker, "COPILOT_DIR", root), patch.dict(
                os.environ, {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"}
            ), patch.object(tracker.os, "replace", side_effect=PermissionError("denied")):
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(tracker.get_pr_info(repo, "main", 60)["number"], 12)
                self.assertIn("PR cache write failed", errors.getvalue())
                self.assertEqual(list(root.glob(".pr-cache-*")), [])

    def test_gh_error_is_reported_and_not_cached_as_no_pr(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "gh"
            executable.write_text("#!/bin/sh\nprintf 'connection refused\\n' >&2\nexit 1\n")
            executable.chmod(0o755)
            repo = root / "repo"
            repo.mkdir()
            with patch.object(tracker, "COPILOT_DIR", root), patch.dict(
                os.environ, {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"}
            ):
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertIsNone(tracker.get_pr_info(repo, "main", 60))
                self.assertIn("connection refused", errors.getvalue())
                executable.write_text(
                    "#!/bin/sh\n"
                    "printf '%s\\n' '[{\"number\": 12, \"url\": \"https://github.com/acme/app/pull/12\"}]'\n"
                )
                self.assertEqual(tracker.get_pr_info(repo, "main", 60)["number"], 12)


if __name__ == "__main__":
    unittest.main()
