"""P16 evaluation-only tests: strict fixtures, deterministic reports, no I/O services."""
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connections
from django.test import SimpleTestCase
from pydantic import ValidationError

from evaluation.precheck import build_report
from evaluation.precheck.__main__ import main
from evaluation.precheck.cases import SyntheticCase, fixed_cases
from evaluation.precheck.runner import canonical_json, sha256


class EvaluationTests(SimpleTestCase):
    def cases(self):
        return [case.model_dump(mode="json") for case in fixed_cases()]

    def test_default_suite_is_not_empty_and_covers_all_six_categories(self):
        report = build_report()
        self.assertEqual(report["summary"]["total"], 24)
        self.assertEqual(report["summary"]["technical_failed"], 0)
        self.assertTrue(report["summary"]["technical_success"])
        self.assertEqual(set(report["summary"]["expected_status_counts"]), {
            "association_error", "supplier_missing", "supplier_invalid",
            "needs_confirmation", "consistent", "inconsistent",
        })
        for key in ("synthetic", "technical_only", "model_not_called", "quality_gate_pending"):
            self.assertIs(report[key], True)
        self.assertEqual(report["versions"]["model"], "not_called")
        self.assertNotIn("pass_rate", report["summary"])
        self.assertTrue(any("NOT business accuracy" in limitation for limitation in report["limitations"]))

    def test_repeated_reports_are_byte_stable_and_hashes_recompute(self):
        first, second = build_report(), build_report()
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["input_sha256"], sha256([
            {"id": row["id"], "input": row["input"]} for row in first["cases"]]))
        self.assertEqual(first["output_sha256"], sha256([
            {"id": row["id"], "output": row["output"]} for row in first["cases"]]))
        for row in first["cases"]:
            self.assertEqual(row["input_sha256"], sha256(row["input"]))
            self.assertEqual(row["output_sha256"], sha256(row["output"]))
            self.assertEqual(row["differences"], [])

    def test_no_network_database_or_model_call(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
             patch.object(connections["default"], "cursor", side_effect=AssertionError("DB forbidden")), \
             patch("adapters.engines.precheck.PrecheckEngine.recommend", side_effect=AssertionError("model integration forbidden")):
            self.assertTrue(build_report()["summary"]["technical_success"])

    def test_empty_nonlist_and_missing_categories_rejected(self):
        for invalid in ([], None, (), {}, [self.cases()[0]]):
            # None selects defaults by API design, not an explicit empty suite.
            if invalid is None:
                continue
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_report(invalid)
        for status in ("association_error", "supplier_missing", "supplier_invalid", "needs_confirmation", "consistent", "inconsistent"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                build_report([row for row in self.cases() if row["expected_status"] != status])

    def test_duplicate_and_blank_ids_rejected(self):
        cases = self.cases()
        with self.assertRaises(ValueError):
            build_report(cases + [cases[0]])
        for invalid in ("", " ", "id with spaces", 123):
            cases = self.cases()
            cases[0]["id"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                build_report(cases)

    def test_extra_forbid_at_all_fixture_levels(self):
        for level in ("case", "recommendation", "evidence"):
            cases = self.cases()
            target = cases[0]
            if level == "recommendation":
                target = target["recommendation"]
            elif level == "evidence":
                target = target["recommendation"]["evidence"][0]
            target["unexpected"] = "not allowed"
            with self.subTest(level=level), self.assertRaises(ValidationError):
                build_report(cases)

    def test_strict_types_and_true_synthetic_only(self):
        for field, value in (("association_ok", "false"), ("association_ok", 0),
                             ("synthetic", False), ("synthetic", 1), ("synthetic", "true"),
                             ("supplier_hs_raw", 8544422900), ("expected_issue_codes", ())):
            cases = self.cases()
            cases[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                build_report(cases)

    def test_constructed_instance_is_revalidated(self):
        cases = fixed_cases()
        cases[0] = SyntheticCase.model_construct(**{**cases[0].model_dump(), "association_ok": "false"})
        with self.assertRaises(ValidationError):
            build_report(cases)

    def test_fresh_defaults_and_no_input_mutation(self):
        cases = self.cases()
        before = deepcopy(cases)
        build_report(cases)
        self.assertEqual(before, cases)
        other = fixed_cases()
        other[0].recommendation.gap_codes.append("CHANGED")
        self.assertNotIn("CHANGED", fixed_cases()[0].recommendation.gap_codes)

    def test_source_issues_reported_without_asserting_invalidity(self):
        report = build_report()
        cases = {row["id"]: row for row in report["cases"]}
        for id_ in ("source-not-found", "source-query-failed", "source-version-unverified"):
            self.assertEqual(cases[id_]["actual"]["comparison_status"], "needs_confirmation")
            self.assertIn("SUPPLIER_HS_VALIDITY_PENDING", cases[id_]["source_issues"]["issue_codes"])
            self.assertEqual(cases[id_]["source_issues"]["original_validity"], "pending")
        self.assertEqual(cases["consistent"]["source_issues"]["original_validity"], "not_verified")

    def test_difference_detected_instead_of_false_success(self):
        cases = self.cases()
        next(row for row in cases if row["id"] == "missing-facts")["expected_issue_codes"] = ["WRONG_EXPECTATION"]
        report = build_report(cases)
        self.assertFalse(report["summary"]["technical_success"])
        self.assertEqual(report["summary"]["technical_failed"], 1)
        failed = next(row for row in report["cases"] if not row["technical_passed"])
        self.assertEqual(failed["differences"][0]["field"], "issue_codes")

    def test_runtime_error_is_sanitized_and_fails(self):
        with patch("evaluation.precheck.runner.compare_original_checked", side_effect=RuntimeError("private-token-and-path")):
            report = build_report()
        self.assertEqual(report["summary"]["technical_failed"], report["summary"]["total"])
        self.assertNotIn("private-token-and-path", canonical_json(report))
        self.assertEqual(report["cases"][0]["error"], "COMPARISON_TECHNICAL_ERROR")

    def test_cli_failures_nonzero_and_never_leak_raw_error(self):
        with patch.dict(os.environ, {"DJANGO_SECRET_KEY": ""}), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([]), 2)
        self.assertTrue(json.loads(out.getvalue())["model_not_called"])
        fake = build_report()
        fake["summary"]["technical_success"] = False
        with patch.dict(os.environ, {"DJANGO_SECRET_KEY": "p16-offline-test-only", "DJANGO_SETTINGS_MODULE": "evaluation.precheck.settings"}), \
             patch("django.conf.settings", SimpleNamespace(configured=True, SETTINGS_MODULE="evaluation.precheck.settings")), \
             patch("django.setup"), \
             patch("evaluation.precheck.build_report", return_value=fake), redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 1)

    def test_cli_refuses_business_settings_before_django_setup(self):
        with patch.dict(os.environ, {"DJANGO_SECRET_KEY": "p16-offline-test-only", "DJANGO_SETTINGS_MODULE": "config.settings"}), \
             patch("django.setup") as setup, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([]), 2)
        setup.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["error"], "EVALUATION_CONFIGURATION_OR_EXECUTION_ERROR")

    def test_cli_default_settings_never_load_dotenv_and_is_stable(self):
        # In-memory child guard fails if any .env reader, business config import,
        # socket or DB access is introduced. The CLI must initialize itself.
        script = '''import os, runpy, sys
import dotenv
from unittest.mock import patch
with patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")), patch("socket.socket", side_effect=AssertionError("network forbidden")):
    try:
        runpy.run_module("evaluation.precheck", run_name="__main__")
    finally:
        assert "config.settings" not in sys.modules
'''
        env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
        env["DJANGO_SECRET_KEY"] = "p16-offline-test-only"
        proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report, build_report())

    def test_committed_technical_deliverable_matches_rebuild(self):
        path = Path(__file__).resolve().parents[2] / "evaluation" / "precheck" / "report.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), build_report())
