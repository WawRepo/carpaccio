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
        }


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
            "model": d.get("model", ""),
            "kind": d.get("type", ""),
            "ip": d.get("ip", ""),
            "notes": "",
        }
        for d in raw["device"]
    ]

    firewall_rules = [
        {
            "id": str(p.get("_id", p.get("id", ""))),
            "name": p.get("name", ""),
            "action": {"ALLOW": "allow", "BLOCK": "drop", "REJECT": "reject"}.get(
                str(p.get("action", "")).upper(), "drop"
            ),
            "src": str(p.get("source", {}).get("network_id", "any")),
            "dst": str(p.get("destination", {}).get("network_id", "any")),
            "protocol": p.get("protocol", "any"),
            "ports": "",
            "enabled": bool(p.get("enabled", True)),
            "description": p.get("description", ""),
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
        "wans": _merge_by(existing["wans"], imported["wans"], "name"),
        "vlans": _merge_by(existing["vlans"], imported["vlans"], "name"),
        "devices": [],
        "infrastructure": _merge_by(existing["infrastructure"], imported["infrastructure"], "mac"),
        # UniFi has zero rules today, so rules are primarily hand-written:
        # keep every existing rule, append imported ones not already present.
        "firewall_rules": list(existing["firewall_rules"]),
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

    known_rules = {(r.get("id"), r["name"]) for r in merged["firewall_rules"]}
    merged["firewall_rules"].extend(
        r for r in imported["firewall_rules"] if (r.get("id"), r["name"]) not in known_rules
    )
    return merged


def _merge_by(existing: list, imported: list, key: str) -> list:
    """Imported entries win field-wise, but non-empty notes survive; entries
    only the human knows about are kept."""
    old = {e[key]: e for e in existing}
    out = []
    for imp in imported:
        prev = old.pop(imp[key], None)
        entry = dict(imp)
        if prev and prev.get("notes"):
            entry["notes"] = prev["notes"]
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
