"""Public service interfaces for the ops API and maintenance command."""
from .accounts import (
    account_state,
    create_user,
    delete_user,
    get_user,
    group_summary,
    session_counts,
    set_user_active,
    update_group,
    update_user,
    user_summary,
)
from .rates import prune_rate_buckets, rate_limit
from .tickets import consume_ticket, expire_tickets, issue_ticket

__all__ = [
    "account_state", "consume_ticket", "create_user", "delete_user", "expire_tickets", "get_user",
    "group_summary", "issue_ticket", "maintain_lifecycle", "rate_limit", "session_counts", "set_user_active",
    "update_group", "update_user", "user_summary",
]


def maintain_lifecycle(*, batch_size=250):
    return {"expired_tickets": expire_tickets(batch_size=batch_size), "pruned_rate_buckets": prune_rate_buckets()}
