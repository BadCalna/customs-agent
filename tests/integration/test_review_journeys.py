"""P21 S01-S03/A10-A17: real public API journeys with synthetic mock AI.

These tests establish technical flow and isolation, not HS accuracy, expert
gold-standard quality, browser usability or a real notification delivery.
"""
import io
from unittest.mock import patch
from zipfile import ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.notifications.models import Message, NotificationDelivery
from apps.reviews.models import (
    AuditEvent, Confirmation, Decision, DecisionItem, FileVersion, ImportJob, Issue,
    Item, ItemImage, ItemMember, Submission, SubmissionItem, Task,
)
from apps.runs.models import Run, RunItem
from tests.p04.fixtures import png
from tests.p06.samples import sample

from .journey_support import (
    CONFIRMED_HS, RECODED_HS, JourneyCase, amended_workbook,
    changed_members_workbook, exportable_sample, grouped_workbook,
)


class ReviewJourneyTests(JourneyCase):
    def test_t01_fatal_parse_creates_no_success_or_residual_business_objects(self):
        failures = (
            ("missing_sheet", sample(missing_sheet=True)),
            ("missing_key", sample(missing_key=True)),
            ("ambiguous_ownership", sample(ambiguous=True)),
        )
        for name, data in failures:
            with self.subTest(upload="initial", failure=name):
                response = self.sessions[self.supplier.pk].post(
                    "/api/v1/tasks", {"file": SimpleUploadedFile("fatal.xlsx", data)},
                    HTTP_IDEMPOTENCY_KEY=f"fatal-initial-{name}",
                )
                self.assertEqual(response.status_code, 422, response.content)
                self.assertEqual(response.json()["error"]["code"], "parse_blocked")
                for model in (Task, FileVersion, ImportJob, Item, ItemImage, ItemMember,
                              Run, RunItem, Confirmation, Submission, Decision, AuditEvent,
                              Message, NotificationDelivery):
                    self.assertFalse(model.objects.exists(), model.__name__)
                self.assertFalse(any(path.is_file() for path in self.file_root.rglob("*")))

        task, version, _ = self.upload()
        self.claim(task)
        submitted = self.submit(task, version)
        self.approve(submitted, version)
        before = self.business_state(task)
        versions = list(FileVersion.objects.order_by("pk").values())
        jobs = list(ImportJob.objects.order_by("pk").values())
        files = {path.relative_to(self.file_root): path.read_bytes()
                 for path in self.file_root.rglob("*") if path.is_file()}
        for name, data in failures:
            with self.subTest(upload="replacement", failure=name):
                response = self.sessions[self.supplier.pk].post(
                    f"/api/v1/tasks/{task.pk}/versions",
                    {"file": SimpleUploadedFile("fatal.xlsx", data),
                     "expected_current_version_id": str(version.pk)},
                    HTTP_IDEMPOTENCY_KEY=f"fatal-replacement-{name}",
                )
                self.assertEqual(response.status_code, 422, response.content)
                self.assertEqual(response.json()["error"]["code"], "parse_blocked")
                self.assertEqual(self.business_state(task), before)
                self.assertEqual(list(FileVersion.objects.order_by("pk").values()), versions)
                self.assertEqual(list(ImportJob.objects.order_by("pk").values()), jobs)
                self.assertEqual({path.relative_to(self.file_root): path.read_bytes()
                                  for path in self.file_root.rglob("*") if path.is_file()}, files)
        self.assertEqual(AuditEvent.objects.filter(action="import.succeeded").count(), 1)
        self.assertEqual(Run.objects.filter(kind=Run.Kind.PRECHECK, status=Run.Status.SUCCEEDED).count(), 1)

    def test_a12_changed_name_elements_image_and_original_hs_require_full_new_review(self):
        task, old, old_run = self.upload()
        self.claim(task)
        submitted = self.submit(task, old)
        self.approve(submitted, old)
        old_item = old.items.get(source_row=2)
        old_image = old_item.images.get()
        old_image_bytes = (self.file_root / old_image.storage_path).read_bytes()
        old_facts = self.card(old, old_item)["facts"]
        old_source = self.original(old)
        task, new, new_run = self.upload(data=amended_workbook(), task=task, previous=old)
        self.assert_new_review(task, old, new, old_run, new_run)
        item = new.items.get(source_row=2)
        facts = self.card(new, item)["facts"]
        self.assertEqual(facts["name"], "合成部件1修订品名")
        self.assertEqual(facts["supplier_hs_raw"], "1111111111")
        self.assertIn("SYNTHETIC-SPEC-V2", facts["supplier_elements_raw"])
        image = item.images.get()
        self.assertEqual((image.status, image.source_cell), ("available", "G2"))
        self.assertNotEqual(image.pk, old_image.pk)
        self.assertIn(str(new.pk), image.storage_path)
        self.assertEqual((self.file_root / image.storage_path).read_bytes(), png(b"\xc0\x20\x70"))
        self.assertNotEqual((self.file_root / image.storage_path).read_bytes(), old_image_bytes)
        self.assertEqual((self.file_root / old_image.storage_path).read_bytes(), old_image_bytes)
        self.assertEqual(self.card(old, old_item)["facts"], old_facts)
        self.assertEqual(self.original(old), old_source)
        self.assertEqual(Submission.objects.get(pk=submitted["submission_id"]).status, Submission.Status.APPROVED)
        renewed = self.submit(task, new)
        self.assertNotEqual(renewed["submission_id"], submitted["submission_id"])
        self.assertEqual(Submission.objects.get(pk=renewed["submission_id"]).version, new)

    def test_a13_supplemental_facts_only_still_require_full_new_review(self):
        task, old, old_run = self.upload()
        self.claim(task)
        submitted = self.submit(task, old)
        self.approve(submitted, old)
        old_item = old.items.get(source_row=2)
        old_image = old_item.images.get()
        old_results = list(old_run.items.order_by("item_id").values("item_id", "result"))
        task, new, new_run = self.upload(data=amended_workbook(facts_only=True), task=task, previous=old)
        self.assert_new_review(task, old, new, old_run, new_run)
        self.assertEqual(list(new.items.order_by("source_row").values_list("name", "supplier_hs_raw")),
                         list(old.items.order_by("source_row").values_list("name", "supplier_hs_raw")))
        item = new.items.get(source_row=2)
        self.assertNotEqual(item.supplier_elements_raw, old_item.supplier_elements_raw)
        self.assertIn("材质：合成材料", item.supplier_elements_raw)
        image = item.images.get()
        self.assertEqual((self.file_root / image.storage_path).read_bytes(),
                         (self.file_root / old_image.storage_path).read_bytes())
        self.assertNotEqual(image.storage_path, old_image.storage_path)
        self.assertEqual({row.item_id for row in new_run.items.all()}, set(new.items.values_list("pk", flat=True)))
        self.assertEqual(list(old_run.items.order_by("item_id").values("item_id", "result")), old_results)
        self.assertEqual(Submission.objects.get(pk=submitted["submission_id"]).status, Submission.Status.APPROVED)

    def test_a16_split_then_merge_reconstructs_rows_images_members_without_approval(self):
        task, merged, merged_run = self.upload(data=grouped_workbook(merged=True, generation=1))
        self.claim(task)
        first_submission = self.submit(task, merged)
        self.approve(first_submission, merged)
        original = self.original(merged)
        task, split, split_run = self.upload(
            data=grouped_workbook(merged=False, generation=2), task=task, previous=merged,
        )
        self.assert_new_review(task, merged, split, merged_run, split_run)
        self.assertEqual(list(split.items.order_by("source_row").values_list("name", flat=True)),
                         ["合成部件3", "合成部件1", "合成部件2"])
        for item in split.items.all():
            number = int(item.name[-1])
            self.assertEqual(list(item.members.values_list("item_no", "source_row")), [(str(number), number + 4)])
            image = item.images.get()
            self.assertEqual((image.status, image.source_cell), ("available", f"G{item.source_row}"))
            self.assertIn(str(split.pk), image.storage_path)
            self.assertEqual((self.file_root / image.storage_path).read_bytes(), png(bytes((2, number, 128))))
        split_submission = self.submit(task, split)
        self.approve(split_submission, split)
        retained = {
            model: list(model.objects.filter(**lookup).order_by("pk").values())
            for model, lookup in (
                (Item, {"version__in": [merged, split]}),
                (ItemMember, {"item__version__in": [merged, split]}),
                (ItemImage, {"item__version__in": [merged, split]}),
                (Confirmation, {"item__version__in": [merged, split]}),
                (SubmissionItem, {"submission__version__in": [merged, split]}),
                (DecisionItem, {"decision__submission__version__in": [merged, split]}),
            )
        }
        task, rebuilt, rebuilt_run = self.upload(
            data=grouped_workbook(merged=True, generation=3), task=task, previous=split,
        )
        self.assert_new_review(task, split, rebuilt, split_run, rebuilt_run)
        item = rebuilt.items.get()
        self.assertEqual(list(item.members.order_by("source_row").values_list("item_no", "source_row")),
                         [("1", 5), ("2", 6), ("3", 7)])
        image = item.images.get()
        self.assertEqual((image.status, image.source_cell), ("available", "G2"))
        self.assertEqual((self.file_root / image.storage_path).read_bytes(), png(bytes((3, 0, 128))))
        for model, rows in retained.items():
            self.assertEqual(list(model.objects.filter(pk__in=[row["id"] for row in rows]).order_by("pk").values()), rows)
        for model, lookup in ((Item, "version"), (ItemMember, "item__version"), (ItemImage, "item__version")):
            groups = [set(model.objects.filter(**{lookup: version}).values_list("pk", flat=True))
                      for version in (merged, split, rebuilt)]
            self.assertTrue(all(groups))
            self.assertEqual(len(set.union(*groups)), sum(map(len, groups)))
        self.assertEqual(self.original(merged), original)
        self.assertEqual(Submission.objects.filter(pk__in=[first_submission["submission_id"], split_submission["submission_id"]],
                                                   status=Submission.Status.APPROVED).count(), 2)

    def test_t07_notification_failure_preserves_approval_and_replay_deduplicates(self):
        self.assert_failed_notification_preserves_decision("approve")

    def test_t07_notification_failure_preserves_return_and_replay_deduplicates(self):
        self.assert_failed_notification_preserves_decision("return")

    def assert_failed_notification_preserves_decision(self, action):
        task, version, _ = self.upload()
        self.claim(task)
        submitted = self.submit(task, version)
        body = {"action": action, "reason": "P21 通知失败合成验收"}
        if action == "approve":
            body["items"] = [{"item_id": str(item.pk), "final_hs": CONFIRMED_HS}
                             for item in version.items.all()]
        else:
            body.update(return_to="operator", issues=[
                {"item_id": str(version.items.first().pk), "message": "请补充合成资料"},
            ])
        path = f"/api/v1/submissions/{submitted['submission_id']}/decisions"
        with patch("socket.create_connection", side_effect=AssertionError("P21 must stay offline")) as network, \
                patch("adapters.notifications.dws.MockDwsTransport.send", side_effect=TimeoutError("synthetic timeout")) as send:
            response = self.request(self.expert, "POST", path, body, key=f"failed-notification-{action}").json()
            decision = Decision.objects.get(pk=response["decision_id"])
            task.refresh_from_db()
            submission = Submission.objects.get(pk=submitted["submission_id"])
            expected_stage = Task.Stage.APPROVED if action == "approve" else Task.Stage.RETURNED
            expected_status = Submission.Status.APPROVED if action == "approve" else Submission.Status.RETURNED
            self.assertEqual((task.stage, submission.status, decision.action), (expected_stage, expected_status, action))
            self.assertEqual(decision.created_by, self.expert)
            self.assertEqual(decision.items.count(), version.items.count() if action == "approve" else 0)
            messages = Message.objects.filter(decision=decision)
            self.assertEqual(messages.count(), 2 if action == "approve" else 1)
            delivery = NotificationDelivery.objects.get(message__decision=decision)
            self.assertEqual((delivery.status, delivery.attempts, delivery.last_error_code), ("pending", 1, "transport_failed"))
            self.assertEqual(delivery.message.recipient, self.operator)
            self.assertIsNotNone(delivery.next_attempt_at)
            self.assertIsNone(delivery.lease_token)
            self.assertEqual(delivery.receipt, "")
            attempt = delivery.history.get()
            self.assertEqual((attempt.status, attempt.error_code), ("failed", "transport_failed"))
            self.assertIsNotNone(attempt.finished_at)
            after_failure = self.business_state(task)
            attempt_state = list(delivery.history.values())
            replay = self.request(self.expert, "POST", path, body, key=f"failed-notification-{action}").json()
            self.assertEqual(replay["decision_id"], response["decision_id"])
            self.assertEqual(self.business_state(task), after_failure)
            self.assertEqual(list(delivery.history.values()), attempt_state)
            self.assertEqual(send.call_count, 1)
            network.assert_not_called()

    def test_s01_upload_claim_confirm_submit_ai_then_human_approval(self):
        source = sample()
        task, version, precheck = self.upload(data=source)
        self.assertEqual(precheck.kind, Run.Kind.PRECHECK)
        self.assertFalse(Confirmation.objects.exists())
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(Decision.objects.exists())
        self.assertEqual(self.original(version), source)
        self.claim(task)
        submitted = self.submit(task, version)
        submission = Submission.objects.get(pk=submitted["submission_id"])
        expert_run = Run.objects.get(pk=submitted["expert_run_id"])
        self.assertEqual(expert_run.status, Run.Status.SUCCEEDED)
        self.assertEqual(expert_run.submission, submission)
        self.assertEqual(expert_run.version, version)
        task.refresh_from_db()
        self.assertEqual(task.stage, Task.Stage.SUBMITTED)
        self.assertFalse(Decision.objects.exists(), "AI completion must never create approval")
        for row in expert_run.items.all():
            self.assertEqual(row.result["target_hs"], CONFIRMED_HS)
            self.assertEqual(row.result["result_mode"], "mock")
        approved = self.approve(submitted, version)
        task.refresh_from_db()
        submission.refresh_from_db()
        self.assertEqual((task.stage, submission.status), (Task.Stage.APPROVED, Submission.Status.APPROVED))
        decision = Decision.objects.get(pk=approved["decision_id"])
        self.assertEqual(decision.created_by, self.expert)
        self.assertIsNotNone(decision.created_at)
        self.assertEqual(decision.items.count(), version.items.count())
        for item in version.items.all():
            card = self.card(version, item)
            self.assertEqual(card["facts"]["supplier_hs_raw"], "0000000000")
            self.assertEqual(card["business_confirmed_hs"]["value"], CONFIRMED_HS)
            self.assertEqual(card["submitted_hs"]["value"], CONFIRMED_HS)
            self.assertEqual(card["expert_final_hs"]["value"], CONFIRMED_HS)
        self.assertEqual(self.original(version), source)

    def test_t09_expert_recode_preserves_submitted_snapshot_and_ai_target(self):
        task, version, _ = self.upload()
        self.claim(task)
        submitted = self.submit(task, version)
        expert_run = Run.objects.get(pk=submitted["expert_run_id"])
        item = version.items.order_by("source_row").first()
        before = expert_run.items.get(item=item).result
        self.approve(submitted, version, recode=True)
        line = SubmissionItem.objects.get(submission_id=submitted["submission_id"], item=item)
        decision_line = DecisionItem.objects.get(item=item)
        card = self.card(version, item)
        self.assertEqual(line.submitted_hs, CONFIRMED_HS)
        self.assertEqual((decision_line.previous_hs, decision_line.final_hs), (CONFIRMED_HS, RECODED_HS))
        self.assertEqual(decision_line.change_reason, "P21 合成专家改码原因")
        self.assertEqual(card["expert_final_hs"]["value"], RECODED_HS)
        self.assertEqual(card["latest_expert_check"]["target_hs"], CONFIRMED_HS)
        self.assertEqual(expert_run.items.get(item=item).result, before)
        event = AuditEvent.objects.get(task=task, action="decision.recoded")
        self.assertEqual((event.previous_value, event.new_value, event.actor), (CONFIRMED_HS, RECODED_HS, self.expert))

    def test_s02_operator_return_then_reordered_v2_requires_full_new_review(self):
        task, old, old_run = self.upload()
        self.claim(task)
        self.confirm_all(old)
        task.refresh_from_db()
        item = old.items.order_by("source_row").first()
        response = self.request(
            self.operator, "POST", f"/api/v1/tasks/{task.pk}/returns",
            {"version_id": str(old.pk), "revision": task.revision, "reason": "需补用途",
             "issues": [{"item_id": str(item.pk), "message": "请补充技术资料"}]},
        ).json()
        self.assertEqual(response["stage"], "returned")
        self.assertEqual(Issue.objects.get(task=task, kind="operator_return").created_by, self.operator)
        self.assertTrue(Message.objects.filter(recipient=self.supplier, task=task, kind="task.returned").exists())
        old_results = list(old_run.items.order_by("item_id").values("item_id", "result"))
        task, new, new_run = self.upload(data=sample(order=(3, 1, 2)), task=task, previous=old)
        self.assert_new_review(task, old, new, old_run, new_run)
        self.assertEqual(list(old_run.items.order_by("item_id").values("item_id", "result")), old_results)
        self.assertEqual(new.items.order_by("source_row").first().name, "合成部件3")
        submitted = self.submit(task, new)
        self.assertEqual(Submission.objects.get(pk=submitted["submission_id"]).version, new)

    def test_s03_expert_return_to_operator_then_v2_preserves_old_decision(self):
        self.expert_return_and_reupload("operator")

    def test_s03_expert_return_to_supplier_then_v2_preserves_old_decision(self):
        self.expert_return_and_reupload("supplier")

    def expert_return_and_reupload(self, recipient_role):
        task, old, old_run = self.upload()
        self.claim(task)
        submitted = self.submit(task, old)
        item = old.items.order_by("source_row").first()
        response = self.request(
            self.expert, "POST", f"/api/v1/submissions/{submitted['submission_id']}/decisions",
            {"action": "return", "return_to": recipient_role, "reason": "P21 专家待补事实",
             "issues": [{"item_id": str(item.pk), "message": "请补规格与用途"}]},
        ).json()
        decision = Decision.objects.get(pk=response["decision_id"])
        old_submission = Submission.objects.get(pk=submitted["submission_id"])
        self.assertEqual(old_submission.status, Submission.Status.RETURNED)
        self.assertEqual(decision.return_to, recipient_role)
        self.assertFalse(decision.items.exists(), "A return must not write final HS values")
        target = self.operator if recipient_role == "operator" else self.supplier
        message = Message.objects.get(decision=decision)
        self.assertEqual((message.recipient, message.version, message.submission), (target, old, old_submission))
        task, new, new_run = self.upload(data=sample(order=(2, 3, 1)), task=task, previous=old)
        self.assert_new_review(task, old, new, old_run, new_run)
        new_submission = self.submit(task, new)
        self.assertNotEqual(new_submission["submission_id"], submitted["submission_id"])
        self.assertNotEqual(new_submission["expert_run_id"], submitted["expert_run_id"])
        old_submission.refresh_from_db()
        self.assertEqual(old_submission.status, Submission.Status.RETURNED)
        self.assertEqual(Decision.objects.get(pk=decision.pk).return_to, recipient_role)

    def test_a14_approved_v1_does_not_approve_v2_or_inherit_human_values(self):
        task, old, old_run = self.upload()
        self.claim(task)
        submitted = self.submit(task, old)
        approved = self.approve(submitted, old)
        old_source = self.original(old)
        old_snapshot = list(SubmissionItem.objects.filter(submission_id=submitted["submission_id"])
                            .order_by("item_id").values("item_id", "submitted_hs", "item_revision"))
        task, new, new_run = self.upload(data=sample(order=(2, 1, 3)), task=task, previous=old)
        self.assert_new_review(task, old, new, old_run, new_run)
        self.assertEqual(self.original(old), old_source)
        self.assertEqual(list(SubmissionItem.objects.filter(submission_id=submitted["submission_id"])
                              .order_by("item_id").values("item_id", "submitted_hs", "item_revision")), old_snapshot)
        old_submission = Submission.objects.get(pk=submitted["submission_id"])
        self.assertEqual(old_submission.status, Submission.Status.APPROVED)
        history = self.request(self.operator, "GET", f"/api/v1/tasks/{task.pk}/history").json()
        by_id = {entry["version_id"]: entry for entry in history["versions"]}
        self.assertFalse(by_id[str(old.pk)]["is_current"])
        self.assertTrue(by_id[str(new.pk)]["is_current"])
        self.assertEqual(by_id[str(old.pk)]["submissions"][0]["decisions"][0]["decision_id"], approved["decision_id"])
        self.assertEqual(by_id[str(new.pk)]["submissions"], [])

    def test_a16_reordered_images_follow_new_rows_and_remain_version_scoped(self):
        task, old, old_run = self.upload()
        self.claim(task)
        old_images = list(ItemImage.objects.filter(item__version=old).order_by("pk")
                          .values("pk", "item_id", "status", "storage_path", "source_cell"))
        task, new, new_run = self.upload(data=sample(order=(3, 1, 2)), task=task, previous=old)
        self.assert_new_review(task, old, new, old_run, new_run)
        expected = {"合成部件1": "available", "合成部件2": "absent", "合成部件3": "read_failed"}
        for item in new.items.all():
            self.assertEqual(item.images.get().status, expected[item.name])
        available = ItemImage.objects.get(item__version=new, status="available")
        self.assertIn(str(new.pk), available.storage_path)
        self.assertNotIn(str(old.pk), available.storage_path)
        self.assertEqual(available.source_cell, "G3")
        self.assertEqual(list(ItemImage.objects.filter(item__version=old).order_by("pk")
                              .values("pk", "item_id", "status", "storage_path", "source_cell")), old_images)

    def test_a16_deleted_and_added_members_receive_new_identity_and_precheck(self):
        task, old, old_run = self.upload()
        self.claim(task)
        task, new, new_run = self.upload(data=changed_members_workbook(), task=task, previous=old)
        self.assert_new_review(task, old, new, old_run, new_run)
        self.assertEqual(set(new.items.values_list("name", flat=True)), {"合成部件1", "合成部件3", "合成部件4"})
        self.assertEqual(set(old.items.values_list("name", flat=True)), {"合成部件1", "合成部件2", "合成部件3"})
        self.assertEqual(new_run.items.count(), 3)
        self.assertEqual({row.item_id for row in new_run.items.all()}, set(new.items.values_list("pk", flat=True)))
        self.assertFalse(set(old.items.values_list("pk", flat=True)) & set(new.items.values_list("pk", flat=True)))
        self.assertEqual(sum(item.members.count() for item in new.items.all()), 3)

    def test_a10_a17_exports_are_downloads_without_approval_return_or_notification_side_effects(self):
        """A17 media-byte subset; P03/P20 own full formula/merge/client fidelity."""
        task, version, run = self.upload(data=exportable_sample())
        self.claim(task)
        original = self.original(version)
        before = self.business_state(task)
        content = self.export(version, run)
        with ZipFile(io.BytesIO(content)) as archive:
            text = "\n".join(archive.read(name).decode("utf-8", errors="replace")
                             for name in archive.namelist() if name.endswith(".xml"))
            sheet_text = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        # D2/Q02 forbids *item* IDs in supplier material. Task/version/run
        # provenance in document properties is intentional P20 audit metadata.
        for identifier in map(str, version.items.values_list("pk", flat=True)):
            self.assertNotIn(identifier, text, "Supplier workbook must not carry system item IDs")
        for identifier in (str(task.pk), str(version.pk), str(run.pk)):
            self.assertNotIn(identifier, sheet_text, "Supplier cells must not expose execution IDs")
        self.assertEqual(self.business_state(task), before)
        # This sample deliberately contains a missing DISPIMG resource. The
        # original-formula copy can preserve it; an Excel2016 fallback cannot.
        refused = self.request(
            self.operator, "POST", f"/api/v1/versions/{version.pk}/exports",
            {"kind": "precheck", "run_id": str(run.pk), "excel2016_images": True}, expected=422,
        ).json()
        self.assertEqual(refused["error"]["code"], "export_validation_failed")
        self.assertEqual(self.business_state(task), before)
        workbook = self.export(version, run, kind="precheck", excel2016_images=False)
        with ZipFile(io.BytesIO(original)) as old, ZipFile(io.BytesIO(workbook)) as new:
            media = [name for name in old.namelist() if name.startswith("xl/media/")]
            self.assertTrue(media)
            for name in media:
                self.assertEqual(new.read(name), old.read(name), name)
        self.assertEqual(self.original(version), original)
        self.assertEqual(self.business_state(task), before)
        submitted = self.submit(task, version)
        expert_run = Run.objects.get(pk=submitted["expert_run_id"])
        before = self.business_state(task)
        self.export(version, expert_run, submission_id=submitted["submission_id"])
        self.assertEqual(self.business_state(task), before)

    def test_t07_messages_are_recipient_scoped_and_keep_exact_historical_version(self):
        task, old, _ = self.upload()
        self.claim(task)
        submitted = self.submit(task, old)
        self.approve(submitted, old)
        expert_messages = self.request(self.expert, "GET", "/api/v1/messages").json()
        self.assertEqual(len(expert_messages["items"]), 1)
        target = expert_messages["items"][0]
        self.assertEqual(target["kind"], "submission.created")
        self.assertEqual(target["submission_id"], submitted["submission_id"])
        task, new, _ = self.upload(data=sample(order=(3, 2, 1)), task=task, previous=old)
        messages = self.request(self.expert, "GET", "/api/v1/messages").json()
        retained = next(row for row in messages["items"] if row["message_id"] == target["message_id"])
        self.assertEqual(retained["version_id"], str(old.pk))
        self.assertIn(str(old.pk), retained["target_url"])
        self.assertNotIn(str(new.pk), retained["target_url"])
        outsider = self.request(self.outsider, "GET", "/api/v1/messages").json()
        self.assertEqual((outsider["items"], outsider["unread_count"]), ([], 0))
        self.request(self.outsider, "PATCH", f"/api/v1/messages/{target['message_id']}", {"is_read": True}, expected=404)
        self.request(self.expert, "PATCH", f"/api/v1/messages/{target['message_id']}", {"is_read": True})
        messages = self.request(self.expert, "GET", "/api/v1/messages").json()
        self.assertEqual(messages["unread_count"], 0)
        self.assertTrue(messages["items"][0]["is_read"])
        self.assertFalse(NotificationDelivery.objects.exclude(status="mocked").exists())

    def assert_new_review(self, task, old, new, old_run, new_run):
        task.refresh_from_db()
        self.assertEqual(task.stage, Task.Stage.AWAITING_CONFIRMATION)
        self.assertEqual(new.number, old.number + 1)
        self.assertNotEqual(new_run.pk, old_run.pk)
        self.assertEqual(new_run.version, new)
        self.assertEqual(new_run.items.count(), new.items.count())
        self.assertFalse(Confirmation.objects.filter(item__version=new).exists())
        self.assertFalse(Submission.objects.filter(version=new).exists())
        self.assertFalse(Decision.objects.filter(submission__version=new).exists())
        for item in new.items.all():
            card = self.card(new, item)
            self.assertIsNone(card["business_confirmed_hs"]["value"])
            self.assertIsNone(card["submitted_hs"]["value"])
            self.assertIsNone(card["expert_final_hs"]["value"])
            self.assertEqual(card["precheck_suggested_hs"]["source_id"], str(new_run.pk))

    @staticmethod
    def business_state(task):
        task.refresh_from_db()
        # Full persisted row snapshots catch updates to existing values and
        # timestamps as well as inserts/deletes. Counts alone would miss them.
        querysets = {
            "confirmations": Confirmation.objects.filter(item__version__task=task),
            "submissions": Submission.objects.filter(task=task),
            "submission_items": SubmissionItem.objects.filter(submission__task=task),
            "decisions": Decision.objects.filter(submission__task=task),
            "decision_items": DecisionItem.objects.filter(decision__submission__task=task),
            "issues": Issue.objects.filter(task=task),
            "messages": Message.objects.filter(task=task),
            "deliveries": NotificationDelivery.objects.filter(message__task=task),
            "audit_events": AuditEvent.objects.filter(task=task),
            "runs": Run.objects.filter(task=task),
            "run_items": RunItem.objects.filter(run__task=task),
        }
        return {"task_stage": task.stage, "task_revision": task.revision,
                **{name: list(queryset.order_by("pk").values()) for name, queryset in querysets.items()}}
