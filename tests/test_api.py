"""The HTTP surface: the sheet, the API, the 409 and the 422."""

from carpaccio.importer import parse
from carpaccio.model import empty_document
from carpaccio.store import MISSING


def seed(client, unifi_raw) -> str:
    """Put the parsed fixture network into the store; return its version."""
    doc = parse(unifi_raw)
    resp = client.put("/api/network", json={"version": MISSING, "document": doc})
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_serves_the_sheet(client, unifi_raw):
    seed(client, unifi_raw)
    resp = client.get("/")
    assert resp.status_code == 200
    page = resp.text
    # The VLAN tabs are rendered from the embedded document.
    for vlan in ("home", "iot", "guest"):
        assert f'"name": "{vlan}"' in page
    assert "cARPaccio" in page


def test_index_on_first_run_is_not_a_stack_trace(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_get_network_empty(client):
    resp = client.get("/api/network")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == MISSING
    assert body["document"] == empty_document()


def test_schema_endpoint(client):
    resp = client.get("/api/schema")
    assert resp.status_code == 200
    assert resp.json()["properties"]["schema_version"] == {"const": 1}


def test_racing_saves_second_writer_gets_409(client, unifi_raw):
    """The human in the UI and the agent on the API race; the loser is told."""
    version = seed(client, unifi_raw)

    # Both writers GET the same version...
    human = client.get("/api/network").json()
    agent = client.get("/api/network").json()
    assert human["version"] == agent["version"] == version

    human["document"]["firewall_rules"].append(
        {"name": "guest cannot reach home", "action": "drop", "src": "guest", "dst": "home"}
    )
    agent["document"]["devices"][0]["notes"] = "agent wrote this"

    first = client.put(
        "/api/network", json={"version": human["version"], "document": human["document"]}
    )
    assert first.status_code == 200

    second = client.put(
        "/api/network", json={"version": agent["version"], "document": agent["document"]}
    )
    assert second.status_code == 409
    body = second.json()
    # The refusal describes what changed and hands over the fresh version.
    assert body["current_version"] == first.json()["version"]
    assert any("firewall_rules" in c or "devices" in c for c in body["changes"])

    # The agent's write did NOT land: the firewall rule is intact.
    stored = client.get("/api/network").json()["document"]
    assert [r["name"] for r in stored["firewall_rules"]] == ["guest cannot reach home"]
    assert stored["devices"][0]["notes"] == ""


def test_invalid_document_is_422_and_store_untouched(client, unifi_raw):
    version = seed(client, unifi_raw)
    before = client.get("/api/network").json()["document"]

    broken = dict(before)
    broken["devices"] = before["devices"] + [{"mac": "not-a-mac", "assignment": "wishful-thinking"}]
    resp = client.put("/api/network", json={"version": version, "document": broken})
    assert resp.status_code == 422
    assert any("assignment" in e for e in resp.json()["errors"])

    after = client.get("/api/network").json()
    assert after["document"] == before, "a refused write must leave the object untouched"
    assert after["version"] == version


def test_malformed_body_is_400(client):
    resp = client.put("/api/network", json={"document": []})
    assert resp.status_code == 400
