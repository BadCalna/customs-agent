"""Tests for P18 offline evaluation matrices, honest gold-standard gate, and committed report.json."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from pydantic import ValidationError

from evaluation.expert.cases import (
    HS_MAIN,
    EvaluationThresholds,
    ExpertEvalCase,
    ExpertGoldSuite,
    fixed_cases,
)
from evaluation.expert.runner import evaluate_cases, evaluate_gold_suite

ROOT = Path(settings.BASE_DIR)
REPORT_PATH = ROOT / "evaluation" / "expert" / "report.json"


def _real_case(case_id: str, verdict: str = "matched", *, elements_mode: str = "official_sufficient") -> dict:
    return dict(
        id=case_id,
        description="非合成专家标注样例",
        synthetic=False,
        submitted_hs=HS_MAIN,
        sources={"elements": elements_mode, "tax": "official_match_13", "regulatory": "official_declared_none"},
        expected_verdicts={
            "elements": verdict,
            "images": "unknown",
            "tax": "matched",
            "regulatory": "matched",
        },
        expected_evidence_status="obtained",
        expected_issue_codes=["IMAGE_ABSENT"] + (["ELEMENT_MISSING"] if elements_mode == "official_insufficient" else []),
        **({"elements_text": "1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:;4:品牌（中文或外文名称）:无"} if elements_mode == "official_insufficient" else {}),
    )


class TestExpertEvaluation(SimpleTestCase):
    def test_fixed_technical_suite_passes_all_cases_but_stays_pending_gold_standard(self):
        cases = fixed_cases()
        self.assertGreaterEqual(len(cases), 10)
        self.assertLessEqual(len(cases), 30)
        report = evaluate_cases(cases)
        self.assertEqual(report["failed_cases"], [])
        self.assertEqual(report["case_count"], len(cases))
        self.assertEqual(report["case_pass_count"], len(cases))
        self.assertFalse(report["gold_standard"])
        self.assertEqual(report["verification_scope"], "technical_synthetic_verification")
        self.assertEqual(report["metrics"]["unknown_coverage"], 1.0)
        self.assertEqual(report["metrics"]["false_matched_on_unknown"], 0)
        self.assertEqual(report["metrics"]["conflict_recall"], 1.0)
        verdict = report["threshold_verdict"]
        self.assertEqual(verdict["status"], "pending_expert_gold_standard")
        self.assertFalse(verdict["customs_accuracy_verified"])
        self.assertTrue(verdict["technical_checks_passed"])

        # Confusion matrix totals equal case_count per aspect and 4 * case_count overall
        overall_total = sum(sum(row.values()) for row in report["confusion_matrix"]["overall"].values())
        self.assertEqual(overall_total, len(cases) * 4)
        for aspect, mat in report["confusion_matrix"]["by_aspect"].items():
            aspect_sum = sum(sum(row.values()) for row in mat.values())
            self.assertEqual(aspect_sum, len(cases), f"{aspect} matrix total mismatch")

    def test_gold_suite_rejects_synthetic_or_missing_annotator(self):
        with self.assertRaises(ValidationError):
            ExpertGoldSuite.model_validate(
                dict(
                    suite_id="bad-synth",
                    gold_standard=True,
                    synthetic=False,
                    annotated_by="Jay",
                    annotated_at="2026-11-18",
                    cases=[{**_real_case("c1"), "synthetic": True}],
                )
            )
        with self.assertRaises(ValidationError):
            ExpertGoldSuite.model_validate(
                dict(
                    suite_id="missing-annotator",
                    gold_standard=True,
                    synthetic=False,
                    annotated_by="   ",
                    annotated_at="2026-11-18",
                    cases=[_real_case("c1")],
                )
            )

    def test_gold_suite_thresholds_pass_and_fail_honestly(self):
        passing_suite = ExpertGoldSuite.model_validate(
            dict(
                suite_id="gold-pass-sim",
                annotated_by="Jay",
                annotated_at="2026-11-18T12:00:00Z",
                cases=[
                    _real_case("gold-1", "matched", elements_mode="official_sufficient"),
                    _real_case("gold-2", "conflict", elements_mode="official_insufficient"),
                ],
            )
        )
        ok_report = evaluate_gold_suite(passing_suite)
        self.assertTrue(ok_report["gold_standard"])
        self.assertEqual(ok_report["verification_scope"], "customs_gold_standard")
        self.assertEqual(ok_report["threshold_verdict"]["status"], "passed")
        self.assertTrue(ok_report["threshold_verdict"]["customs_accuracy_verified"])

        # Force false_matched_on_unknown: claim regulatory is unknown when sources actually return matched
        bad_case = _real_case("gold-bad-unknown")
        bad_case["expected_verdicts"]["regulatory"] = "unknown"
        failing_suite = ExpertGoldSuite.model_validate(
            dict(
                suite_id="gold-fail-sim",
                annotated_by="Jay",
                annotated_at="2026-11-18T12:00:00Z",
                thresholds=EvaluationThresholds(max_false_matched_on_unknown=0).model_dump(mode="json"),
                cases=[bad_case],
            )
        )
        fail_report = evaluate_gold_suite(failing_suite)
        self.assertEqual(fail_report["metrics"]["false_matched_on_unknown"], 1)
        self.assertEqual(fail_report["threshold_verdict"]["status"], "failed")
        self.assertFalse(fail_report["threshold_verdict"]["customs_accuracy_verified"])
        self.assertEqual(len(fail_report["failed_cases"]), 1)

    def test_committed_report_matches_fresh_run_and_cli_enforces_isolation(self):
        fresh = evaluate_cases()
        committed = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(committed, fresh)

        # CLI fails without DJANGO_SECRET_KEY or when pointed at config.settings
        clean_env = {k: v for k, v in os.environ.items() if k not in {"DJANGO_SECRET_KEY", "DJANGO_SETTINGS_MODULE"}}
        missing_secret = subprocess.run(
            [sys.executable, "-m", "evaluation.expert"],
            cwd=str(ROOT),
            env=clean_env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(missing_secret.returncode, 0)

        prod_env = {**clean_env, "DJANGO_SECRET_KEY": "eval-check", "DJANGO_SETTINGS_MODULE": "config.settings"}
        blocked_settings = subprocess.run(
            [sys.executable, "-m", "evaluation.expert"],
            cwd=str(ROOT),
            env=prod_env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(blocked_settings.returncode, 0)

        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "report.json"
            ok_env = {**clean_env, "DJANGO_SECRET_KEY": "eval-check"}
            proc = subprocess.run(
                [sys.executable, "-m", "evaluation.expert", "--output", str(out_file)],
                cwd=str(ROOT),
                env=ok_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(out_file.read_text(encoding="utf-8")), fresh)
