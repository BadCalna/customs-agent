import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.http import Http404
from django.core.files.uploadedfile import SimpleUploadedFile

from adapters.files import storage
from adapters.files.ooxml import Workbook
from adapters.files.packing_links import map_workbook
from apps.accounts.errors import ApiError
from apps.accounts.models import Role, TaskAssignment
from apps.reviews.models import AuditEvent, Confirmation, FileVersion, IdempotencyRecord, ImportJob, Item, ItemImage, ItemMember, Task, TaskClaim
from apps.reviews.services import files, idempotency, imports
from apps.reviews.services.claims import claim_task, claimable_tasks
from apps.runs.models import Run, RunItem
from tests.p01.factories import make_user, assign
from tests.p06.samples import sample


def no_claim_scope(user, queryset):
    return queryset.none()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ImportTests(TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)/"files"
        override=override_settings(CUSTOMS_FILE_ROOT=self.root); override.enable(); self.addCleanup(override.disable)
        self.supplier=make_user("s",Role.SUPPLIER); self.other=make_user("s2",Role.SUPPLIER)
        self.operator=make_user("o",Role.OPERATOR); self.operator2=make_user("o2",Role.OPERATOR)
        self.expert=make_user("e",Role.EXPERT); self.data=sample()

    def upload(self, **kwargs):
        values=dict(filename="synthetic.xlsx",data=self.data,key="upload-1");values.update(kwargs)
        return imports.upload(self.supplier,**values)

    def test_full_import_sources_members_and_three_image_states(self):
        result=self.upload();v=FileVersion.objects.get(pk=result["version_id"])
        self.assertEqual(result["parse_status"],"succeeded"); self.assertEqual(result["result_mode"],"mock")
        self.assertEqual(result["assignment_status"],"unclaimed")
        self.assertEqual((result["item_count"],result["member_count"]),(3,3))
        self.assertEqual(result["image_counts"],dict(available=1,absent=1,read_failed=1))
        self.assertEqual(files.read_version_original(self.supplier,v.pk).content,self.data)
        mapping=map_workbook(Workbook(self.data))
        for link,item in zip(mapping.links,v.items.order_by("source_row")):
            self.assertEqual(item.source_row,link.row);self.assertEqual(item.source_sheet,link.sheet)
            self.assertEqual(item.supplier_hs_raw,link.record.value("hs"));self.assertEqual(item.attributes["existing_opinion"]["value"],"历史备注原文")
            self.assertEqual(list(item.members.values_list("source_row",flat=True)),[m["source_row"] for m in link.member_values()])
        self.assertEqual(Run.objects.count(),1); self.assertEqual(RunItem.objects.count(),3)
        self.assertFalse(Confirmation.objects.exists());self.assertFalse(TaskAssignment.objects.exists())
        self.assertEqual(ImportJob.objects.get().status,"succeeded")
        self.assertFalse(list(self.root.glob("incoming/*.json")))

    def test_retry_does_not_duplicate_version_or_precheck(self):
        first=self.upload(); second=self.upload()
        self.assertEqual(first["version_id"],second["version_id"]);self.assertTrue(second["idempotent_replay"])
        self.assertEqual((Task.objects.count(),FileVersion.objects.count(),Run.objects.count()),(1,1,1))
        self.assertEqual(AuditEvent.objects.filter(action="import.succeeded").count(),1)

    def test_legacy_upload_record_still_replays(self):
        first=self.upload()
        job=ImportJob.objects.get(pk=first["import_id"])
        payload={"task_id":None,"filename":"synthetic.xlsx","sha256":job.sha256,
                 "expected_current_version_id":None,"confirm_duplicate":False}
        record=IdempotencyRecord.objects.get(key="upload-1")
        record.request_hash=idempotency.legacy_request_hash(payload)
        record.save(update_fields=["request_hash"])
        second=self.upload()
        self.assertEqual(second["version_id"],first["version_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(FileVersion.objects.count(),1)

    def test_same_key_different_content_conflicts(self):
        self.upload()
        with self.assertRaises(ApiError) as ctx:self.upload(data=sample(order=(3,2,1)))
        self.assertEqual(ctx.exception.code,"idempotency_conflict")

    def test_same_key_different_target_conflicts(self):
        first=self.upload()
        with self.assertRaises(ApiError) as ctx:self.upload(task_id=first["task_id"],expected_current_version_id=first["version_id"])
        self.assertEqual(ctx.exception.code,"idempotency_conflict")

    def test_invalid_import_does_not_leave_task_or_file(self):
        for data in (sample(missing_sheet=True),sample(missing_key=True),sample(ambiguous=True),b"broken"):
            with self.subTest(data_size=len(data)):
                with self.assertRaises(ApiError) as ctx:self.upload(data=data)
                self.assertEqual(ctx.exception.status,422);self.assertTrue(ctx.exception.details["issues"])
                self.assertEqual(Task.objects.count(),0)
        self.assertFalse(self.root.exists())
        self.assertFalse(IdempotencyRecord.objects.exists())

    def test_blank_hs_elements_tax_preserved(self):
        self.upload(data=sample(blank_raw=True))
        for item in Item.objects.all():
            self.assertEqual((item.supplier_hs_raw,item.supplier_elements_raw,item.supplier_tax_rate_raw),("","",""))

    def test_missing_image_column_is_read_failed(self):
        result=self.upload(data=sample(image_column=False))
        self.assertEqual(result["image_counts"],dict(available=0,absent=0,read_failed=3))
        self.assertTrue(all(i.error_message for i in ItemImage.objects.all()))

    def test_same_file_new_version_requires_confirmation(self):
        first=self.upload()
        args=dict(task_id=first["task_id"],expected_current_version_id=first["version_id"],key="new-version")
        with self.assertRaises(ApiError) as ctx:self.upload(**args)
        self.assertEqual(ctx.exception.code,"duplicate_file");self.assertEqual(FileVersion.objects.count(),1)
        second=self.upload(**args,confirm_duplicate=True)
        self.assertEqual(second["version_number"],2);self.assertNotEqual(first["precheck_run_id"],second["precheck_run_id"])
        self.assertEqual(self.upload(**args,confirm_duplicate=True)["version_id"],second["version_id"])

    def test_reordered_v2_keeps_v1_and_does_not_inherit_confirmations(self):
        first=self.upload();old=FileVersion.objects.get(pk=first["version_id"])
        claim_task(self.operator,first["task_id"],"claim")
        olditem=old.items.first()
        Confirmation.objects.create(item=olditem,revision=1,confirmed_hs="0000000000",created_by=self.operator)
        snapshots=list(old.items.values_list("pk","name","source_row"));images=list(ItemImage.objects.values_list("pk","storage_path"))
        newdata=sample(order=(3,1,2))
        second=self.upload(task_id=first["task_id"],expected_current_version_id=first["version_id"],key="v2",data=newdata)
        new=FileVersion.objects.get(pk=second["version_id"])
        self.assertEqual(list(old.items.values_list("pk","name","source_row")),snapshots)
        self.assertFalse(Confirmation.objects.filter(item__version=new).exists())
        self.assertEqual(files.read_version_original(self.supplier,old.pk).content,self.data)
        self.assertEqual(files.read_version_original(self.supplier,new.pk).content,newdata)
        self.assertEqual(list(ItemImage.objects.filter(item__version=old).values_list("pk","storage_path")),images)
        for image in ItemImage.objects.filter(item__version=new,status="available"):
            self.assertIn(str(new.pk),image.storage_path);self.assertNotIn(str(old.pk),image.storage_path)
        self.assertEqual(new.items.get(source_row=2).name,"合成部件3")
        self.assertEqual(new.items.get(source_row=2).images.get().status,"read_failed")
        self.assertEqual(Run.objects.filter(version=old,is_current=True).count(),1)
        self.assertEqual(Run.objects.filter(version=new,is_current=True).count(),1)

    def test_stale_version_rejected(self):
        first=self.upload()
        with self.assertRaises(ApiError) as ctx:self.upload(task_id=first["task_id"],expected_current_version_id="wrong",key="v2")
        self.assertEqual(ctx.exception.code,"stale_version")

    def test_publish_failure_is_explicit_and_retry_reuses_reservation(self):
        with patch.object(files,"publish_staged_version_original",side_effect=ApiError(500,"storage_unavailable","disk failure")):
            with self.assertRaises(ApiError) as ctx:self.upload()
        self.assertEqual(ctx.exception.code,"import_failed");self.assertEqual(ImportJob.objects.get().status,"failed")
        self.assertEqual(Item.objects.count(),0);self.assertFalse(Run.objects.filter(status="succeeded").exists())
        ids=(FileVersion.objects.get().pk,Run.objects.get().pk)
        result=self.upload()
        self.assertEqual(result["parse_status"],"succeeded");self.assertEqual((FileVersion.objects.get().pk,Run.objects.get().pk),ids)

    def test_interrupted_after_publish_rolls_back_items_then_resumes(self):
        with patch.object(imports,"execute_precheck",side_effect=RuntimeError("simulated interrupt")):
            with self.assertRaises(ApiError):self.upload()
        job=ImportJob.objects.get();self.assertEqual(job.status,"failed");self.assertEqual(Item.objects.count(),0)
        self.assertTrue(list(self.root.glob("tasks/*/versions/*/original/*")))
        self.assertEqual(imports.finish_import(job.pk)["item_count"],3)
        self.assertEqual(Run.objects.count(),1);self.assertEqual(RunItem.objects.count(),3)

    def test_staging_failure_does_not_reserve_version(self):
        with patch.object(files,"stage_version_original",side_effect=ApiError(500,"storage_unavailable","full")):
            with self.assertRaises(ApiError):self.upload()
        self.assertEqual(Task.objects.count(),0)

    def test_transaction_failure_discards_stage(self):
        with patch.object(ImportJob.objects,"create",side_effect=IntegrityError("test rollback")):
            with self.assertRaises(IntegrityError):self.upload()
        self.assertEqual(Task.objects.count(),0);self.assertFalse(list(self.root.glob("incoming/*.xlsx")))

    def test_claim_only_minimum_scope_and_object_permissions(self):
        first=self.upload();tid=first["task_id"];vid=first["version_id"]
        self.assertTrue(claimable_tasks(self.operator).filter(pk=tid).exists())
        for user in (self.operator,self.operator2,self.other,self.expert):
            with self.assertRaises(Http404):files.read_version_original(user,vid)
        claimed=claim_task(self.operator,tid,"claim")
        self.assertEqual(claimed["operator_id"],str(self.operator.pk))
        self.assertTrue(claim_task(self.operator,tid,"claim")["idempotent_replay"])
        self.assertTrue(claim_task(self.operator,tid,"another-key")["idempotent_replay"])
        self.assertEqual(TaskClaim.objects.count(),1);self.assertEqual(TaskAssignment.objects.count(),1)
        self.assertEqual(AuditEvent.objects.filter(action="task.claim").count(),1)
        self.assertEqual(files.read_version_original(self.operator,vid).content,self.data)
        with self.assertRaises(ApiError) as ctx:claim_task(self.operator2,tid,"claim2")
        self.assertEqual(ctx.exception.code,"task_already_claimed")
        with self.assertRaises(Http404):files.read_version_original(self.operator2,vid)
        with override_settings(CUSTOMS_CLAIM_SCOPE_POLICY="tests.p06.test_imports.no_claim_scope"):
            self.assertFalse(claimable_tasks(self.operator).exists())
            self.assertTrue(claim_task(self.operator,tid,"claim")["idempotent_replay"])
            with self.assertRaises(Http404):claim_task(self.operator2,tid,"claim")

    def test_role_checks(self):
        for user in (self.operator,self.expert):
            with self.assertRaises(PermissionDenied):imports.upload(user,filename="x.xlsx",data=self.data,key="k")
        for user in (self.supplier,self.expert):
            with self.assertRaises(PermissionDenied):claimable_tasks(user)

    def test_http_real_multipart_claim_report_and_download(self):
        self.client.force_login(self.supplier)
        response=self.client.post("/api/v1/tasks",{"file":SimpleUploadedFile("x.xlsx",self.data)},HTTP_IDEMPOTENCY_KEY="http")
        self.assertEqual(response.status_code,200,response.content)
        result=response.json();tid=result["task_id"];vid=result["version_id"]
        self.assertEqual(self.client.get(f"/api/v1/versions/{vid}/import").status_code,200)
        self.assertEqual(self.client.get(result["original_download_url"]).content,self.data)
        available=ItemImage.objects.get(status="available")
        self.assertEqual(self.client.get(f"/api/v1/files/{available.pk}").status_code,200)
        self.client.force_login(self.operator)
        pool=self.client.get("/api/v1/tasks/claimable")
        self.assertEqual(pool.status_code,200,pool.content)
        self.assertEqual(set(pool.json()["items"][0]),{"task_id","created_at","assignment_status"})
        for path in (f"/tasks/{tid}",f"/versions/{vid}/items",f"/versions/{vid}/import",f"/files/{vid}",f"/files/{available.pk}"):
            self.assertEqual(self.client.get("/api/v1"+path).status_code,404,path)
        claim=self.client.post(f"/api/v1/tasks/{tid}/claim",HTTP_IDEMPOTENCY_KEY="claim")
        self.assertEqual(claim.status_code,200,claim.content)
        self.assertEqual(self.client.get(f"/api/v1/tasks/{tid}").status_code,200)
        card=Item.objects.get(pk=available.item_id)
        payload=self.client.get(f"/api/v1/versions/{vid}/items/{card.pk}").json()
        self.assertEqual(payload["images"][0]["download_url"],f"/api/v1/files/{available.pk}")
        self.assertEqual(self.client.get("/api/v1/files/..%2F..%2F.env").status_code,404)
        self.client.logout();self.assertEqual(self.client.get(f"/api/v1/files/{vid}").status_code,401)
        self.assertEqual(self.client.get("/api/v1/tasks/claimable").status_code,401)
        self.client.force_login(self.supplier)
        body={"file":SimpleUploadedFile("x.xlsx",self.data),"expected_current_version_id":vid}
        duplicate=self.client.post(f"/api/v1/tasks/{tid}/versions",body,HTTP_IDEMPOTENCY_KEY="http-v2")
        self.assertEqual(duplicate.status_code,409,duplicate.content)
        self.assertEqual(duplicate.json()["error"]["code"],"duplicate_file")
        body={"file":SimpleUploadedFile("x.xlsx",self.data),"expected_current_version_id":vid,"confirm_duplicate":"true"}
        second=self.client.post(f"/api/v1/tasks/{tid}/versions",body,HTTP_IDEMPOTENCY_KEY="http-v2-confirmed")
        self.assertEqual(second.status_code,200,second.content)
        self.assertEqual(second.json()["version_number"],2)
        self.assertNotEqual(second.json()["precheck_run_id"],result["precheck_run_id"])

    def test_http_roles_and_csrf(self):
        from django.test import Client
        c=Client(enforce_csrf_checks=True);c.force_login(self.supplier)
        self.assertEqual(c.post("/api/v1/tasks",{"file":SimpleUploadedFile("x.xlsx",self.data)},HTTP_IDEMPOTENCY_KEY="k").status_code,403)
        for user in (self.supplier,self.expert):
            self.client.force_login(user);self.assertEqual(self.client.get("/api/v1/tasks/claimable").status_code,403)
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post("/api/v1/tasks",{"file":SimpleUploadedFile("x.xlsx",self.data)},HTTP_IDEMPOTENCY_KEY="k").status_code,403)

    def test_merged_declaration_and_split_packing_keep_all_members(self):
        from tests.p04.builders import build_package
        from tests.p05.workbooks import package_bytes
        package=build_package(declaration_rows=[
            ["C","1-2","合并部件","","",None,None], ["C","3","独立部件","","",None,None]],packing_rows=[
            ["C","1","部件","","A"], ["C","1","部件","","B"], ["C","2","部件","","C"], ["C","3","部件","","D"]])
        result=self.upload(data=package_bytes(package))
        self.assertEqual((result["item_count"],result["member_count"]),(2,4))
        self.assertEqual(list(Item.objects.order_by("source_row").values_list("members__source_row",flat=True)),[5,6,7,8])
        self.assertEqual(Item.objects.get(source_row=2).members.count(),3)

    def test_unmatched_member_is_distinct_from_parse_or_storage_failure(self):
        from tests.p04.builders import build_package
        from tests.p05.workbooks import package_bytes
        package=build_package(declaration_rows=[["C","99","无匹配部件","","",None,None]],packing_rows=[["C","1","部件"]])
        result=self.upload(data=package_bytes(package));self.assertEqual(result["parse_status"],"succeeded")
        self.assertEqual(result["member_count"],0)
        self.assertTrue(any(i["code"].startswith("missing_packing_member") for i in ImportJob.objects.get().report["links"][0]["issues"]))

    def test_missing_name_and_p04_key_errors_block_before_reservation(self):
        from tests.p04.builders import build_package
        from tests.p05.workbooks import package_bytes
        cases=[("3-1","部件"),("x","部件"),("1,1","部件"),("1",None)]
        for key,name in cases:
            with self.subTest(key=key,name=name):
                data=package_bytes(build_package(declaration_rows=[["C",key,name,"0000000000","要素"]],packing_rows=[["C","1","部件"]]))
                with self.assertRaises(ApiError) as ctx:self.upload(data=data)
                issues=ctx.exception.details["issues"]
                self.assertTrue(all(i["sheet"] and i["cell"] and i["row"]>0 for i in issues))
                self.assertFalse(Task.objects.exists())

    def test_overlong_raw_hs_and_tax_are_preserved(self):
        from tests.p04.builders import build_package
        from tests.p05.workbooks import package_bytes
        raw="原始异常值 "*20
        data=package_bytes(build_package(declaration_rows=[["C","1","部件",raw,"",raw]],packing_rows=[["C","1","部件"]]))
        self.upload(data=data)
        self.assertEqual((Item.objects.get().supplier_hs_raw,Item.objects.get().supplier_tax_rate_raw),(raw,raw))

    def test_precheck_row_failure_is_reported_without_success_fabrication(self):
        from adapters.engines.mock import EngineTechnicalError
        from adapters.engines.protocols import get_engine
        with patch.object(type(get_engine()),"recommend",side_effect=EngineTechnicalError("mock unavailable")):
            result=self.upload()
        self.assertEqual(result["parse_status"],"succeeded")
        self.assertEqual(result["precheck_status"],"failed")
        self.assertEqual(Run.objects.count(),1);self.assertEqual(RunItem.objects.filter(status="failed").count(),3)
        self.assertFalse(RunItem.objects.exclude(suggested_hs=None).exists())

    def test_other_supplier_expert_and_losing_operator_cannot_read_any_details(self):
        result=self.upload();vid=result["version_id"];tid=result["task_id"]
        claim_task(self.operator,tid,"claim")
        item=Item.objects.first();image=ItemImage.objects.filter(status="available").get()
        for user in (self.other,self.expert,self.operator2):
            self.client.force_login(user)
            for path in (f"/tasks/{tid}",f"/tasks/{tid}/history",f"/versions/{vid}/items",f"/versions/{vid}/items/{item.pk}",
                         f"/versions/{vid}/import",f"/runs/{result['precheck_run_id']}",f"/files/{vid}",f"/files/{image.pk}"):
                self.assertEqual(self.client.get("/api/v1"+path).status_code,404,(user.role,path))
            response=self.client.post(f"/api/v1/tasks/{tid}/versions",{"file":SimpleUploadedFile("x.xlsx",self.data),"expected_current_version_id":vid},HTTP_IDEMPOTENCY_KEY="forbidden-upload")
            self.assertEqual(response.status_code,404)

    def test_latest_failed_version_is_not_offered_for_claim(self):
        first=self.upload()
        with patch.object(files,"publish_staged_version_original",side_effect=ApiError(500,"storage_unavailable","test")):
            with self.assertRaises(ApiError):self.upload(task_id=first["task_id"],expected_current_version_id=first["version_id"],key="failed-v2",data=sample(order=(2,3,1)))
        self.assertFalse(claimable_tasks(self.operator).exists())

    def test_image_component_failure_keeps_products(self):
        with patch.object(files,"extract_version_images_for_version",side_effect=ApiError(422,"workbook_invalid_xml","broken image definitions")):
            result=self.upload()
        self.assertEqual(result["parse_status"],"succeeded")
        self.assertEqual(result["image_counts"],dict(available=0,absent=0,read_failed=3))
        self.assertEqual(Item.objects.count(),3)
        self.assertTrue(all(i.error_message.startswith("workbook_invalid_xml") for i in ItemImage.objects.all()))

    def test_image_storage_failure_is_not_image_absence(self):
        with patch.object(files,"extract_version_images_for_version",side_effect=ApiError(500,"storage_unavailable","disk full")):
            with self.assertRaises(ApiError) as ctx:self.upload()
        self.assertEqual(ctx.exception.code,"import_failed")
        self.assertEqual(ImportJob.objects.get().error["code"],"storage_unavailable")
        self.assertFalse(ItemImage.objects.exists());self.assertFalse(Item.objects.exists())

    @override_settings(DEBUG=True)
    def test_p06_page_is_live_and_static_assets_are_available(self):
        response=self.client.get("/uploads/")
        self.assertEqual(response.status_code,200)
        content=b"".join(response.streaming_content).decode()
        self.assertIn("LIVE API",content);self.assertIn("/workbench/assets/pages/p06.js",content)
        for asset in ("pages/p06.js","styles/p06.css"):
            self.assertEqual(self.client.get("/workbench/assets/"+asset).status_code,200)

    def test_key_required_and_invalid_upload_errors_are_json(self):
        self.client.force_login(self.supplier)
        response=self.client.post("/api/v1/tasks",{"file":SimpleUploadedFile("x.xlsx",self.data)})
        self.assertEqual(response.status_code,422);self.assertEqual(response.json()["error"]["code"],"invalid_idempotency_key")
        response=self.client.post("/api/v1/tasks",{},content_type="application/json")
        self.assertEqual(response.status_code,422);self.assertEqual(response.json()["error"]["code"],"validation_error")
        self.assertFalse(Task.objects.exists())

    def test_recommendation_only_receives_allowlisted_scalar_facts(self):
        from adapters.engines.protocols import get_engine
        from apps.runs.inline import _recommend_attributes
        captured=[];engine=get_engine();recommend=type(engine).recommend
        def capture(obj,payload):
            captured.append(payload)
            return recommend(obj,payload)
        with patch.object(type(engine),'recommend',capture):self.upload()
        self.assertEqual(len(captured),3)
        for payload in captured:
            self.assertNotIn('excel_fields',payload.attributes)
            self.assertNotIn('existing_opinion',payload.attributes)
            self.assertNotIn('refund_rate',payload.attributes)
            self.assertNotIn('0000000000',str(payload.attributes))
            self.assertNotIn('历史备注原文',str(payload.attributes))
        self.assertEqual(_recommend_attributes({'material':'钢','specification':'12R20','name':{'hs':'1234567890'},'hs':'1234567890','unknown':'old result'}),
                         {'material':'钢','specification':'12R20'})
        self.assertIn('excel_fields',Item.objects.first().attributes)
        self.assertEqual(Item.objects.first().supplier_hs_raw,'0000000000')

    def test_broken_optional_drawing_keeps_products_and_warns_about_hidden_rows(self):
        from tests.p06.samples import broken_drawing
        result=self.upload(data=broken_drawing(self.data))
        self.assertEqual(result['parse_status'],'succeeded')
        self.assertEqual((result['item_count'],result['member_count']),(3,3))
        self.assertEqual(result['image_counts'],{'available':1,'absent':0,'read_failed':2})
        job=ImportJob.objects.get()
        issue=next(i for i in job.report['issues'] if i['code']=='image_discovery_failed')
        self.assertEqual((issue['sheet'],issue['cell'],issue['row']),('节点①申报要素','G1',1))
        self.assertTrue(issue['details']['may_hide_image_only_rows'])
        self.assertEqual(Item.objects.get(source_row=3).images.get().status,'read_failed')
        self.assertIn('floating_image_scan_failed',Item.objects.get(source_row=3).images.get().error_message)

    def test_unrelated_broken_drawing_does_not_poison_declaration_images(self):
        from tests.p06.samples import broken_drawing
        result=self.upload(data=broken_drawing(self.data,sheet_number=2))
        self.assertEqual(result['image_counts'],{'available':1,'absent':1,'read_failed':1})
        self.assertFalse(any(i['code']=='image_discovery_failed' for i in ImportJob.objects.get().report['issues']))
