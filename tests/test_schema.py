"""The schema itself: what it admits and what it refuses."""

from carpaccio.model import empty_document, validation_errors


def test_empty_document_is_valid():
    assert validation_errors(empty_document()) == []


def test_assignment_enum_is_closed():
    doc = empty_document()
    doc["devices"].append({"mac": "aa:bb:cc:dd:ee:ff", "assignment": "hardcoded"})
    errors = validation_errors(doc)
    assert errors and "assignment" in errors[0]


def test_uppercase_mac_is_refused():
    # One canonical form (lowercase) or the MAC-keyed merge silently forks.
    doc = empty_document()
    doc["devices"].append({"mac": "AA:BB:CC:DD:EE:FF", "assignment": "dhcp"})
    assert validation_errors(doc)


def test_firewall_action_enum():
    doc = empty_document()
    doc["firewall_rules"].append({"name": "x", "action": "permit", "src": "home", "dst": "iot"})
    assert validation_errors(doc)


def test_unknown_top_level_key_is_refused():
    doc = empty_document()
    doc["surprise"] = []
    assert validation_errors(doc)


def test_valid_full_entries():
    doc = empty_document()
    doc["vlans"].append(
        {
            "name": "iot",
            "vlan_id": 2,
            "subnet": "192.168.2.0/24",
            "dhcp": {
                "enabled": True,
                "start": "192.168.2.6",
                "stop": "192.168.2.254",
                "lease_seconds": 86400,
                "dns": [],
            },
            "notes": "",
        }
    )
    doc["firewall_rules"].append(
        {
            "id": "manual-1",
            "name": "iot cannot reach home",
            "action": "drop",
            "src": "iot",
            "dst": "home",
            "protocol": "any",
            "ports": "",
            "enabled": True,
            "description": "cameras stay in their pen",
        }
    )
    assert validation_errors(doc) == []


def test_schema_endpoint_serves_the_full_schema(client):
    """An agent can fetch this and validate before PUTting - the response is
    the byte-identical schema the server itself validates with."""
    from carpaccio.model import load_schema

    assert client.get("/api/schema").json() == load_schema()


def test_every_field_the_importer_writes_is_described(unifi_raw):
    """Walk everything parse() emits and demand a schema property for it.

    additionalProperties is false throughout, so an undescribed field would be
    REFUSED at save time - this test moves that discovery from the first
    import in production to the test run."""
    from carpaccio.importer import parse
    from carpaccio.model import load_schema

    schema = load_schema()
    doc = parse(unifi_raw)
    for section, entries in doc.items():
        if section == "schema_version":
            continue
        props = schema["properties"][section]["items"]["properties"]
        for entry in entries:
            for field in entry:
                assert field in props, f"{section}[].{field} is not in the schema"
            # one level deeper: the dhcp sub-object
            if "dhcp" in entry:
                dhcp_props = props["dhcp"]["properties"]
                for field in entry["dhcp"]:
                    assert field in dhcp_props, f"{section}[].dhcp.{field} is not in the schema"


def test_every_row_the_ui_creates_is_valid():
    """Mirrors of the exact objects app.js pushes on each '+ Add' button.

    If a button creates a row the schema refuses, the page can build a
    document that cannot be saved - a bad button. If one of these templates
    drifts from app.js, this test lies; keep them in sync when editing the
    frontend's add-buttons."""
    doc = empty_document()
    # + Add VLAN (addVlan in app.js): subnet deliberately empty
    doc["vlans"].append(
        {
            "name": "new-vlan",
            "vlan_id": None,
            "subnet": "",
            "isolated": False,
            "dhcp": {"enabled": False, "start": "", "stop": "", "lease_seconds": None, "dns": []},
            "notes": "",
            "source": "manual",
        }
    )
    # + Add device (renderVlan): mac empty until typed - normalise() lowercases
    # but an empty mac must still be refused, so use a typed-in one here
    doc["devices"].append(
        {
            "mac": "02:00:00:00:00:01",
            "name": "",
            "hostname": "",
            "ip": "",
            "vlan": "new-vlan",
            "assignment": "static",
            "wired": True,
            "active": False,
            "oui": "",
            "last_seen": None,
            "notes": "",
            "source": "manual",
        }
    )
    # + Add rule (addRule)
    doc["firewall_rules"].append(
        {
            "id": "manual-1757500000000",
            "name": "new-vlan to any",
            "action": "drop",
            "src": "new-vlan",
            "dst": "any",
            "protocol": "any",
            "ports": "",
            "enabled": True,
            "description": "",
        }
    )
    # + Add WAN (renderWans)
    doc["wans"].append(
        {
            "name": "new uplink",
            "wan_type": "",
            "failover_priority": None,
            "notes": "",
            "source": "manual",
        }
    )
    # + Add gear (renderWans)
    doc["infrastructure"].append(
        {
            "name": "patch panel",
            "unifi_name": "",
            "model": "",
            "kind": "",
            "ip": "",
            "mac": "02:00:00:00:00:02",
            "notes": "",
        }
    )
    assert validation_errors(doc) == []
