"""Issue, deliver and atomically consume short-lived one-time credentials."""
import logging
import re
from secrets import token_urlsafe
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.accounts.models import User

from ..events import audit
from ..mutations import lock_ops_state
from ..security import digest, fail, request_id, require
from .accounts import ACTIVE, DISABLED, PENDING, _target_guard, account_state, get_user, reserved_account
from .models import AccountLifecycle, SecretTicket
from .sessions import revoke_sessions

logger = logging.getLogger("customs.ops.lifecycle")
TICKET_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
OPEN = {"consumed_at__isnull": True, "revoked_at__isnull": True, "expired_at__isnull": True}
SECRET_PATH = "/ops/admin/secret/"


def _audit_ticket(request, ticket, event, *, outcome="success", summary_code="ok"):
    audit(request, f"ops.secret.{event}", target_type="user", target_id=ticket.user_id,
          outcome=outcome, summary_code=summary_code, payload={"delivery_mode": ticket.delivery_mode})


def _fingerprint(user):
    # Read actual grants, including for inactive accounts. Match the issuance state after any
    # native password, address, role, membership, permission, or status changes.
    groups = tuple(user.groups.order_by("pk").values_list("pk", flat=True))
    direct = tuple(user.user_permissions.order_by("pk").values_list("pk", flat=True))
    included = tuple(user.groups.order_by("pk", "permissions__pk").values_list("pk", "permissions__pk"))
    state = (user.pk, user.username, user.email, user.password, user.role, user.is_staff,
             user.is_superuser, account_state(user), groups, direct, included)
    return digest("secret.account", repr(state))


def _terminal(request, queryset, *, expired=False, now=None):
    now = now or timezone.now()
    field = "expired_at" if expired else "revoked_at"
    name = "expired" if expired else "revoked"
    count = 0
    for ticket in queryset.filter(**OPEN).order_by("pk").iterator(chunk_size=200):
        if queryset.filter(pk=ticket.pk, **OPEN).update(**{field: now}):
            _audit_ticket(request, ticket, name)
            count += 1
    return count


def revoke_tickets(request, user_id, *, purpose=None):
    now = timezone.now()
    query = SecretTicket.objects.filter(user_id=user_id)
    if purpose is not None:
        query = query.filter(purpose=purpose)
    _terminal(request, query.filter(expires_at__lte=now), expired=True, now=now)
    return _terminal(request, query.filter(expires_at__gt=now), now=now)


def _email_target(user):
    if not getattr(settings, "OPS_ADMIN_TRUSTED_EMAIL_ENABLED", False):
        return None
    url = getattr(settings, "OPS_ADMIN_SECRET_PUBLIC_URL", "")
    origin = getattr(settings, "OPS_ADMIN_PUBLIC_ORIGIN", "")
    try:
        parsed = urlsplit(url or origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ({SECRET_PATH} if url else {"", "/"})
                or any(c in parsed.netloc for c in "\\\r\n\t ")
                or not settings.DEFAULT_FROM_EMAIL):
            return None
        # Accessing .port validates syntax; interpolation uses only the configured authority.
        parsed.port
        validate_email(user.email)
    except (TypeError, ValueError, ValidationError):
        return None
    return urlunsplit(("https", parsed.netloc, SECRET_PATH, "", ""))


def _new_ticket(request, user, purpose, mode):
    now = timezone.now()
    ttl = int(getattr(settings, "OPS_ADMIN_SECRET_TTL_SECONDS", 900))
    if not 1 <= ttl <= 900:
        fail(503, "secret_configuration_unavailable", "一次性设密当前不可用")
    raw = token_urlsafe(32)
    ticket = SecretTicket.objects.create(
        user=user, ticket_hash=digest("secret.ticket", raw), account_fingerprint=_fingerprint(user),
        purpose=purpose, issued_by=request.user.pk, delivery_mode=mode, issued_at=now,
        expires_at=now + timezone.timedelta(seconds=ttl),
        idempotency_key_hash=digest("secret.idempotency", request.headers.get("Idempotency-Key", "")),
        request_id=request_id(request),
    )
    _audit_ticket(request, ticket, "issued")
    return ticket, raw


