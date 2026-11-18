"""Immutable, non-secret engine identity captured when a Run is created.

Constructing an adapter reads local metadata only; it must never call a model.
Credentials are deliberately excluded: rotating a key does not change results.
"""
import hashlib
import json

from django.conf import settings

from adapters.engines.protocols import get_engine


VERSION_FIELDS = ("engine_version", "prompt_version", "rules_version")


def current_engine_snapshot(engine=None):
    engine = engine or get_engine()
    mode = settings.ENGINE_MODE
    config = {"engine_mode": mode}
    if mode == "openai":
        config.update(api_base=settings.CUSTOMS_AI_API_BASE, model=settings.CUSTOMS_AI_MODEL)
    return {
        "engine_mode": mode,
        "result_mode": "mock" if mode == "mock" else "live",
        **{key: getattr(engine, key, "") for key in VERSION_FIELDS},
        "config_hash": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
    }


def snapshot_complete(snapshot):
    return (isinstance(snapshot, dict)
            and snapshot.get("engine_mode") in ("mock", "openai")
            and snapshot.get("result_mode") == ("mock" if snapshot["engine_mode"] == "mock" else "live")
            and all(snapshot.get(key) for key in VERSION_FIELDS))


def engine_matches(run, engine=None):
    return snapshot_complete(run.engine_snapshot) and run.engine_snapshot == current_engine_snapshot(engine)


def result_metadata(run):
    return {key: run.engine_snapshot.get(key, "") for key in ("result_mode", *VERSION_FIELDS)}


def result_matches(run, result):
    return (snapshot_complete(run.engine_snapshot) and isinstance(result, dict)
            and all(result.get(key) == value for key, value in result_metadata(run).items()))


def rows_match(run):
    return all(result_matches(run, row.result) for row in run.items.all())


def run_result_mode(run):
    """Only complete, consistent persisted engine identity permits a live badge.

    Historical/empty/mixed/unknown rows fail closed to mock, never to today's
    deployment mode. Queued rows have persisted identity too, but no empty Run
    can claim that live work has happened.
    """
    from apps.api.schemas import ResultMode

    if run is None or not snapshot_complete(run.engine_snapshot):
        return ResultMode.MOCK
    rows = list(run.items.all())
    if (run.engine_snapshot["result_mode"] == "live" and rows
            and all(result_matches(run, row.result) for row in rows)):
        return ResultMode.LIVE
    return ResultMode.MOCK
