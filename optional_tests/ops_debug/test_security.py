"""Connection secrets are write-only over HTTP and isolated on disk."""
import json
from pathlib import Path
from secrets import token_urlsafe

import pytest

from apps.agent_debug.connections import key_for
from apps.agent_debug.errors import DebugError
from apps.agent_debug.models import ConnectionRevision, EventOutbox
from apps.agent_debug.security import load_secret

from .test_workflow import API, _client, _post


def test_credentials_are_revision_bound_and_never_returned(settings, user):
    client = _client(user)
    assert client.get("/ops/debug/connections/").status_code == 200
    plaintext = token_urlsafe(32)
    body = {"alias": "外部测试", "endpoint_id": "external-test", "model": "debug-model", "auth_mode": "bearer"}
    first = _post(client, "connections", {**body, "api_key": plaintext}, 201)
    second = _post(client, "connections", {**body, "copy_secret_from": first["id"]}, 201)
    records = list(ConnectionRevision.objects.filter(pk__in=[first["id"], second["id"]]))
    assert len(records) == 2
    assert all(key_for(record) == plaintext for record in records)
    refs = {record.secret_ref for record in records}
    assert len(refs) == 2
    # Two file contents are separately encrypted under revision-specific associated data.
    disk = {load_secret(ref) for ref in refs}
    assert len(disk) == 2 and all(plaintext not in ciphertext for ciphertext in disk)
    root = Path(settings.OPS_DEBUG_SECRET_STORE)
    assert root.stat().st_mode & 0o777 == 0o700
    assert all((root / ref).stat().st_mode & 0o777 == 0o600 for ref in refs)
    response = client.get(API + "connections").content.decode()
    audit = json.dumps(list(EventOutbox.objects.values_list("envelope", flat=True)))
    for sensitive in {plaintext, *refs, *disk}:
        assert sensitive not in response and sensitive not in audit and sensitive not in str((first, second))

    root.chmod(0o755)
    with pytest.raises(DebugError, match="SECRET_UNAVAILABLE"):
        load_secret(next(iter(refs)))
    root.chmod(0o700)
    one, other = sorted(refs)
    (root / one).unlink()
    (root / one).symlink_to(root / other)
    with pytest.raises(DebugError, match="SECRET_UNAVAILABLE"):
        load_secret(one)
