"""carpaccio-import: read the UniFi controller, merge into the stored document.

READ-ONLY against UniFi. The only POST is the login itself; every data call is
a GET. This tool must never create, change or delete anything on the
controller.

Credentials come from the environment only (UNIFI_HOST, UNIFI_USER,
UNIFI_PASSWORD) - never from a flag, a file in this repo, or a default, so
they cannot end up in shell history, a fixture or a log line.
"""

import argparse
import json
import os
import sys

import httpx

from .model import empty_document, mac_is_randomised, validation_errors
from .store import StaleVersion, Store

# UniFi OS (UDM / UDM SE / UNAS) fronts the network application on port 443,
# NOT the classic controller's 8443 - and proxies the API under /proxy/network.
LOGIN_PATH = "/api/auth/login"
V1 = "/proxy/network/api/s/default"
V2 = "/proxy/network/v2/api/site/default"


class UniFiClient:
    def __init__(self, host: str, username: str, password: str, verify_tls: bool):
        # verify_tls defaults to False at the CLI: a UDM ships a self-signed
        # certificate and most homelabs never replace it. --verify-tls turns
        # verification back on for anyone who did.
        self._client = httpx.Client(base_url=f"https://{host}", verify=verify_tls, timeout=30.0)
        self._username = username
        self._password = password

    def login(self) -> None:
        resp = self._client.post(
            LOGIN_PATH, json={"username": self._username, "password": self._password}
        )
        resp.raise_for_status()  # cookie jar on the client keeps the session

    def get(self, path: str) -> list:
        """GET one endpoint; both API generations wrap the payload differently."""
        resp = self._client.get(path)
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict) and "data" in body:
            return body["data"]  # v1 wraps in {"meta": ..., "data": [...]}
        return body  # v2 returns the bare list (or [] when nothing is configured)

    def fetch_all(self) -> dict:
        return {
            "networkconf": self.get(f"{V1}/rest/networkconf"),
            "user": self.get(f"{V1}/rest/user"),
            "sta": self.get(f"{V1}/stat/sta"),
            "device": self.get(f"{V1}/stat/device"),
            # Genuinely [] on a freshly-segmented network - handled, not assumed.
            "firewall_policies": self.get(f"{V2}/firewall-policies"),
            # ZONES, without which the policies above are unreadable. A zone
            # policy says zone_id -> zone_id; only this endpoint knows that
            # "6ab844dddb9b7fedb9a3a12c" is Iot and which networks are in it.
            "firewall_zones": self.get(f"{V2}/firewall/zone"),
        }


# UniFi replaced per-network isolation with ZONE-BASED policies, and a zone is
# not a VLAN: it is a bag of networks. Internal holds two, Iot holds one, and
# several hold none at all. cARPaccio's matrix is VLAN x VLAN, so a policy has
# to be projected down onto the VLANs its zones contain.
#
# THE PROJECTION IS LOSSY, AND IT LIES IN THE SAFE DIRECTION ON PURPOSE. A
# policy scoped to an IP RANGE inside a zone — "192.168.1.213-254 may reach
# Iot" — cannot be expressed as "home may reach iot". Answering that question
# with a flat yes would be worse than useless: somebody would read the matrix,
# believe the whole home VLAN can reach the cameras, and be wrong. So a
# range-scoped endpoint keeps the zone name AND the qualifier, and the rule
# reads "Internal (192.168.1.213-254)" rather than pretending to be simple.
def _zone_lookup(raw: dict) -> tuple[dict, dict, dict]:
    """zone id -> name, zone id -> the VLAN names inside it, and network id -> name."""
    zones = raw.get("firewall_zones") or []
    names = {}
    vlans = {}
    networks = {n["_id"]: n["name"] for n in raw.get("networkconf", [])}
    for z in zones:
        zid = str(z.get("_id", ""))
        if not zid:
            continue
        names[zid] = z.get("name", zid)
        vlans[zid] = [networks[i] for i in (z.get("network_ids") or []) if i in networks]
    return names, vlans, networks


