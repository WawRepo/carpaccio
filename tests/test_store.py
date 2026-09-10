"""The S3 store: first-run behaviour and the optimistic lock."""

import pytest

from carpaccio.model import empty_document
from carpaccio.store import MISSING, StaleVersion


def test_missing_object_is_an_empty_document(store):
    """First run must not be a stack trace."""
    doc, version = store.load()
    assert doc == empty_document()
    assert version == MISSING


def test_save_and_reload(store):
    doc = empty_document()
    doc["vlans"].append({"name": "home", "vlan_id": None, "subnet": "192.168.1.0/24"})
    v1 = store.save(doc, expected_version=MISSING)
    loaded, v2 = store.load()
    assert loaded == doc
    assert v1 == v2 != MISSING


def test_stale_write_is_refused(store):
    """Two writers race; the loser is refused, never silently merged."""
    base_doc, base_version = store.load()

    writer_a = dict(base_doc, wans=[{"name": "fibre"}])
    writer_b = dict(base_doc, wans=[{"name": "lte"}])

    store.save(writer_a, expected_version=base_version)
    with pytest.raises(StaleVersion) as exc:
        store.save(writer_b, expected_version=base_version)

    # The refusal names the current version and describes what moved.
    assert exc.value.current_version != base_version
    assert any("wans" in change for change in exc.value.changes)
    # And the stored document is writer A's, untouched by the refused write.
    stored, _ = store.load()
    assert stored["wans"] == [{"name": "fibre"}]


def test_refused_writer_can_retry_with_fresh_version(store):
    doc, version = store.load()
    v1 = store.save(dict(doc, wans=[{"name": "fibre"}]), expected_version=version)
    fresh, fresh_version = store.load()
    assert fresh_version == v1
    store.save(dict(fresh, wans=fresh["wans"] + [{"name": "lte"}]), expected_version=v1)
    final, _ = store.load()
    assert [w["name"] for w in final["wans"]] == ["fibre", "lte"]
