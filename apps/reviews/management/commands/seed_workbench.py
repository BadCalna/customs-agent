"""独立 LIVE 工作台验收种子：两批均从待确认起跑，所有数据都是合成样例。

UUID5 的键是公开的本地验收标识，不含业务秘密。只在首次创建某批任务时，
于同一事务内创建其版本、合成预审及历史；已有任务整批跳过，人工确认、
提交、决定、问题状态、授权与新版本均不补写或重置。新账号才设置 demo 密码。
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from adapters.engines.mock import compare_original
from adapters.engines.protocols import Candidate, CompareInput, ExecutionContext, RecommendOutput
from apps.accounts.models import Role, TaskAssignment, User
from apps.api.schemas import EvidenceEntry, EvidenceStatus
from apps.runs.models import Run, RunItem

from ...models import (
    AuditEvent,
    Confirmation,
    Decision,
    FileVersion,
    Issue,
    Item,
    Submission,
    SubmissionItem,
    Task,
)

DISCLAIMER = "合成脱敏流程验收数据；编码、建议与依据均非真实归类结论，不证明专业有效性。"
SHEET = "申报要素"
ENGINE_VERSION = "workbench-synthetic-v1"
APPROVE_TITLE = "工作台验收-整批通过"
RETURN_TITLE = "工作台验收-整批退回"
ACCOUNTS = (
    ("workbench_supplier", "工作台验收供应商", Role.SUPPLIER),
    ("workbench_operator", "工作台验收业务员", Role.OPERATOR),
    ("workbench_expert", "工作台验收专家", Role.EXPERT),
)


def seed_id(key):
    """同一验收键在任意数据库里均产生同一 UUID；请勿修改已发布的前缀。"""
    return uuid5(NAMESPACE_URL, f"https://customs-agent.local/seed-workbench/v1/{key}")


@dataclass(frozen=True)
class ItemSpec:
    key: str
    row: int
    name: str
    supplier_hs: str
    suggested_hs: str
    expected_hs: str
    choice: str


APPROVE_ITEMS = (
    ItemSpec("cable", 5, "合成线缆组件 A", "8544422900", "8536901900", "8544422900", "supplier"),
    ItemSpec("controller", 6, "合成控制模块 B", "8537109090", "8543709990", "8543709990", "ai"),
    ItemSpec("bracket", 7, "合成安装支架 C", "7326909000", "7616999000", "3926909090", "manual"),
)
RETURN_V1_ITEMS = (
    ItemSpec("sensor", 5, "合成传感组件 R1", "9026100000", "9026809000", "9026809000", "ai"),
    ItemSpec("connector", 6, "合成连接组件 R2", "8536690000", "8544422900", "8536690000", "supplier"),
)
RETURN_V2_ITEMS = (
    ItemSpec("sensor", 5, "合成传感组件 R1", "9026201090", "9026900090", "9026900090", "ai"),
    ItemSpec("connector", 6, "合成连接组件 R2", "8536909000", "8544492900", "8536909000", "supplier"),
)
BATCHES = (
    ("approve", APPROVE_TITLE, ((1, APPROVE_ITEMS),)),
    ("return", RETURN_TITLE, ((1, RETURN_V1_ITEMS), (2, RETURN_V2_ITEMS))),
)
HISTORY_REASON = "合成 V1 历史退回：传感组件缺少量程与用途，连接组件缺少材质与连接方式；请补料重传。"
HISTORY_ISSUES = {
    "sensor": "合成 V1：请补充传感组件量程与具体用途，旧版结论不能作为 V2 的确认。",
    "connector": "合成 V1：请补充连接组件材质与连接方式，新版仍需逐行重新确认。",
}


def version_key(batch, number):
    return f"{batch}/v{number}"


def item_key(batch, number, spec):
    return f"{version_key(batch, number)}/item/{spec.key}"


def batch_manifest(batch, title, versions):
    current_number = versions[-1][0]
    return {
        "scenario": batch,
        "title": title,
        "task_id": str(seed_id(f"{batch}/task")),
        "expected_action": "approve" if batch == "approve" else "return",
        "current_version_id": str(seed_id(version_key(batch, current_number))),
        "versions": [
            {
                "number": number,
                "version_id": str(seed_id(version_key(batch, number))),
                "is_current": number == current_number,
                "items": [
                    {
                        "item_id": str(seed_id(item_key(batch, number, spec))),
                        "row": spec.row,
                        "name": spec.name,
                        "supplier_hs": spec.supplier_hs,
                        "suggested_hs": spec.suggested_hs,
                        "expected_confirmation_hs": spec.expected_hs,
                        "choice": spec.choice,
                    }
                    for spec in specs
                ],
            }
            for number, specs in versions
        ],
    }


class Command(BaseCommand):
    help = "幂等建立独立工作台通过/退回验收批；已有任务及人工业务资料完全保留。"

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="输出稳定 ID、实际任务阶段和初次验收编码表。")

    @transaction.atomic
    def handle(self, *args, **options):
        # Never populate a default developer DB or an online engine from this command.
        target = os.environ.get("CUSTOMS_SQLITE_PATH", "")
        name = str(connection.settings_dict["NAME"])
        if connection.vendor != "sqlite" or settings.ENGINE_MODE != "mock" or settings.EXECUTOR_MODE != "inline":
            raise CommandError("验收种子仅支持 sqlite / mock / inline。")
        database = Path(name).resolve()
        if not connection.is_in_memory_db() and (
            not target or database != (settings.BASE_DIR / target).resolve()
            or database == (settings.LOCAL_DIR / "db.sqlite3").resolve()
        ):
            raise CommandError("请显式选择独立 CUSTOMS_SQLITE_PATH，使用 sqlite / mock / inline。")
        users = self._users()
        supplier, operator, expert = (users[role] for role in (Role.SUPPLIER, Role.OPERATOR, Role.EXPERT))
        batches = []
        for batch, title, versions in BATCHES:
            task, created = Task.objects.get_or_create(
                pk=seed_id(f"{batch}/task"),
                defaults={"title": title, "supplier": supplier, "stage": Task.Stage.AWAITING_CONFIRMATION},
            )
            if task.supplier_id != supplier.pk:
                raise CommandError(f"验收任务 UUID 已被其它供应商使用：{task.pk}；未修改已有资料。")
            if created:
                for user in (operator, expert):
                    TaskAssignment.objects.create(
                        id=seed_id(f"{batch}/assignment/{user.role}"), task=task, user=user, role=user.role,
                    )
                for number, specs in versions:
                    version = self._version(batch, number, task, supplier, specs)
                    if batch == "return" and number == 1:
                        self._historical_return(version, specs, operator, expert)
                # V2 是全新待确认版本，历史退回不改变本次验收的起点。
            manifest = batch_manifest(batch, title, versions)
            current_version = task.versions.order_by("-number").first()
            manifest.update(
                created=created, actual_title=task.title, actual_stage=task.stage, actual_revision=task.revision,
                actual_current_version_id=str(current_version.pk) if current_version else None,
            )
            batches.append(manifest)

        manifest = {
            "result_mode": "mock",
            "disclaimer": DISCLAIMER,
            "accounts": [
                {"username": user.username, "role": role, "user_id": str(user.pk)}
                for role, user in users.items()
            ],
            "batches": batches,
            "acceptance_plan_note": "编码表是首次验收计划；重跑保留当前业务阶段，不把已操作任务恢复为待确认。",
        }
        if options["json"]:
            self.stdout.write(json.dumps(manifest, ensure_ascii=False, indent=2))
            return
        self.stdout.write(DISCLAIMER)
        for account in manifest["accounts"]:
            self.stdout.write(f"账号 {account['username']} ({account['role']})：{account['user_id']}")
        for entry in batches:
            status = "新建" if entry["created"] else "已存在，整批保留"
            self.stdout.write(f"{entry['title']}：{entry['task_id']} [{status}；当前 {entry['actual_stage']}]")
            for version in entry["versions"]:
                self.stdout.write(f"  V{version['number']}：{version['version_id']}")
                for item in version["items"]:
                    self.stdout.write(
                        f"    行{item['row']} {item['name']} {item['item_id']}："
                        f"原码 {item['supplier_hs']} / AI {item['suggested_hs']} / "
                        f"验收选择 {item['choice']} {item['expected_confirmation_hs']}"
                    )
        self.stdout.write(self.style.SUCCESS("seed_workbench 完成；编码表仅描述初次验收计划，重跑不重置业务资料。"))

    def _users(self):
        users = {}
        for username, display_name, role in ACCOUNTS:
            user_id = seed_id(f"account/{username}")
            user = User.objects.filter(pk=user_id).first()
            if user is None:
                if User.objects.filter(username=username).exists():
                    raise CommandError(f"账号 {username} 已由其它 UUID 使用；拒绝接管或重置该账号。")
                user = User(
                    id=user_id, username=username, display_name=display_name,
                    first_name=display_name, role=role,
                )
                password = os.environ.get("WORKBENCH_DEMO_PASSWORD")
                if not password:
                    raise CommandError("新建验收账号需要临时 WORKBENCH_DEMO_PASSWORD；不提供默认口令。")
                user.set_password(password)
                user.save(force_insert=True)
            elif user.username != username or user.role != role:
                raise CommandError(f"验收账号 {username} 的用户名或角色已改变；拒绝覆盖，请检查账号归属。")
            users[role] = user
        return users

    def _version(self, batch, number, task, supplier, specs):
        key = version_key(batch, number)
        version = FileVersion.objects.create(
            id=seed_id(key), task=task, number=number, uploaded_by=supplier,
            original_filename=f"workbench-{batch}-synthetic-V{number}.xlsx",
        )
        now = timezone.now()
        run = Run.objects.create(
            id=seed_id(f"{key}/precheck"), task=task, version=version, kind=Run.Kind.PRECHECK,
            status=Run.Status.SUCCEEDED, is_current=True, started_at=now, finished_at=now,
        )
        for spec in specs:
            row_key = item_key(batch, number, spec)
            item = Item.objects.create(
                id=seed_id(row_key), version=version, source_sheet=SHEET, source_row=spec.row,
                source_cells=f"A{spec.row}:H{spec.row}", name=spec.name,
                attributes={"品名": spec.name, "规格型号": f"SYNTHETIC-{batch}-V{number}-{spec.key}", "样例性质": DISCLAIMER},
                supplier_hs_raw=spec.supplier_hs,
                supplier_elements_raw=f"品名:{spec.name}|规格型号:SYNTHETIC-V{number}-{spec.key}|仅流程验收",
            )
            self._precheck_row(run, item, spec, row_key)
        return version

    def _precheck_row(self, run, item, spec, row_key):
        """显式构造合成引擎输出，复用正式比较规则；不发起任何外部模型或法规查询。"""
        context = ExecutionContext(
            task_id=str(run.task_id), version_id=str(run.version_id), item_id=str(item.pk),
            run_id=str(run.pk), input_revision=item.revision, engine_version=ENGINE_VERSION,
        )
        recommendation = RecommendOutput(
            suggested_hs=spec.suggested_hs,
            candidates=[Candidate(hs=spec.suggested_hs, reason="合成候选，专供确认选择与版本隔离验收")],
            confidence_note="合成样例不提供专业置信度；HS 仅验证数据流转。",
            evidence_status=EvidenceStatus.OBTAINED,
            evidence=[EvidenceEntry(
                evidence_id=str(seed_id(f"{row_key}/synthetic-evidence")), source_type="synthetic",
                title="工作台合成依据（非真实法规）", status=EvidenceStatus.OBTAINED,
                document="WORKBENCH-SYNTHETIC-ONLY", locator="无真实条款；仅合成记录",
                query={"mode": "mock", "external_query_performed": False}, note=DISCLAIMER,
            )],
        )
        comparison = compare_original(CompareInput(
            context=context, recommendation=recommendation, supplier_hs_raw=item.supplier_hs_raw,
        ))
        RunItem.objects.create(
            id=seed_id(f"{row_key}/precheck-result"), run=run, item=item,
            status=RunItem.Status.SUCCEEDED, suggested_hs=recommendation.suggested_hs,
            comparison_status=comparison.comparison_status.value,
            result={
                "candidates": [candidate.model_dump(mode="json") for candidate in recommendation.candidates],
                "comparison_reason": comparison.reason, "confidence_note": recommendation.confidence_note,
                "evidence_status": recommendation.evidence_status.value,
                "evidence": [entry.model_dump(mode="json") for entry in recommendation.evidence],
                "issues": [issue.model_dump(mode="json") for issue in comparison.issues],
                "attempts": 1, "engine_version": ENGINE_VERSION, "result_mode": "mock",
                "synthetic": True, "disclaimer": DISCLAIMER,
            },
        )

    def _historical_return(self, version, specs, operator, expert):
        """历史 V1 快照与退回只关联 V1，V2 不创建任何人工结论。"""
        task = version.task
        key = "return/v1/history"
        confirmed_items = []
        for spec in specs:
            item = Item.objects.get(pk=seed_id(item_key("return", 1, spec)))
            confirmation = Confirmation.objects.create(
                id=seed_id(f"{key}/{spec.key}/confirmation"), item=item, revision=2,
                confirmed_hs=spec.expected_hs, reason="合成 V1 历史确认，不适用于 V2", created_by=operator,
            )
            item.revision = confirmation.revision
            item.save(update_fields=["revision"])
            confirmed_items.append((spec, item, confirmation))
        submission = Submission.objects.create(
            id=seed_id(f"{key}/submission"), task=task, version=version,
            revision=1 + len(specs), status=Submission.Status.RETURNED, created_by=operator,
        )
        for spec, item, confirmation in confirmed_items:
            SubmissionItem.objects.create(
                id=seed_id(f"{key}/{spec.key}/submission-item"), submission=submission, item=item,
                confirmation=confirmation, submitted_hs=confirmation.confirmed_hs,
                item_revision=confirmation.revision,
            )
        decision = Decision.objects.create(
            id=seed_id(f"{key}/decision"), submission=submission, action=Decision.Action.RETURN,
            return_to=Decision.ReturnTo.SUPPLIER, reason=HISTORY_REASON, created_by=expert,
        )
        issue_ids = []
        for spec, item, _ in confirmed_items:
            issue = Issue.objects.create(
                id=seed_id(f"{key}/{spec.key}/issue"), task=task, version=version,
                submission=submission, decision=decision, item=item,
                kind="expert_return", message=HISTORY_ISSUES[spec.key], created_by=expert,
            )
            issue_ids.append(str(issue.pk))
        AuditEvent.objects.create(
            id=seed_id(f"{key}/audit"), task=task, version=version, actor=expert,
            action="decision.returned", stage="expert_decision", reason=HISTORY_REASON,
            details={"decision_id": str(decision.pk), "submission_id": str(submission.pk),
                     "return_to": decision.return_to, "issue_ids": issue_ids, "synthetic": True},
        )
        # 确认 + 提交 + 决定 + V2 重传的历史修订；只写新任务，不在重跑时回退修订。
        task.revision = 1 + len(specs) + 3
        task.save(update_fields=["revision", "updated_at"])
