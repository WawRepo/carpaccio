"""History: the bucket's object versions surfaced, summarised, and restorable.

The app implements no history of its own - S3 versioning IS the history.
These tests drive it through the API the way the History tab does.
"""

from carpaccio.importer import parse
from carpaccio.store import MISSING


def test_history_lists_versions_with_readable_summaries(client, unifi_raw):
    doc = parse(unifi_raw)
    r1 = client.put("/api/network", json={"version": MISSING, "document": doc})
    v1 = r1.json()["version"]

    doc["firewall_rules"].append(
        {"id": "manual-1", "name": "iot to home", "action": "drop", "src": "iot", "dst": "home"}
    )
    v2 = client.put("/api/network", json={"version": v1, "document": doc}).json()["version"]

    doc["devices"][0]["notes"] = "the basement camera"
    client.put("/api/network", json={"version": v2, "document": doc})

    body = client.get("/api/history").json()
    versions = body["versions"]
    assert len(versions) == 3
    assert versions[0]["is_latest"] and not versions[1]["is_latest"]
    for v in versions:
        assert v["version_id"] and v["last_modified"] and v["size"] > 0

    # Newest first, each summarised against its predecessor, in plain words.
    assert any("devices: 1 edited" in c for c in versions[0]["changes"])
    assert any("firewall_rules: 1 added (iot to home)" in c for c in versions[1]["changes"])
    # The very first version diffs against the empty document: a creation.
    assert any(c.startswith("devices: 87 added") for c in versions[2]["changes"])


def test_read_one_old_version(client, unifi_raw):
    doc = parse(unifi_raw)
    v1 = client.put("/api/network", json={"version": MISSING, "document": doc}).json()["version"]
    changed = dict(
        doc,
        firewall_rules=[{"id": "x", "name": "r", "action": "drop", "src": "iot", "dst": "home"}],
    )
    client.put("/api/network", json={"version": v1, "document": changed})

    oldest = client.get("/api/history").json()["versions"][-1]
    resp = client.get("/api/network", params={"version": oldest["version_id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["historical"] is True
    assert body["document"]["firewall_rules"] == []
    # No lock token on a historical read: "version" is absent so the body
    # cannot be round-tripped into PUT by accident.
    assert "version" not in body

    assert client.get("/api/network", params={"version": "no-such-id"}).status_code == 404


def test_restore_goes_through_the_normal_save_path(client, unifi_raw):
    """Restoring writes the old content as a NEW version - never a rollback
    that rewrites or deletes history."""
    doc = parse(unifi_raw)
    v1 = client.put("/api/network", json={"version": MISSING, "document": doc}).json()["version"]
    changed = dict(
        doc,
        firewall_rules=[{"id": "x", "name": "oops", "action": "allow", "src": "any", "dst": "any"}],
    )
    client.put("/api/network", json={"version": v1, "document": changed})

    # What the History tab's Restore button does:
    oldest_id = client.get("/api/history").json()["versions"][-1]["version_id"]
    old_doc = client.get("/api/network", params={"version": oldest_id}).json()["document"]
    current = client.get("/api/network").json()
    resp = client.put("/api/network", json={"version": current["version"], "document": old_doc})
    assert resp.status_code == 200

    # The live document is the old content again...
    assert client.get("/api/network").json()["document"]["firewall_rules"] == []
    # ...and history GREW to 3: the bad version is still there, recoverable.
    versions = client.get("/api/history").json()["versions"]
    assert len(versions) == 3
    assert any("firewall_rules: 1 removed (oops)" in c for c in versions[0]["changes"])
