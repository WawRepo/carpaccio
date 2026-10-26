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
    # 02:00:5e:... is the RFC 7042 documentation range with the
    # locally-administered bit set, which is exactly what this asserts.
    # This was ALSO a real device's address until HOME-341 — the first audit
    # caught the burned-in MAC on the line below and missed this one, because
    # a randomised MAC does not look like hardware. It still named a device
    # that was on this network the day it was written.
    assert mac_is_randomised("02:00:5e:00:53:1f")  # 02 = ...0010, bit 1 set
    assert mac_is_randomised("0a:11:22:33:44:55")
    # 00:00:5e:00:53:xx is reserved for documentation by RFC 7042 §2.1.2, the
    # MAC equivalent of 192.0.2.0/24. It was a real Raspberry Pi's address
    # until HOME-341: a MAC is not a secret — not a credential, not routable —
    # but it fingerprints hardware in someone's house, and the test only ever
    # needed a first octet with the locally-administered bit clear. There is
    # no reason to spend a real device to get one.
    assert not mac_is_randomised("00:00:5e:00:53:2b")
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


# ---- zone-based firewall policies (HOME-467) --------------------------------
#
# UniFi replaced per-network isolation with zone policies and turned the old
# toggle OFF while doing it. The importer read `source.network_id`, which zone
# policies do not have, so every rule would have imported as "any -> any": 85
# real rules flattened into 85 meaningless ones, and a matrix that showed a
# segmented network as wide open. These tests exist so that cannot come back
# quietly.

ZONES = [
    {"_id": "z-int", "name": "Internal", "network_ids": ["n-home", "n-lab"]},
    {"_id": "z-iot", "name": "Iot", "network_ids": ["n-iot"]},
    {"_id": "z-ext", "name": "External", "network_ids": []},
]

NETWORKS = [
    {"_id": "n-home", "name": "home", "purpose": "corporate", "ip_subnet": "192.0.2.1/24"},
    {"_id": "n-lab", "name": "lab", "purpose": "corporate", "ip_subnet": "198.51.100.1/24"},
    {"_id": "n-iot", "name": "iot", "purpose": "corporate", "ip_subnet": "203.0.113.1/24"},
]


def _raw(policies):
    return {
        "networkconf": NETWORKS,
        "user": [],
        "sta": [],
        "device": [],
        "firewall_policies": policies,
        "firewall_zones": ZONES,
    }


def test_a_zone_policy_is_named_not_flattened_to_any():
    """The bug itself: zone_id, not network_id, and never a silent "any"."""
    doc = parse(
        _raw(
            [
                {
                    "_id": "p1",
                    "name": "Block All Traffic",
                    "action": "BLOCK",
                    "enabled": True,
                    "source": {"zone_id": "z-int", "matching_target": "ANY"},
                    "destination": {"zone_id": "z-iot", "matching_target": "ANY"},
                }
            ]
        )
    )
    r = doc["firewall_rules"][0]
    assert r["action"] == "drop"
    # A zone is named for the VLANS IT CONTAINS, not for itself. The matrix
    # asks VLAN against VLAN, so a rule whose source reads "Internal" matches
    # nothing and the pair renders "no rule" — a blocked path shown as open.
    assert r["dst"] == "iot"
    assert r["src"] == "home, lab"
    assert "any" not in (r["src"], r["dst"])


def test_an_ip_scoped_policy_keeps_its_qualifier():
    """The dangerous case. "192.168.1.213-254 may reach iot" must NOT be
    rendered as "Internal may reach iot" — somebody would read that as the
    whole VLAN and be wrong about their own firewall."""
    doc = parse(
        _raw(
            [
                {
                    "_id": "p2",
                    "name": "cluster",
                    "action": "ALLOW",
                    "enabled": True,
                    "source": {
                        "zone_id": "z-int",
                        "matching_target": "IP",
                        "ips": ["192.0.2.213-192.0.2.254"],
                    },
                    "destination": {"zone_id": "z-iot", "matching_target": "ANY"},
                }
            ]
        )
    )
    src = doc["firewall_rules"][0]["src"]
    assert src.startswith("home, lab")
    assert "192.0.2.213-192.0.2.254" in src


