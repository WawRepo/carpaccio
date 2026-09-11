"""The importer against the sanitised fixtures: parse, counts, merge semantics."""

import copy

from carpaccio.importer import merge, parse
from carpaccio.model import empty_document, mac_is_randomised, validation_errors


def test_parse_validates(unifi_raw):
    doc = parse(unifi_raw)
    assert validation_errors(doc) == []


def test_parse_counts(unifi_raw):
    """The counts from the live network, preserved through sanitising."""
    doc = parse(unifi_raw)
    assert len(doc["vlans"]) == 3
    assert len(doc["wans"]) == 3
    assert len(doc["devices"]) == 87
    reservations = [d for d in doc["devices"] if d["assignment"] == "reservation"]
    assert len(reservations) == 17
    # Randomised is only meaningful for devices currently on the network: the
    # 8 phones/tablets/laptops using a private Wi-Fi address right now.
    active_randomised = [d for d in doc["devices"] if d["active"] and mac_is_randomised(d["mac"])]
    assert len(active_randomised) == 8
    assert sum(1 for d in doc["devices"] if d["active"]) == 41


def test_parse_vlans(unifi_raw):
    doc = parse(unifi_raw)
    by_name = {v["name"]: v for v in doc["vlans"]}
    assert set(by_name) == {"home", "iot", "guest"}
    assert by_name["home"]["vlan_id"] is None  # the untagged LAN
    assert by_name["iot"]["vlan_id"] == 2
    assert by_name["guest"]["vlan_id"] == 3
    assert by_name["home"]["dhcp"]["start"].endswith(".6")
    assert by_name["home"]["dhcp"]["stop"].endswith(".190")
    # Firewall rules genuinely empty on the live controller today.
    assert doc["firewall_rules"] == []


def test_every_device_maps_to_a_vlan(unifi_raw):
    doc = parse(unifi_raw)
    vlan_names = {v["name"] for v in doc["vlans"]}
    assert all(d["vlan"] in vlan_names for d in doc["devices"])


def test_mac_is_randomised():
    assert mac_is_randomised("02:00:5e:00:53:1f")  # b6 = ...0110, bit 1 set
    assert mac_is_randomised("0a:11:22:33:44:55")
    assert not mac_is_randomised("00:00:5e:00:53:2b")  # burned-in Pi address
    assert not mac_is_randomised("00:11:22:33:44:55")


def test_merge_preserves_handwritten_data(unifi_raw):
    """Re-importing must never eat descriptions or hand-added static entries."""
    imported = parse(unifi_raw)
    existing = copy.deepcopy(imported)

    # The human annotated a controller-known device...
    annotated = existing["devices"][0]
    annotated["notes"] = "the basement camera, do not move"
    # ...corrected a device the controller misclassifies: the host itself
    # holds this address by configuration, invisible to any API...
    static = existing["devices"][1]
    static["assignment"] = "static"
    static["ip"] = "192.168.1.212"
    # ...and added a device UniFi has never seen at all.
    existing["devices"].append(
        {
            "mac": "02:00:00:00:00:01",
            "name": "hand-added k3s node",
            "hostname": "",
            "ip": "192.168.1.224",
            "vlan": "home",
            "assignment": "static",
            "wired": True,
            "active": False,
            "oui": "",
            "last_seen": None,
            "notes": "",
            "source": "manual",
        }
    )
    existing["vlans"][0]["notes"] = "the trusted LAN"
    existing["firewall_rules"].append(
        {
            "id": "manual-1",
            "name": "iot cannot reach home",
            "action": "drop",
            "src": "iot",
            "dst": "home",
            "protocol": "any",
            "ports": "",
            "enabled": True,
            "description": "",
        }
    )

    merged = merge(existing, imported)
    assert validation_errors(merged) == []

    by_mac = {d["mac"]: d for d in merged["devices"]}
    assert by_mac[annotated["mac"]]["notes"] == "the basement camera, do not move"
    assert by_mac[static["mac"]]["assignment"] == "static"
    assert by_mac[static["mac"]]["ip"] == "192.168.1.212"
    assert "02:00:00:00:00:01" in by_mac, "hand-added device was eaten by the import"
    assert len(merged["devices"]) == 88
    assert merged["vlans"][0]["notes"] == "the trusted LAN"
    assert [r["name"] for r in merged["firewall_rules"]] == ["iot cannot reach home"]


def test_merge_lets_the_controller_update_observations(unifi_raw):
    imported = parse(unifi_raw)
    existing = copy.deepcopy(imported)
    moved = existing["devices"][2]
    moved["ip"] = "192.168.1.99"  # stale observation from last month
    moved["active"] = not moved["active"]
    merged = merge(existing, imported)
    by_mac = {d["mac"]: d for d in merged["devices"]}
    fresh = next(d for d in imported["devices"] if d["mac"] == moved["mac"])
    assert by_mac[moved["mac"]]["ip"] == fresh["ip"]
    assert by_mac[moved["mac"]]["active"] == fresh["active"]


def test_merge_into_empty_document(unifi_raw):
    merged = merge(empty_document(), parse(unifi_raw))
    assert validation_errors(merged) == []
    assert len(merged["devices"]) == 87


