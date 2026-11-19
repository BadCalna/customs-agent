import uuid
from datetime import timezone as datetime_timezone
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware
from ninja import Router
from apps.accounts.auth import session_auth
from apps.accounts.errors import ApiError
from apps.accounts.permissions import visible_tasks
from apps.api.pagination import decode_cursor, encode_cursor, normalize_limit
from .models import Message
from .schemas import MessageOut, MessagePage, MessageReadIn
from .services import mark_read, message_out

router = Router(tags=["本人消息"], auth=session_auth)


@router.get("/messages", response=MessagePage)
def list_messages(request, cursor: str | None = None, limit: int | None = None, unread_only: bool = False):
    size = normalize_limit(limit)
    rows = Message.objects.filter(recipient=request.auth)
    unread_count = rows.filter(is_read=False).count()
    if unread_only:
        rows = rows.filter(is_read=False)
    position = decode_cursor(cursor)
    if cursor:
        try:
            if set(position) != {"id", "created_at"} or not isinstance(position["created_at"], str):
                raise ValueError
            stamp = parse_datetime(position["created_at"])
            if stamp is None or not is_aware(stamp):
                raise ValueError
            stamp = stamp.astimezone(datetime_timezone.utc)
            identifier = uuid.UUID(position["id"])
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise ApiError(400, "invalid_cursor", "消息分页游标格式不正确")
        rows = rows.filter(Q(created_at__lt=stamp) | Q(created_at=stamp, pk__lt=identifier))
    page = list(rows.order_by("-created_at", "-pk")[:size + 1])
    has_more = len(page) > size
    page = page[:size]
    allowed = set(visible_tasks(request.auth).filter(pk__in=[m.task_id for m in page]).values_list("pk", flat=True))
    next_cursor = encode_cursor({"created_at": page[-1].created_at.isoformat(), "id": str(page[-1].pk)}) if has_more else None
    return dict(items=[message_out(m, can_open=m.task_id in allowed) for m in page],
                next_cursor=next_cursor, unread_count=unread_count)


@router.patch("/messages/{message_id}", response=MessageOut)
def read_message(request, message_id: str, payload: MessageReadIn):
    return mark_read(request.auth, message_id)
