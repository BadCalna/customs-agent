"""Real Redis worker with explicit test-only fault injection, never user input."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--fail-first-item", action="store_true")
    args = parser.parse_args()
    import django_rq
    from django.conf import settings
    from apps.runs import services
    original = services.evaluate_row
    blocked = False
    def evaluate(run, item):
        global blocked
        if args.marker and not blocked:
            blocked = True
            args.marker.write_text(json.dumps({"pid": os.getpid(), "run_id": str(run.pk), "item_id": str(item.pk)}))
            time.sleep(60)
        if args.fail_first_item and run.items.order_by("item__source_row").first().item_id == item.pk:
            raise TimeoutError("explicit P13 test injection")
        return original(run,item)
    with patch.object(services, "evaluate_row", side_effect=evaluate):
        django_rq.get_worker(settings.CUSTOMS_QUEUE_NAME).work()