def _set_delivery(reply, ticket, *, raw=None):
    reply.update(delivery_mode=ticket.delivery_mode, expires_at=ticket.expires_at.isoformat())
    if raw is not None:
        reply["ticket"] = raw


def _undeliverable(reply):
    for key in ("ticket", "delivery_mode", "expires_at"):
        reply.pop(key, None)
    reply["delivery_status"] = "unavailable"


def _delivery_user(request, ticket):
    current = SecretTicket.objects.filter(pk=ticket.pk, **OPEN).first()
    if current is None:
        return None
    now = timezone.now()
    queryset = SecretTicket.objects.filter(pk=current.pk)
    if current.expires_at <= now:
        _terminal(request, queryset, expired=True, now=now)
        return None
    user = User.objects.select_for_update().get(pk=current.user_id)
    if not constant_time_compare(current.account_fingerprint, _fingerprint(user)):
        _terminal(request, queryset, now=now)
        return None
    return user


def _deliver_email(request, ticket, raw, address, url, reply):
    """Dispatch only after issuance *and* the send-attempt audit commit."""
    try:
        message_id = f"<{uuid4().hex}@{urlsplit(url).hostname}>"
        with transaction.atomic():
            lock_ops_state()
            if _delivery_user(request, ticket) is None:
                _undeliverable(reply)
                return
            _audit_ticket(request, ticket, "delivery_attempted")
        # Never hold the global DB lock during SMTP. A revoke racing delivery just yields
        # a dead link: future consumption rechecks the fingerprint and terminal state.
        try:
            delivered = EmailMessage(
                subject="AI 智能关务 · 一次性设密", body=f"{url}#ticket={raw}",
                from_email=settings.DEFAULT_FROM_EMAIL, to=[address], headers={"Message-ID": message_id},
            ).send(fail_silently=False)
        except Exception:  # transport exceptions can contain the address or message body
            delivered = 0
        with transaction.atomic():
            lock_ops_state()
            if delivered == 1:
                SecretTicket.objects.filter(pk=ticket.pk).update(email_message_id=message_id)
                _audit_ticket(request, ticket, "sent")
                return
            _audit_ticket(request, ticket, "delivery_failed", outcome="failure", summary_code="send_failed")
            if (user := _delivery_user(request, ticket)) is None:
                _undeliverable(reply)
                return
            _terminal(request, SecretTicket.objects.filter(pk=ticket.pk))
            replacement, offline_raw = _new_ticket(request, user, ticket.purpose, SecretTicket.Delivery.OFFLINE)
            _audit_ticket(request, replacement, "offline_delivered")
        # Return a distinct offline credential only after its replacement and audit commit.
        _set_delivery(reply, replacement, raw=offline_raw)
    except Exception:
        # A provider response is irreversible; the issued credential and send-attempt
        # event already committed before dispatch. Never expose the email secret here.
        _undeliverable(reply)
        logger.error("ops.secret.delivery_processing_failed")


