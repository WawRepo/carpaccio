"""The per-resource convenience API: reads, targeted writes, and the lock.

These routes sit ON TOP of the whole-document contract: every write reads the
document, applies one change, and saves against the version it just read - so
a concurrent save produces the same 409 the whole-document PUT gets, and a
lost update is impossible. There is no server-side retry on conflict, on
purpose: re-applying a change to a document someone else just reshaped is
last-writer-wins with extra steps.
"""

from carpaccio.importer import parse
from carpaccio.store import MISSING


def seed(client, unifi_raw) -> str:
    doc = parse(unifi_raw)
    resp = client.put("/api/network", json={"version": MISSING, "document": doc})
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


DEVICE = {
    "mac": "02:00:00:00:00:aa",
    "name": "hand-added node",
    "hostname": "",
    "ip": "192.168.1.223",
    "vlan": "home",
    "assignment": "static",
    "wired": True,
    "active": False,
    "oui": "",
    "last_seen": None,
    "notes": "",
    "source": "manual",
}


def test_collection_reads(client, unifi_raw):
    seed(client, unifi_raw)
    for section, count in (
        ("vlans", 3),
        ("wans", 3),
        ("devices", 87),
        ("infrastructure", 9),
        ("firewall_rules", 0),
    ):
        resp = client.get(f"/api/{section}")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body[section]) == count
        assert body["version"]  # the lock token rides along on every read


def test_device_crud(client, unifi_raw):
    seed(client, unifi_raw)

    created = client.post("/api/devices", json=dict(DEVICE))
    assert created.status_code == 200, created.text
    assert client.get("/api/devices").json()["devices"][-1]["mac"] == DEVICE["mac"]

    # Same MAC again: refused, the MAC is the key.
    dup = client.post("/api/devices", json=dict(DEVICE))
    assert dup.status_code == 409

    patched = client.patch(f"/api/devices/{DEVICE['mac']}", json={"notes": "rack 2"})
    assert patched.status_code == 200
    assert patched.json()["device"]["notes"] == "rack 2"

    missing = client.patch("/api/devices/02:00:00:00:00:ff", json={"notes": "x"})
    assert missing.status_code == 404

    deleted = client.delete(f"/api/devices/{DEVICE['mac']}")
    assert deleted.status_code == 200
    assert client.delete(f"/api/devices/{DEVICE['mac']}").status_code == 404


def test_stale_if_match_is_409(client, unifi_raw):
    """An agent that read the document earlier can pin its write to that read;
    if anything saved in between, the write is refused, exactly like PUT."""
    version = seed(client, unifi_raw)
    # someone else saves first
    other = client.get("/api/network").json()
    other["document"]["devices"][0]["notes"] = "someone else"
    assert (
        client.put(
            "/api/network", json={"version": other["version"], "document": other["document"]}
        ).status_code
        == 200
    )

    resp = client.post("/api/devices", json=dict(DEVICE), headers={"If-Match": version})
    assert resp.status_code == 409
    assert resp.json()["current_version"] != version
    # and the write did NOT land
    assert all(d["mac"] != DEVICE["mac"] for d in client.get("/api/devices").json()["devices"])


def test_invalid_patch_is_422_and_store_untouched(client, unifi_raw):
    seed(client, unifi_raw)
    mac = client.get("/api/devices").json()["devices"][0]["mac"]
    before = client.get("/api/network").json()

    resp = client.patch(f"/api/devices/{mac}", json={"assignment": "wishful-thinking"})
    assert resp.status_code == 422
    after = client.get("/api/network").json()
    assert after["version"] == before["version"]
    assert after["document"] == before["document"]


def test_vlan_crud_and_the_two_guards(client, unifi_raw):
    seed(client, unifi_raw)

    created = client.post(
        "/api/vlans",
        json={
            "name": "cameras",
            "vlan_id": 40,
            "subnet": "",
            "source": "unifi",  # lying about the source is corrected, not honoured
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["vlan"]["source"] == "manual"

    # Guard 1: a unifi VLAN's name is its merge key - rename refused.
    renamed = client.patch("/api/vlans/iot", json={"name": "things"})
    assert renamed.status_code == 409
    assert "merge key" in renamed.json()["detail"]
    # ...but its OTHER fields edit freely.
    edited = client.patch("/api/vlans/iot", json={"notes": "cameras stay in their pen"})
    assert edited.status_code == 200

    # A manual VLAN renames, and devices follow the rename.
    client.post("/api/devices", json=dict(DEVICE, vlan="cameras"))
    renamed2 = client.patch("/api/vlans/cameras", json={"name": "cctv"})
    assert renamed2.status_code == 200
    moved = next(
        d for d in client.get("/api/devices").json()["devices"] if d["mac"] == DEVICE["mac"]
    )
    assert moved["vlan"] == "cctv"

    # Guard 2: deleting a VLAN that still has devices is refused with the count.
    refused = client.delete("/api/vlans/cctv")
    assert refused.status_code == 409
    assert refused.json()["devices_on_vlan"] == 1

    client.delete(f"/api/devices/{DEVICE['mac']}")
    assert client.delete("/api/vlans/cctv").status_code == 200
    assert client.delete("/api/vlans/cctv").status_code == 404


def test_firewall_rule_crud(client, unifi_raw):
    seed(client, unifi_raw)

    created = client.post(
        "/api/firewall_rules",
        json={"name": "guest cannot reach home", "action": "drop", "src": "guest", "dst": "home"},
    )
    assert created.status_code == 200, created.text
    rule_id = created.json()["firewall_rule"]["id"]
    assert rule_id.startswith("manual-")  # minted server-side when absent

    patched = client.patch(f"/api/firewall_rules/{rule_id}", json={"action": "reject"})
    assert patched.status_code == 200
    assert patched.json()["firewall_rule"]["action"] == "reject"

    assert client.delete(f"/api/firewall_rules/{rule_id}").status_code == 200
    assert client.delete(f"/api/firewall_rules/{rule_id}").status_code == 404
