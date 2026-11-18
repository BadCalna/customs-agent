"""Exit the request process *after* DB commit and *before* Redis dispatch."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE","config.settings")
import django
django.setup()

if __name__=="__main__":
    from django.conf import settings
    from apps.accounts.models import User
    from apps.reviews.services.imports import upload
    from apps.runs.models import Run, RunWork
    from tests.p06.samples import sample
    if settings.DATABASES["default"]["NAME"]!="customs_p13":
        raise SystemExit("isolated P13 database required")
    result_path=Path(sys.argv[1])
    def exit_before_dispatch(run_id):
        run=Run.objects.get(pk=run_id)
        assert RunWork.objects.filter(run=run,state="pending",dispatched_at__isnull=True).exists()
        result_path.write_text(json.dumps({"run_id":str(run.pk),"task_id":str(run.task_id)}))
        os._exit(91)
    with patch("apps.runs.dispatch.safe_dispatch",side_effect=exit_before_dispatch):
        upload(User.objects.get(username="supplier_a"),filename="P13-commit-crash.xlsx",data=sample(),key=os.urandom(16).hex())
    raise SystemExit("the after-commit crash did not occur")