@transaction.atomic
def issue_ticket(request, user_id, *, delivery_mode=None, reply=None):
    require(request.user, "reset_user_password")
    user = get_user(user_id, lock=True)
    _target_guard(request.user, user)
    if user.role and user.pk != request.user.pk:
        # Account takeover confers business capabilities even without explicit Django grants.
        require(request.user, "change_business_role")
    state = account_state(user)
    if state == DISABLED:
        fail(409, "account_disabled", "禁用账号不能签发一次性设密")
    if delivery_mode not in (None, SecretTicket.Delivery.EMAIL, SecretTicket.Delivery.OFFLINE):
        fail(422, "invalid_delivery_mode", "交付方式无效")
    purpose = SecretTicket.Purpose.SET if state == PENDING else SecretTicket.Purpose.RESET
    revoke_tickets(request, user.pk, purpose=purpose)
    email_url = _email_target(user) if delivery_mode != SecretTicket.Delivery.OFFLINE else None
    mode = SecretTicket.Delivery.EMAIL if email_url else SecretTicket.Delivery.OFFLINE
    ticket, raw = _new_ticket(request, user, purpose, mode)
    if reply is None:
        reply = {"status": "accepted"}
    _set_delivery(reply, ticket, raw=None if email_url else raw)
    if email_url:
        transaction.on_commit(lambda: _deliver_email(request, ticket, raw, user.email, email_url, reply))
    else:
        if delivery_mode == SecretTicket.Delivery.EMAIL:
            _audit_ticket(request, ticket, "delivery_failed", outcome="failure", summary_code="email_unavailable")
        _audit_ticket(request, ticket, "offline_delivered")
    return reply


def _invalid_ticket(request, account_id=None):
    audit(request, "ops.secret.consume_failed", target_type="user", target_id=account_id,
          outcome="denied", summary_code="invalid_ticket")
    return 422, {"error": {"code": "invalid_ticket", "message": "一次性设密凭据无效或已失效", "details": {}}}


@transaction.atomic
def consume_ticket(request, *, ticket, new_password):
    if not isinstance(ticket, str) or not TICKET_SHAPE.fullmatch(ticket):
        return _invalid_ticket(request)
    row = SecretTicket.objects.select_for_update().filter(ticket_hash=digest("secret.ticket", ticket)).first()
    if row is None or row.consumed_at or row.revoked_at or row.expired_at:
        return _invalid_ticket(request, row.user_id if row else None)
    now = timezone.now()
    if row.expires_at <= now:
        _terminal(request, SecretTicket.objects.filter(pk=row.pk), expired=True, now=now)
        return _invalid_ticket(request, row.user_id)
    user = User.objects.select_for_update().get(pk=row.user_id)
    state = account_state(user)
    valid_state = PENDING if row.purpose == SecretTicket.Purpose.SET else ACTIVE
    if (state != valid_state or reserved_account(user)
            or not constant_time_compare(row.account_fingerprint, _fingerprint(user))):
        _terminal(request, SecretTicket.objects.filter(pk=row.pk), now=now)
        return _invalid_ticket(request, user.pk)
    try:
        if not isinstance(new_password, str) or not 1 <= len(new_password) <= 1024:
            raise ValidationError("invalid_length")
        validate_password(new_password, user)
    except ValidationError:
        audit(request, "ops.secret.consume_failed", target_type="user", target_id=user.pk,
              outcome="denied", summary_code="invalid_password")
        # Custom validators may interpolate user input; never include their raw messages.
        return 422, {"error": {"code": "invalid_password", "message": "新密码不符合长度或安全要求", "details": {}}}
    if not SecretTicket.objects.filter(pk=row.pk, expires_at__gt=timezone.now(), **OPEN).update(consumed_at=now):
        # A validator can cross the expiry boundary even though the row is locked.
        _terminal(request, SecretTicket.objects.filter(pk=row.pk, expires_at__lte=timezone.now()), expired=True)
        return _invalid_ticket(request, user.pk)
    user.set_password(new_password)
    user.is_active = True
    user.save(update_fields=["password", "is_active"])
    AccountLifecycle.objects.update_or_create(user=user, defaults={"status": ACTIVE})
    revoke_tickets(request, user.pk)
    revoke_sessions([user.pk])
    _audit_ticket(request, row, "consumed")
    return {"status": "accepted"}


@transaction.atomic
def expire_tickets(*, batch_size=250):
    lock_ops_state()
    now = timezone.now()
    ids = list(SecretTicket.objects.filter(expires_at__lte=now, **OPEN).order_by("expires_at", "pk")
               .values_list("pk", flat=True)[:batch_size])
    return _terminal(None, SecretTicket.objects.filter(pk__in=ids), expired=True, now=now)
