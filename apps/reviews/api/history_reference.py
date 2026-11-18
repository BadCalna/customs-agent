"""P15 read-only item-scoped historical reference endpoint.

The existing item READ permission gates access to private reference records.
No approval/recommendation fields or task state are changed by this endpoint.
"""
from ninja import Router
from django.http import Http404

from adapters.sources.history_search import HistorySearchResult
from apps.accounts.auth import session_auth
from apps.accounts.models import Role
from apps.accounts.permissions import READ, require_action
from apps.reviews.api.read import _get_or_404
from apps.reviews.models import FileVersion, Item
from apps.reviews.services.history_reference import search_item_history

router = Router(tags=["历史参考"])


@router.get("/versions/{version_id}/items/{item_id}/history-reference", response=HistorySearchResult, auth=session_auth)
def get_history_reference(request, version_id: str, item_id: str):
    version = _get_or_404(FileVersion.objects.select_related("task"), pk=version_id)
    require_action(request.auth, READ, version)
    if request.auth.role not in {Role.OPERATOR, Role.EXPERT}:
        # 历史库是跨供应商参考数据，供应商不能读取其他供应商的合同/报关来源。
        raise Http404("历史参考仅对内部关务角色开放")
    item = _get_or_404(Item.objects.select_related("version__task"), pk=item_id, version=version)
    return search_item_history(item)