def _ports(ep: dict | None) -> str:
    """The ports one side of a policy matches, in the schema's words.

    EMPTY MEANS EVERY PORT in the schema, so empty is only ever written for
    ANY. Until HOME-950 this was hard-coded to "", and a rule that let iot
    reach the gateway on 53, 67 and 123 read as iot reaching every port on it.
    SPECIFIC carries the list in `port` ("53,67,123"). Any other type - a port
    group, something a later UniFi adds - keeps whatever names it rather than
    falling back to empty: the same "never call it any" rule as
    _endpoint_name below.
    """
    if not ep:
        return ""
    kind = str(ep.get("port_matching_type", "ANY")).upper()
    if kind in ("ANY", ""):
        return ""
    ports = str(ep.get("port") or ep.get("port_group_id") or kind)
    return f"not {ports}" if ep.get("match_opposite_ports") else ports


def _description(p: dict) -> str:
    """The policy's own description, plus its SOURCE ports when it has any.

    A rule has one `ports` field and it means the destination. The "(Return)"
    rules UniFi makes for an allow rule match on the source port instead -
    home_to_talos (Return) answers FROM 6443 and 50000 - so those go here, in
    words, rather than being lost or put in a field that means the other end.
    """
    text = p.get("description") or ""
    src_ports = _ports(p.get("source"))
    if src_ports:
        text = f"{text} source port {src_ports}" if text else f"source port {src_ports}"
    return text


def _endpoint_name(ep: dict | None, zone_names: dict, vlan_by_zone: dict, networks: dict) -> str:
    """What one side of a policy actually matches, in words.

    Falls back to the zone id rather than to "any": an unknown zone is a thing
    we could not name, and calling it "any" would turn a specific rule into a
    universal one — the single most dangerous mistake this file can make.
    """
    if not ep:
        return "any"
    zid = str(ep.get("zone_id", ""))
    label = zone_names.get(zid, zid or "any")

    # NAME THE VLANS, NOT THE ZONE, whenever the zone contains any — and this
    # is what makes the matrix work rather than merely read nicely.
    #
    # The matrix asks "can home reach iot", VLAN against VLAN. A rule whose
    # source says "Internal" answers nothing, because no VLAN is called that,
    # and the cell falls through to "no rule" — which renders a BLOCKED pair as
    # open. That is the exact failure this whole file exists to prevent, so the
    # rule names the members instead: Internal becomes "home, guest".
    #
    # A zone with no VLANs in it (External holds three WANs, Gateway holds
    # nothing) keeps its zone name. There is no VLAN to match, and inventing
    # one would be worse than showing the truth.
    members = vlan_by_zone.get(zid) or []
    if members:
        label = ", ".join(sorted(members))

    target = str(ep.get("matching_target", "ANY")).upper()
    if target == "NETWORK" and ep.get("network_ids"):
        # By name, the way the rest of the document names a VLAN: a raw id
        # ("home (693d6285743b520e6e124f19)") is true and tells nobody
        # anything. An id networkconf does not know stays as the id.
        named = ", ".join(networks.get(i, i) for i in ep["network_ids"])
        label = f"{label} (network {named})"
    elif target not in ("ANY", ""):
        detail = (
            ep.get("ips")
            or ep.get("ip_group_id")
            or ep.get("network_ids")
            or ep.get("matching_target_type")
            or target
        )
        if isinstance(detail, list):
            detail = ", ".join(str(x) for x in detail[:3])
        label = f"{label} ({detail})"
    return label


