"""python -m evaluation.precheck: JSON on stdout, 0/1/2 exit statuses."""
import argparse
import json
import os
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="P16 synthetic technical comparison evaluation; model NOT called")
    parser.add_argument("--output", type=Path, help="optional report JSON file (stdout always contains report)")
    args = parser.parse_args(argv)
    try:
        # Never manufacture secrets or read dotenv. Explicit overrides remain caller-owned.
        if not os.environ.get("DJANGO_SECRET_KEY", "").strip():
            raise ValueError("DJANGO_SECRET_KEY required")
        # Reject other settings BEFORE django import/setup: config.settings reads .env.
        settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
        if settings_module not in (None, "evaluation.precheck.settings"):
            raise ValueError("CLI accepts only isolated offline settings")
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evaluation.precheck.settings")
        import django
        from django.conf import settings
        if settings.configured and getattr(settings, "SETTINGS_MODULE", None) != "evaluation.precheck.settings":
            raise ValueError("Django already configured with non-offline settings")
        django.setup()
        from . import build_report
        report = build_report()
        rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.output is not None:
            args.output.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 0 if report["summary"]["technical_success"] else 1
    except Exception:
        # Do not echo exception details or configuration which might contain secrets/paths.
        error = {"synthetic": True, "technical_only": True, "model_not_called": True,
                 "quality_gate_pending": True, "error": "EVALUATION_CONFIGURATION_OR_EXECUTION_ERROR",
                 "hint": "Set DJANGO_SECRET_KEY explicitly, use offline settings, call django.setup(), and check output permissions."}
        sys.stdout.write(json.dumps(error, sort_keys=True) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