def test_network_isolation_is_imported(unifi_raw):
    """Isolation is separation that is NOT a firewall rule, and missing it is the
    worst thing this tool can get wrong.

    Every rule list on the real network is empty — rest/firewallrule,
    rest/firewallgroup, v2 firewall-policies, firewall/zone, trafficrules — and
    yet iot and guest genuinely cannot reach home, because UniFi's per-network
    "Network Isolation" switch is on. A rules-only audit reports that as "nothing
    separates these VLANs", which invites someone to go and build a wall that is
    already standing. Assert the flag survives the import so it cannot regress.
    """
    doc = parse(unifi_raw)
    by_name = {v["name"]: v for v in doc["vlans"]}
    assert by_name["iot"]["isolated"] is True
    assert by_name["guest"]["isolated"] is True
    # home is the network the other two are isolated FROM; it is not isolated
    # itself, and a missing flag must read as False rather than None.
    assert by_name["home"]["isolated"] is False


def test_infrastructure_rename_survives_reimport(unifi_raw):
    """Renaming a switch in the UI must not be undone by the next import.

    Network gear is keyed on MAC, so a rename is safe -- but only if merge()
    can tell "the human typed this" from "the controller said this". It does so
    by comparing the stored name against the stored unifi_name. Without that,
    making the field editable would be a lie: the edit would survive until the
    next import and then vanish, which is worse than a read-only field.
    """
    first = parse(unifi_raw)
    stored = copy.deepcopy(first)
    target = stored["infrastructure"][0]
    controller_name = target["name"]
    target["name"] = "switch-loft-the-one-behind-the-boiler"

    merged = merge(stored, parse(unifi_raw))
    row = next(i for i in merged["infrastructure"] if i["mac"] == target["mac"])

    assert row["name"] == "switch-loft-the-one-behind-the-boiler"
    # and the controller's own name is still recorded, so a later UniFi rename
    # is still visible rather than lost
    assert row["unifi_name"] == controller_name
    assert validation_errors(merged) == []


def test_untouched_infrastructure_name_tracks_the_controller(unifi_raw):
    """The other half: if nobody renamed it, the controller still wins."""
    stored = parse(unifi_raw)
    incoming = parse(unifi_raw)
    incoming["infrastructure"][0]["name"] = "renamed-in-unifi"

    merged = merge(stored, incoming)
    row = next(
        i for i in merged["infrastructure"] if i["mac"] == incoming["infrastructure"][0]["mac"]
    )
    assert row["name"] == "renamed-in-unifi"


def test_hand_added_wan_survives_reimport(unifi_raw):
    """A manual uplink must not be matched against the controller, or erased by it.

    The controller reports three WANs. A fourth typed in here -- a 5G dongle not
    plugged in yet, a line on order -- has no counterpart to match, and the merge
    keys on `name`, so a freshly added row whose name is still EMPTY would
    collide with any other empty-named row if it went through the same path.
    """
    stored = parse(unifi_raw)
    stored["wans"].append(
        {
            "name": "new uplink",
            "wan_type": "",
            "failover_priority": None,
            "notes": "on order",
            "source": "manual",
        }
    )
    stored["wans"].append(
        {
            "name": "5g-backup",
            "wan_type": "dhcp",
            "failover_priority": 4,
            "notes": "",
            "source": "manual",
        }
    )

    merged = merge(stored, parse(unifi_raw))

    assert len(merged["wans"]) == 5, "3 from the controller + 2 hand-added"
    manual = [w for w in merged["wans"] if w.get("source") == "manual"]
    assert len(manual) == 2, "a second hand-added row must not be swallowed by the first"
    assert {w["notes"] for w in manual} == {"on order", ""}
    assert validation_errors(merged) == []


def test_controller_wans_are_still_deduplicated(unifi_raw):
    """The other half: importing twice must not double the controller's uplinks."""
    stored = parse(unifi_raw)
    merged = merge(stored, parse(unifi_raw))
    from_controller = [w for w in merged["wans"] if w.get("source") != "manual"]
    assert len(from_controller) == 3


def test_parse_marks_vlans_as_unifi(unifi_raw):
    doc = parse(unifi_raw)
    assert all(v["source"] == "unifi" for v in doc["vlans"])


def test_hand_added_vlan_survives_reimport(unifi_raw):
    """A manual VLAN has no controller counterpart and must never be matched
    against one - including after a rename, which is the whole point of
    keeping it out of the name-keyed merge."""
    imported = parse(unifi_raw)
    existing = copy.deepcopy(imported)
    existing["vlans"].append(
        {
            "name": "cameras",
            "vlan_id": 40,
            "subnet": "",
            "isolated": True,
            "dhcp": {"enabled": False, "start": "", "stop": "", "lease_seconds": None, "dns": []},
            "notes": "planned segment, not built yet",
            "source": "manual",
        }
    )

    merged = merge(existing, imported)
    assert validation_errors(merged) == []
    names = [v["name"] for v in merged["vlans"]]
    assert "cameras" in names
    assert len(names) == 4  # 3 from the controller + 1 manual, nothing duplicated

    # Rename the manual VLAN and re-import again: still one row, new name.
    renamed = copy.deepcopy(merged)
    renamed["vlans"][-1]["name"] = "cctv"
    merged2 = merge(renamed, parse(unifi_raw))
    names2 = [v["name"] for v in merged2["vlans"]]
    assert "cctv" in names2 and "cameras" not in names2
    assert len(names2) == 4


def test_vlan_without_source_is_treated_as_unifi(unifi_raw):
    """A document stored before vlans[].source existed: its rows can only have
    come from the controller, so they must merge by name, not duplicate."""
    imported = parse(unifi_raw)
    existing = copy.deepcopy(imported)
    for v in existing["vlans"]:
        v.pop("source", None)
        v["notes"] = "kept"
    merged = merge(existing, imported)
    assert len(merged["vlans"]) == 3
    assert all(v["notes"] == "kept" for v in merged["vlans"])
