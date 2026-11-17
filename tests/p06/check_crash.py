"""独立 SQLite 数据库实测 os._exit 窗口。所有数据和文件位于自动清理的临时目录。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]

def run(code, env, expected=0):
    result=subprocess.run([sys.executable,"-c",code],cwd=ROOT,env=env,capture_output=True,text=True)
    if result.returncode!=expected:
        raise RuntimeError(f"exit={result.returncode}, expected={expected}: {result.stdout}\n{result.stderr}")
    return result.stdout

if __name__=="__main__":
    with tempfile.TemporaryDirectory(prefix="p06-crash-") as tmp:
        env={**os.environ,"DJANGO_SETTINGS_MODULE":"config.settings","CUSTOMS_DB_BACKEND":"sqlite",
             "CUSTOMS_SQLITE_PATH":str(Path(tmp)/"db.sqlite3"),"CUSTOMS_FILE_ROOT":str(Path(tmp)/"files")}
        subprocess.run([sys.executable,"manage.py","migrate","--noinput"],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,check=True)
        setup="import django; django.setup(); "
        run(setup+'''from unittest.mock import patch
from tests.p01.factories import make_user
from tests.p06.samples import sample
from apps.accounts.models import Role
from apps.reviews.services.imports import upload
import os
supplier=make_user("crash-supplier",Role.SUPPLIER)
with patch("apps.reviews.services.imports.execute_precheck",side_effect=lambda *a,**k:os._exit(91)):
    upload(supplier,filename="synthetic.xlsx",data=sample(),key="crash-key")
''',env,expected=91)
        print(run(setup+'''from apps.reviews.models import ImportJob,FileVersion,Item,Task
from apps.runs.models import Run,RunItem
from apps.reviews.services.imports import finish_import
job=ImportJob.objects.get()
assert job.status=="staged" and Item.objects.count()==0
version_id,run_id=job.version_id,job.precheck_id
result=finish_import(job.pk)
assert result["parse_status"]=="succeeded"
assert Task.objects.count()==1 and FileVersion.objects.count()==1 and Run.objects.count()==1
assert Item.objects.count()==3 and RunItem.objects.count()==3
job.refresh_from_db()
assert (job.version_id,job.precheck_id)==(version_id,run_id)
assert finish_import(job.pk)["item_count"]==3
print("PASS: os._exit(91) after publication; DB rolled back, persisted reservation resumed; 1 task / 1 version / 1 mock precheck / 3 items")
''',env).strip())