def parse(raw: dict) -> dict:
    """Turn the raw endpoint payloads into a valid cARPaccio document."""
    networks = raw["networkconf"]
    id_to_name = {n["_id"]: n["name"] for n in networks}
    active_macs = {s["mac"].lower() for s in raw["sta"]}

    wans = [
        {
            "name": n["name"],
            "wan_type": n.get("wan_type", ""),
            "failover_priority": n.get("wan_failover_priority"),
            "notes": "",
            "source": "unifi",
        }
        for n in networks
        if n.get("purpose") == "wan"
    ]

    vlans = []
    for n in networks:
        if n.get("purpose") != "corporate":
            continue
        vlans.append(
            {
                "name": n["name"],
                # vlan_enabled False = the untagged/native LAN -> null tag.
                "vlan_id": n.get("vlan") if n.get("vlan_enabled") else None,
                "subnet": n.get("ip_subnet", ""),
                # THE SEPARATION THAT IS NOT A FIREWALL RULE. UniFi's
                # "Network Isolation" switch blocks this network from reaching
                # every other local network, and it appears in NO rule list --
                # rest/firewallrule, rest/firewallgroup, v2 firewall-policies,
                # firewall/zone and trafficrules were all empty on a network
                # where iot and guest are, in fact, isolated. Miss this field
                # and the tool confidently reports segmentation that does not
                # exist as missing, which is the most damaging thing it could
                # possibly get wrong.
                "isolated": bool(n.get("network_isolation_enabled")),
                "dhcp": {
                    "enabled": bool(n.get("dhcpd_enabled")),
                    "start": n.get("dhcpd_start", ""),
                    "stop": n.get("dhcpd_stop", ""),
                    "lease_seconds": n.get("dhcpd_leasetime"),
                    "dns": [
                        n[k]
                        for k in ("dhcpd_dns_1", "dhcpd_dns_2", "dhcpd_dns_3", "dhcpd_dns_4")
                        if n.get(k)
                    ],
                },
                "notes": "",
                "source": "unifi",
            }
        )

    devices = []
    for u in raw["user"]:
        mac = u["mac"].lower()
        reservation = bool(u.get("use_fixedip"))
        devices.append(
            {
                "mac": mac,
                "name": u.get("name") or "",
                "hostname": u.get("hostname") or "",
                # A reservation's address is config; last_ip is observation.
                "ip": (u.get("fixed_ip") if reservation else u.get("last_ip")) or "",
                "vlan": id_to_name.get(u.get("last_connection_network_id"))
                or u.get("last_connection_network_name")
                or "",
                "assignment": "reservation" if reservation else "dhcp",
                "wired": bool(u.get("is_wired")),
                "active": mac in active_macs,
                "oui": u.get("oui") or "",
                "last_seen": u.get("last_seen"),
                "notes": "",
                "source": "unifi",
            }
        )

    infrastructure = [
        {
            "mac": d["mac"].lower(),
            "name": d.get("name") or d.get("model", ""),
            # Recorded separately so merge() can tell a human rename from the
            # controller's own name. parse() MUST set it: without it, the first
            # merge sees name != unifi_name on every row, decides a human
            # renamed all of them, and freezes the lot at their current names.
            "unifi_name": d.get("name") or d.get("model", ""),
            "model": d.get("model", ""),
            "kind": d.get("type", ""),
            "ip": d.get("ip", ""),
            "notes": "",
        }
        for d in raw["device"]
    ]

    zone_names, vlan_by_zone, network_names = _zone_lookup(raw)

    firewall_rules = [
        {
            "id": str(p.get("_id", p.get("id", ""))),
            "name": p.get("name", ""),
            "action": {"ALLOW": "allow", "BLOCK": "drop", "REJECT": "reject"}.get(
                str(p.get("action", "")).upper(), "drop"
            ),
            "src": _endpoint_name(p.get("source"), zone_names, vlan_by_zone, network_names),
            "dst": _endpoint_name(p.get("destination"), zone_names, vlan_by_zone, network_names),
            "protocol": p.get("protocol", "any"),
            "ports": _ports(p.get("destination")),
            "enabled": bool(p.get("enabled", True)),
            "description": _description(p),
            "source": "unifi",
        }
        for p in raw["firewall_policies"]
    ]

    return {
        "schema_version": 1,
        "wans": wans,
        "vlans": vlans,
        "devices": devices,
        "infrastructure": infrastructure,
        "firewall_rules": firewall_rules,
    }


