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
