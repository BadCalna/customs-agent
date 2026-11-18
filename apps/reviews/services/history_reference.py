"""Application boundary for P15 history references; never recommendation input."""
from django.conf import settings

from adapters.sources.history_import import derive_material, derive_spec, parse_element_pairs
from adapters.sources.history_search import HistoryQuery, HistorySearchSource


def query_for_item(item) -> HistoryQuery:
    elements, _issues = parse_element_pairs(item.supplier_elements_raw or "")
    attributes = item.attributes or {}
    def scalar(*names):
        for name in names:
            value = attributes.get(name)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
                return str(value).strip()
        return ""
    return HistoryQuery(
        name=item.name,
        elements=elements,
        material=scalar("material", "材质") or derive_material(elements),
        spec=scalar("specification", "spec", "规格型号", "规格") or derive_spec(elements),
    )


def search_item_history(item):
    return HistorySearchSource().search(query_for_item(item))


def precheck_history_reference(item):
    """Opt-in real history evidence, independent of mock engine's recommendation."""
    if not getattr(settings, "HISTORY_REFERENCE_ENABLED", False):
        return None
    return search_item_history(item)