def test_an_unknown_zone_is_never_called_any():
    """Falling back to "any" would turn one specific rule into a universal
    one, which is the single most dangerous thing this parser can do."""
    doc = parse(
        _raw(
            [
                {
                    "_id": "p3",
                    "name": "mystery",
                    "action": "BLOCK",
                    "enabled": True,
                    "source": {"zone_id": "z-does-not-exist", "matching_target": "ANY"},
                    "destination": {"zone_id": "z-iot", "matching_target": "ANY"},
                }
            ]
        )
    )
    assert doc["firewall_rules"][0]["src"] == "z-does-not-exist"


def test_no_policies_still_means_no_rules():
    """The old behaviour has to survive: a network with genuinely no policy
    must not gain an imaginary one."""
    assert parse(_raw([]))["firewall_rules"] == []


def test_a_zone_with_no_vlans_keeps_its_own_name():
    """External holds three WANs and no VLAN; Gateway holds nothing at all.
    There is no VLAN to name, and inventing one would be worse than showing
    the truth — the matrix simply will not match these, which is correct."""
    doc = parse(
        _raw(
            [
                {
                    "_id": "p4",
                    "name": "from the internet",
                    "action": "BLOCK",
                    "enabled": True,
                    "source": {"zone_id": "z-ext", "matching_target": "ANY"},
                    "destination": {"zone_id": "z-iot", "matching_target": "ANY"},
                }
            ]
        )
    )
    assert doc["firewall_rules"][0]["src"] == "External"


# ---- re-importing rules (HOME-950) ------------------------------------------
#
# The merge used to keep every stored rule and append only (id, name) pairs it
# had not seen. With 145 zone policies on the controller the store drifted to
# 151: changed rules kept their old rows, a renamed one sat under both names,
# deleted ones stayed. And `ports` was always "", which the schema reads as
# every port.


def _policy(pid, name, src=None, dst=None, action="ALLOW"):
    return {
        "_id": pid,
        "name": name,
        "action": action,
        "enabled": True,
        "protocol": "tcp",
        "source": src or {"zone_id": "z-int", "matching_target": "ANY"},
        "destination": dst or {"zone_id": "z-iot", "matching_target": "ANY"},
    }


def _hand_written(rule_id, name, **extra):
    rule = {
        "id": rule_id,
        "name": name,
        "action": "drop",
        "src": "iot",
        "dst": "home",
        "protocol": "any",
        "ports": "",
        "enabled": True,
        "description": "",
    }
    rule.update(extra)
    return rule


def test_parse_marks_rules_as_unifi():
    doc = parse(_raw([_policy("p1", "home_to_iot")]))
    assert doc["firewall_rules"][0]["source"] == "unifi"
    assert validation_errors(doc) == []


def test_a_changed_rule_is_replaced():
    """home_to_talos went from Any to network home, ports 6443,50000, and kept
    its id and name. The old row must not survive beside or instead of it."""
    stored = parse(_raw([_policy("p1", "home_to_talos")]))
    changed = _policy(
        "p1",
        "home_to_talos",
        src={"zone_id": "z-int", "matching_target": "NETWORK", "network_ids": ["n-home"]},
        dst={
            "zone_id": "z-iot",
            "matching_target": "ANY",
            "port_matching_type": "SPECIFIC",
            "port": "6443,50000",
        },
    )
    merged = merge(stored, parse(_raw([changed])))
    assert validation_errors(merged) == []
    assert len(merged["firewall_rules"]) == 1
    rule = merged["firewall_rules"][0]
    assert rule["ports"] == "6443,50000"
    assert rule["src"] == "home, lab (network home)"


def test_a_renamed_rule_is_not_duplicated():
    """UniFi renamed `cluser` to `home_to_iot`: one row, under the new name."""
    stored = parse(_raw([_policy("p1", "cluser")]))
    merged = merge(stored, parse(_raw([_policy("p1", "home_to_iot")])))
    assert [r["name"] for r in merged["firewall_rules"]] == ["home_to_iot"]


def test_a_deleted_rule_is_dropped():
    """Three built-in "Isolated Networks" rules left the controller and stayed
    in the store. A rule the controller no longer has is gone."""
    stored = parse(_raw([_policy("p1", "keep"), _policy("p2", "Isolated Networks")]))
    merged = merge(stored, parse(_raw([_policy("p1", "keep")])))
    assert [r["id"] for r in merged["firewall_rules"]] == ["p1"]