def merge(existing: dict, imported: dict) -> dict:
    """Merge an import into the stored document. Hand-written data survives.

    Keyed on MAC. The controller wins on what it can actually observe
    (addresses, activity, VLAN membership); the human wins on what only a
    human knows (notes, `static` entries the controller cannot see, names
    the controller never learned).
    """
    merged = {
        "schema_version": 1,
        # No track_rename: unifi wans are keyed BY name, so a rename would not
        # match the next import and would duplicate the row rather than rename
        # it. The UI keeps a unifi WAN's name read-only for exactly this reason,
        # and lets a MANUAL one be renamed freely — _merge_wans below never
        # matches those against an import at all.
        "wans": _merge_wans(existing["wans"], imported["wans"]),
        "vlans": _merge_vlans(existing["vlans"], imported["vlans"]),
        "devices": [],
        "infrastructure": _merge_by(
            existing["infrastructure"], imported["infrastructure"], "mac", track_rename=True
        ),
        "firewall_rules": _merge_rules(existing["firewall_rules"], imported["firewall_rules"]),
        # Reserved addresses are hand-written by definition — a VIP or a
        # load-balancer pool is exactly what no controller can report. An
        # import must never drop them, or the gap they exist to close reopens
        # silently at the next sync (HOME-445).
        "reserved": list(existing.get("reserved", []) or []),
    }

    existing_devices = {d["mac"]: d for d in existing["devices"]}
    seen = set()
    for imp in imported["devices"]:
        old = existing_devices.get(imp["mac"])
        if old is None:
            merged["devices"].append(imp)
        else:
            keep = dict(imp)
            keep["notes"] = old.get("notes", "") or imp.get("notes", "")
            if old.get("name") and not imp.get("name"):
                keep["name"] = old["name"]
            if old.get("assignment") == "static":
                # A static address lives on the host itself; no API will ever
                # report it, so the controller's view must not demote it.
                keep["assignment"] = "static"
                keep["ip"] = old.get("ip") or imp.get("ip", "")
            merged["devices"].append(keep)
        seen.add(imp["mac"])
    # Devices the controller has never heard of (hand-added statics) survive.
    merged["devices"].extend(d for m, d in existing_devices.items() if m not in seen)
    return merged


def _merge_rules(existing: list, imported: list) -> list:
    """Firewall rules, split into the controller's and the human's like VLANs.

    The controller's rules are REPLACED on every import, and dropped when the
    controller no longer has them. Until HOME-950 the merge kept every stored
    rule and appended only (id, name) pairs it had not seen, on the theory
    that UniFi had no rules and these were all hand-written. With 145 zone
    policies that went wrong three ways: a changed rule kept its old row, a
    renamed one appeared under both names, and a deleted one stayed for ever.
    The store read 151 rules against the controller's 145, and the matrix
    answered from the stale ones.

    A rule is the controller's when it says `source: unifi`, or when it has no
    `source` (stored before the field existed) and its id is one the
    controller reports. Everything else is hand-written and survives. An
    unknown id with no `source` is KEPT, not dropped: deleting a note somebody
    wrote is the mistake that cannot be seen afterwards.

    The controller's rules come first and in its order, because the matrix
    shows the FIRST matching rule and the controller's are what the gateway
    enforces. A hand-written rule then speaks only where the controller says
    nothing.
    """
    controller_ids = {r["id"] for r in imported}

    def from_controller(rule: dict) -> bool:
        if "source" in rule:
            return rule["source"] == "unifi"
        return rule.get("id") in controller_ids

    return list(imported) + [r for r in existing if not from_controller(r)]


def _merge_wans(existing: list, imported: list) -> list:
    """WAN uplinks, with hand-added ones held apart from the controller's.

    A MANUAL uplink — a 5G dongle that is not plugged in yet, a second line
    ordered but not delivered, an ISP account somebody wants written down — has
    no counterpart on the controller and must never be matched against one.
    Two reasons it gets its own path rather than going through _merge_by:

      * _merge_by keys on `name`, and a freshly added row's name starts EMPTY.
        Two blank names would collapse into one dict entry and silently eat a
        row. Manual entries skip the dict entirely.
      * a manual row is renameable precisely because nothing will ever try to
        match it by name, which is the opposite of the rule for unifi rows.
    """
    manual = [w for w in existing if w.get("source") == "manual"]
    from_controller = [w for w in existing if w.get("source") != "manual"]
    return _merge_by(from_controller, imported, "name") + manual


def _merge_vlans(existing: list, imported: list) -> list:
    """VLANs, same shape as _merge_wans and for the same reason.

    VLANs are merged BY NAME, so a unifi VLAN's name is its merge key and the
    UI keeps it read-only - renaming one here would not match the next import
    and would DUPLICATE the segment rather than rename it. A MANUAL VLAN (a
    segment being planned, or one on gear UniFi does not manage) never has a
    controller counterpart: it skips the name-keyed dict entirely, which is
    also what makes it safely renameable.

    A row with no `source` predates the field and can only have come from the
    controller (manual creation arrived together with the field), so absent
    is read as unifi - the cautious side, since wrongly freezing a name is
    harmless and wrongly duplicating a VLAN is not.

    On unifi rows the controller wins field-wise (same as devices): subnet,
    dhcp and isolation are ITS config, and stale hand-corrections should heal
    on the next import. Only notes are the human's (via _merge_by).
    """
    manual = [v for v in existing if v.get("source") == "manual"]
    from_controller = [v for v in existing if v.get("source") != "manual"]
    return _merge_by(from_controller, imported, "name") + manual


