"""Shared audited service boundary for API and HTML requests."""
import logging
from uuid import uuid4

from django.utils import timezone
from django.utils.crypto import salted_hmac

from .errors import QUERY_FAILED, ReferenceError, request_id
from .permissions import authenticated

logger = logging.getLogger("customs.ops.events")


def emit_reference_event(request, operation, target_type, result, error, *, detail=False):
    """Emit exactly one sanitized event for a completed reference request."""
    user = getattr(request, "user", None)
    target_id = None
    if error is None and (row := (result or {}).get("record")):
        target_id = "ref_" + salted_hmac(
            "reference_browser.target", f"{result['snapshot_id']}:{row['record_id']}"
        ).hexdigest()[:32]
    if error:
        outcome = "denied" if error.status in {401, 403, 404} else "failure"
        summary = error.code
    else:
        outcome = "success"
        summary = (result or {}).get("outcome", "ok")
        if detail:
            # The service validates this enum before success; no free-form reasons enter audit.
            summary = request.GET.get("reason", "reference_review").strip()
    logger.info("ops.event", extra={"ops_event": {
        "schema_version": 1, "event_id": uuid4().hex, "occurred_at": timezone.now().isoformat(),
        "event_type": f"reference_browser.{operation}",
        "actor_id": str(user.pk) if authenticated(user) else None, "request_id": request_id(request),
        "target_type": target_type, "target_id": target_id, "outcome": outcome, "summary_code": summary,
    }})


def audited_call(request, operation, target_type, action, *args, detail=False):
    """Run one reference service operation and emit one event, including auth failures."""
    result = {}
    try:
        result = action(*args)
    except ReferenceError as exc:
        error = exc
    except Exception:
        # Fail closed without returning or auditing internal exception details.
        error = QUERY_FAILED()
    else:
        error = None
    emit_reference_event(request, operation, target_type, result, error, detail=detail)
    return result, error