def test_a_hand_written_rule_survives_reimport():
    """Both kinds: one marked manual, and one stored before `source` existed
    whose id the controller has never reported. Neither is the controller's
    to delete. They come after the controller's rules, which are what the
    gateway enforces and so what the matrix's first match should read."""
    stored = parse(_raw([_policy("p1", "home_to_iot")]))
    stored["firewall_rules"].insert(0, _hand_written("manual-1", "planned", source="manual"))
    stored["firewall_rules"].append(_hand_written("manual-2", "old note"))

    merged = merge(stored, parse(_raw([_policy("p1", "home_to_iot")])))
    assert validation_errors(merged) == []
    assert [r["id"] for r in merged["firewall_rules"]] == ["p1", "manual-1", "manual-2"]

    # and a second import changes nothing
    again = merge(merged, parse(_raw([_policy("p1", "home_to_iot")])))
    assert [r["id"] for r in again["firewall_rules"]] == ["p1", "manual-1", "manual-2"]


def test_a_rule_without_source_is_matched_by_id():
    """A store written before the field: its imported rows have no `source`.
    The controller still knows their ids, so they are its rows to replace,
    and a deleted one with `source: unifi` is no longer kept."""
    stored = parse(_raw([_policy("p1", "home_to_iot")]))
    for r in stored["firewall_rules"]:
        r.pop("source")
        r["ports"] = "stale"
    stored["firewall_rules"].append(_hand_written("p9", "gone", source="unifi"))

    merged = merge(stored, parse(_raw([_policy("p1", "home_to_iot")])))
    assert len(merged["firewall_rules"]) == 1
    assert merged["firewall_rules"][0]["ports"] == ""
    assert merged["firewall_rules"][0]["source"] == "unifi"


def test_ports_are_recorded():
    """Empty means every port, so it is only ever written for ANY."""
    doc = parse(
        _raw(
            [
                _policy(
                    "p1",
                    "iot_to_gateway_basics",
                    dst={
                        "zone_id": "z-ext",
                        "matching_target": "ANY",
                        "port_matching_type": "SPECIFIC",
                        "port": "53,67,123",
                    },
                ),
                _policy("p2", "Block All Traffic", action="BLOCK"),
                # The "(Return)" half of an allow rule matches the SOURCE port.
                _policy(
                    "p3",
                    "iot_to_pihole_dns (Return)",
                    src={
                        "zone_id": "z-int",
                        "matching_target": "IP",
                        "ips": ["192.0.2.218"],
                        "port_matching_type": "SPECIFIC",
                        "port": "53",
                    },
                ),
                # A port group, or anything else that is not ANY: never empty.
                _policy(
                    "p4",
                    "grouped",
                    dst={
                        "zone_id": "z-iot",
                        "matching_target": "ANY",
                        "port_matching_type": "OBJECT",
                        "port_group_id": "g-web",
                    },
                ),
                _policy(
                    "p5",
                    "all but ssh",
                    dst={
                        "zone_id": "z-iot",
                        "matching_target": "ANY",
                        "port_matching_type": "SPECIFIC",
                        "port": "22",
                        "match_opposite_ports": True,
                    },
                ),
            ]
        )
    )
    by_name = {r["name"]: r for r in doc["firewall_rules"]}
    assert by_name["iot_to_gateway_basics"]["ports"] == "53,67,123"
    assert by_name["Block All Traffic"]["ports"] == ""
    assert by_name["iot_to_pihole_dns (Return)"]["ports"] == ""
    assert by_name["iot_to_pihole_dns (Return)"]["description"] == "source port 53"
    assert by_name["grouped"]["ports"] == "g-web"
    assert by_name["all but ssh"]["ports"] == "not 22"
    assert validation_errors(doc) == []


def test_a_network_target_is_named():
    """`home (693d6285743b520e6e124f19)` was true and told nobody anything."""
    doc = parse(
        _raw(
            [
                _policy(
                    "p1",
                    "home_to_talos",
                    src={
                        "zone_id": "z-int",
                        "matching_target": "NETWORK",
                        "network_ids": ["n-home"],
                    },
                ),
                _policy(
                    "p2",
                    "unknown network",
                    src={
                        "zone_id": "z-int",
                        "matching_target": "NETWORK",
                        "network_ids": ["n-gone"],
                    },
                ),
            ]
        )
    )
    assert doc["firewall_rules"][0]["src"] == "home, lab (network home)"
    # An id networkconf does not know is shown as the id, never dropped.
    assert doc["firewall_rules"][1]["src"] == "home, lab (network n-gone)"