def _merge_by(existing: list, imported: list, key: str, *, track_rename: bool = False) -> list:
    """Imported entries win field-wise, but non-empty notes survive; entries
    only the human knows about are kept.

    A HUMAN RENAME ALSO SURVIVES, and it needs two fields to do so. The
    controller's name arrives on every import, so keeping "the stored name"
    would freeze the row for ever and a rename in UniFi would never appear.
    Instead the controller's name is recorded separately as `unifi_name`: if
    the stored `name` differs from the stored `unifi_name`, a human typed it
    and it wins; if they match, nobody has renamed this row and it tracks the
    controller. Without this, making the name editable in the UI would be a
    lie — the next import would quietly undo it.
    """
    old = {e[key]: e for e in existing}
    out = []
    for imp in imported:
        prev = old.pop(imp[key], None)
        entry = dict(imp)
        if track_rename:
            entry["unifi_name"] = imp.get("name", "")
        if prev:
            if prev.get("notes"):
                entry["notes"] = prev["notes"]
            # `"unifi_name" in prev`, not `prev.get(...)`: a document stored
            # BEFORE this field existed has no unifi_name at all, and reading
            # its absence as an empty string would mark every existing row as
            # renamed and freeze the lot. Absent means "cannot tell" — trust
            # the controller.
            renamed = (
                track_rename
                and "unifi_name" in prev
                and prev.get("name")
                and prev["name"] != prev["unifi_name"]
            )
            if renamed:
                entry["name"] = prev["name"]
        out.append(entry)
    out.extend(old.values())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="carpaccio-import",
        description="Read the UniFi controller (read-only) and merge into the stored document.",
    )
    ap.add_argument(
        "--verify-tls",
        action="store_true",
        help="verify the controller's TLS certificate (default: off, UDMs are self-signed)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the merged document to stdout instead of writing to S3",
    )
    args = ap.parse_args(argv)

    missing = [k for k in ("UNIFI_HOST", "UNIFI_USER", "UNIFI_PASSWORD") if not os.environ.get(k)]
    if missing:
        print(f"missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    client = UniFiClient(
        host=os.environ["UNIFI_HOST"],
        username=os.environ["UNIFI_USER"],
        password=os.environ["UNIFI_PASSWORD"],
        verify_tls=args.verify_tls,
    )
    client.login()
    imported = parse(client.fetch_all())

    if args.dry_run:
        # No S3 involved: merge against an empty document and print the result.
        merged = merge(empty_document(), imported)
        errors = validation_errors(merged)
        if errors:
            print("parsed document is invalid:", file=sys.stderr)
            for e in errors:
                print(f"  {e}", file=sys.stderr)
            return 1
        json.dump(merged, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0

    store = Store.from_env()

    # Load-merge-save under the same optimistic lock the UI uses; retry a
    # couple of times if a human saves mid-import.
    for attempt in range(3):
        existing, version = store.load()
        merged = merge(existing, imported)
        errors = validation_errors(merged)
        if errors:
            print("refusing to write an invalid document:", file=sys.stderr)
            for e in errors:
                print(f"  {e}", file=sys.stderr)
            return 1
        try:
            store.save(merged, expected_version=version)
        except StaleVersion:
            if attempt == 2:
                print(
                    "gave up after 3 conflicting writes - is something saving in a loop?",
                    file=sys.stderr,
                )
                return 1
            continue
        randomised = sum(
            1 for d in merged["devices"] if d["active"] and mac_is_randomised(d["mac"])
        )
        print(
            f"imported: {len(merged['vlans'])} VLANs, {len(merged['wans'])} WANs, "
            f"{len(merged['devices'])} devices "
            f"({sum(1 for d in merged['devices'] if d['assignment'] == 'reservation')} "
            f"reservations, {randomised} active on a randomised MAC)"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
