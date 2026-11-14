"""P10 只读历史与跨版本候选对照。

历史按"版本 → 提交 → 专家决定 / 执行"分组，所有值来自原始记录，不重算业务结论。
跨版本对照只是**候选展示**（D2）：按合同号+项号、其次品名匹配，绝不推断同一商品身份，
也不把旧版本的通过结论沿用到新版本。
"""
from apps.accounts.permissions import READ, get_visible_task, require_action

from ..models import Confirmation


def _source(item):
    return {"sheet_name": item.source_sheet, "row_number": item.source_row, "cells": item.source_cells}


def _actor(user):
    return (user.display_name or user.username) if user else ""


def cross_version_candidates(task, latest_version) -> list[dict]:
    """为当前版本的商品找旧版本候选：合同号+项号优先，品名兜底。"""
    other_versions = list(task.versions.exclude(pk=latest_version.pk).order_by("-number"))
    by_contract, by_name = {}, {}
    for version in other_versions:
        for item in version.items.prefetch_related("members"):
            for member in item.members.all():
                if member.contract_no and member.item_no:
                    # 旧版本里同键也保留第一条，避免把候选偷偷变成唯一映射。
                    by_contract.setdefault((member.contract_no, member.item_no), item)
            if item.name:
                by_name.setdefault(item.name, item)

    candidates = []
    for item in latest_version.items.prefetch_related("members"):
        keys = sorted({
            (member.contract_no, member.item_no)
            for member in item.members.all() if member.contract_no and member.item_no
        })
        match, basis = None, None
        for key in keys:
            if key in by_contract:
                match, basis = by_contract[key], "contract_no+item_no"
                break
        if match is None and item.name and item.name in by_name:
            match, basis = by_name[item.name], "name"
        if match is not None:
            candidates.append({
                "item_id": str(item.pk),
                "candidate_item_id": str(match.pk),
                "candidate_version_id": str(match.version_id),
                "basis": basis,
                "note": "仅作并排对照的候选，不代表跨版本同一商品身份，旧结论不沿用。",
            })
    return candidates


def task_history(user, task_id: str) -> dict:
    task = get_visible_task(user, task_id)
    require_action(user, READ, task)

    versions = []
    for version in task.versions.order_by("number"):
        items = list(version.items.order_by("source_sheet", "source_row"))
        versions.append({
            "version_id": str(version.pk),
            "number": version.number,
            "original_filename": version.original_filename,
            "uploaded_at": version.uploaded_at.isoformat(),
            "is_current": False,
            "items": [
                {
                    "item_id": str(item.pk), "name": item.name,
                    "supplier_hs_raw": item.supplier_hs_raw, "revision": item.revision,
                    "source_location": _source(item),
                }
                for item in items
            ],
            "confirmations": [
                {
                    "confirmation_id": str(row.pk), "item_id": str(row.item_id),
                    "revision": row.revision, "previous_hs": row.previous_hs,
                    "confirmed_hs": row.confirmed_hs, "reason": row.reason,
                    "created_by": _actor(row.created_by), "created_at": row.created_at.isoformat(),
                }
                for row in Confirmation.objects.filter(item__version=version).order_by("item__source_row", "revision")
            ],
            "runs": [
                {
                    "run_id": str(run.pk), "kind": run.kind, "status": run.status,
                    "is_current": run.is_current,
                    "submission_id": str(run.submission_id) if run.submission_id else None,
                    "created_at": run.created_at.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                }
                for run in version.runs.order_by("created_at")
            ],
            "submissions": [
                {
                    "submission_id": str(submission.pk),
                    "revision": submission.revision, "status": submission.status,
                    "created_by": _actor(submission.created_by),
                    "created_at": submission.created_at.isoformat(),
                    "items": [
                        {
                            "item_id": str(line.item_id), "confirmation_id": str(line.confirmation_id),
                            "submitted_hs": line.submitted_hs, "item_revision": line.item_revision,
                        }
                        for line in submission.items.order_by("item__source_row")
                    ],
                    "decisions": [
                        {
                            "decision_id": str(decision.pk), "action": decision.action,
                            "return_to": decision.return_to, "reason": decision.reason,
                            "created_by": _actor(decision.created_by),
                            "created_at": decision.created_at.isoformat(),
                            "items": [
                                {
                                    "item_id": str(row.item_id), "previous_hs": row.previous_hs,
                                    "final_hs": row.final_hs, "change_reason": row.change_reason,
                                }
                                for row in decision.items.order_by("item__source_row")
                            ],
                        }
                        for decision in submission.decisions.order_by("created_at")
                    ],
                }
                for submission in version.submissions.order_by("created_at")
            ],
        })

    latest = task.versions.order_by("-number").first()
    if latest is not None:
        for entry in versions:
            entry["is_current"] = entry["version_id"] == str(latest.pk)

    return {
        "task_id": str(task.pk),
        "stage": task.stage,
        "revision": task.revision,
        "versions": versions,
        "cross_version_candidates": cross_version_candidates(task, latest) if latest is not None else [],
        "issues": [
            {
                "issue_id": str(issue.pk), "item_id": str(issue.item_id) if issue.item_id else None,
                "kind": issue.kind, "message": issue.message, "status": issue.status,
                "created_at": issue.created_at.isoformat(),
            }
            for issue in task.issues.order_by("created_at")
        ],
        "audit_events": [
            {
                "audit_id": str(event.pk), "action": event.action, "stage": event.stage,
                "version_id": str(event.version_id) if event.version_id else None,
                "item_id": str(event.item_id) if event.item_id else None,
                "actor": _actor(event.actor),
                "previous_value": event.previous_value, "new_value": event.new_value,
                "reason": event.reason, "details": event.details or {},
                "created_at": event.created_at.isoformat(),
            }
            for event in task.audit_events.order_by("-created_at")[:100]
        ],
    }
