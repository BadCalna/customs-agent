"""真实模板 HTTP/ORM/图片逐行核对。不提供默认真实文件，不提交原件或输出业务值。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE","config.settings")
import django
django.setup()
from django.test import Client
from django.core.files.uploadedfile import SimpleUploadedFile
from adapters.files.ooxml import Workbook
from adapters.files.packing_links import map_workbook
from adapters.files.dispimg import extract_images
from apps.accounts.models import User
from apps.reviews.models import FileVersion
from apps.reviews.services import files

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("sample",type=Path);parser.add_argument("--username",default="supplier_a")
    args=parser.parse_args();data=args.sample.read_bytes();digest=hashlib.sha256(data).hexdigest()
    workbook=Workbook(data);mapping=map_workbook(workbook)
    client=Client(SERVER_NAME="localhost");client.force_login(User.objects.get(username=args.username))
    response=client.post("/api/v1/tasks",{"file":SimpleUploadedFile(args.sample.name,data)},HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
    if response.status_code!=200:
        print(json.dumps(response.json(),ensure_ascii=False));sys.exit(1)
    result=response.json();version=FileVersion.objects.get(pk=result["version_id"])
    assert len(mapping.links)==version.items.count()
    for link in mapping.links:
        item=version.items.get(source_sheet=link.sheet,source_row=link.row)
        assert item.name==link.record.value("name")
        assert (item.supplier_hs_raw,item.supplier_elements_raw,item.supplier_tax_rate_raw)==tuple(link.record.value(x) for x in ("hs","elements","refund_rate"))
        actual=list(item.members.order_by("source_row").values_list("source_sheet","source_row","contract_no","item_no"))
        expected=sorted((x["source_sheet"],x["source_row"],x["contract_no"],x["item_no"]) for x in link.member_values())
        assert sorted(actual)==expected
    layout=mapping.declaration.layout
    if "image" in layout.columns:
        extraction=extract_images(workbook,sheet_name=layout.sheet,rows=[l.row for l in mapping.links],column=layout.columns["image"])
        for row in extraction.rows:
            image=version.items.get(source_row=row.row).images.get(source_cell=row.cell)
            assert image.status==row.status
            if row.available:
                downloaded=client.get(f"/api/v1/files/{image.pk}");assert downloaded.status_code==200
                assert hashlib.sha256(downloaded.content).hexdigest()==hashlib.sha256(row.data).hexdigest()
    assert client.get(result["original_download_url"]).content==data
    assert hashlib.sha256(args.sample.read_bytes()).hexdigest()==digest
    print(json.dumps({"sample_sha256":digest,"status":"passed","items":result["item_count"],"members":result["member_count"],"image_counts":result["image_counts"],"result_mode":result["result_mode"],"task_id":result["task_id"]},ensure_ascii=False))
