#!/usr/bin/env python3
"""Rewrite raw UniFi API responses into committable test fixtures.

The committed fixtures under tests/fixtures/unifi/ are SYNTHETIC: this script
produced them from real controller responses that never entered the repo.
It preserves the structure the parser depends on - every key, null, empty
list, and cross-reference (network _id -> client network_id, device MAC ->
client uplink MAC) - while replacing everything that describes a real network:

- MACs are regenerated, but the two low bits of the first octet (the
  locally-administered and group bits) are PRESERVED, so exactly the same
  clients still test as "randomised MAC". That property is the point of the
  fixture.
- 192.168.x.y addresses keep their subnet; host numbers are remapped
  (gateway .1, broadcast, and the DHCP range boundaries stay put so the
  ranges still read sanely). Public addresses become 203.0.113.x (TEST-NET-3).
- Names/hostnames become neutral node-NN / host-NN, one replacement per
  distinct original, so named-vs-unnamed proportions survive. Only the LAN
  names (home/iot/guest) are kept - generic labels the tests and README use.
  WAN names would name the owner's ISPs, so they become wan-NN.
- Tokens, serials, Mongo ids, UUIDs, IPv6 addresses, the PPPoE username,
  802.1X identities, geo data and ISP names are all regenerated or blanked.

Deterministic (fixed seed): rerunning over the same input yields the same
output. Usage:

    python tests/fixtures/sanitise.py --input /path/to/raw --output tests/fixtures/unifi
"""

import argparse
import ipaddress
import json
import random
import re
from pathlib import Path

FILES = ["networkconf.json", "user.json", "sta.json", "device.json"]

MAC_RE = re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HEX24_RE = re.compile(r"^[0-9a-f]{24}$")
HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Host numbers that keep their value inside 192.168.0.0/16 so the fixtures
# still read like a network: gateway, DHCP range boundaries, broadcast.
KEEP_HOSTS = {0, 1, 6, 190, 254, 255}

NAME_KEYS = {"name", "hostname", "last_uplink_name", "uplink_device_name"}
# Only the LAN names stay: they are generic labels the tests and README use.
# WAN names are the owner's real ISPs, so they are remapped like any other name.
NETWORK_NAMES = {"home", "iot", "guest", "Guest"}


class Sanitiser:
    def __init__(self) -> None:
        self.rng = random.Random(0xCA9BACC0)  # fixed seed: deterministic output
        self.macs: dict[str, str] = {}
        self.used_macs: set[str] = set()
        self.hosts: dict[tuple[str, int], int] = {}  # (subnet, host) -> host
        self.free_hosts: dict[str, list[int]] = {}
        self.publics: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.hexes: dict[str, str] = {}
        self.ipv6s: dict[str, str] = {}
        self.identities: dict[str, str] = {}
        self.records: dict[str, str] = {}
        self.vendors: dict[str, str] = {}
        self.file = ""

    # --- value-shape transforms, applied to every string ------------------

    def map_mac(self, match: re.Match) -> str:
        mac = match.group(0).lower()
        if mac not in self.macs:
            while True:
                octets = [self.rng.randrange(256) for _ in range(6)]
                # Preserve the locally-administered and group bits: they are
                # what mac_is_randomised() reads, and the fixture must keep
                # exactly the same randomised/burned-in split as the live data.
                octets[0] = (octets[0] & 0xFC) | (int(mac[:2], 16) & 0x03)
                candidate = ":".join(f"{o:02x}" for o in octets)
                if candidate not in self.used_macs:
                    break
            self.macs[mac] = candidate
            self.used_macs.add(candidate)
        out = self.macs[mac]
        return out.upper() if match.group(0)[0].isupper() else out

    def map_ipv4(self, match: re.Match) -> str:
        ip = match.group(0)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return ip  # e.g. a version string that looks like an address
        if addr.is_loopback or addr.is_multicast or addr.is_unspecified or ip == "255.255.255.255":
            return ip
        octs = ip.split(".")
        if ip.startswith("192.168."):
            subnet, host = ".".join(octs[:3]), int(octs[3])
            if host in KEEP_HOSTS:
                return ip
            key = (subnet, host)
            if key not in self.hosts:
                pool = self.free_hosts.setdefault(
                    subnet, [h for h in range(2, 254) if h not in KEEP_HOSTS]
                )
                self.hosts[key] = pool.pop(self.rng.randrange(len(pool)))
            return f"{subnet}.{self.hosts[key]}"
        if addr.is_private:
            return ip  # 127.x / 10.x-style internals carry nothing identifying
        # Public address -> TEST-NET-3, one stable fake per real address.
        if ip not in self.publics:
            self.publics[ip] = f"203.0.113.{len(self.publics) + 1}"
        return self.publics[ip]

    def rand_hex(self, template: str) -> str:
        out = "".join(self.rng.choice("0123456789abcdef") for _ in template)
        return out.upper() if template.strip("0123456789").isupper() else out

    def map_stringlike(self, value: str) -> str:
        value = MAC_RE.sub(self.map_mac, value)
        value = IPV4_RE.sub(self.map_ipv4, value)
        for pattern in (HEX24_RE, HEX32_RE, UUID_RE):
            if pattern.match(value):
                if value not in self.hexes:
                    self.hexes[value] = "".join(
                        c if c == "-" else self.rng.choice("0123456789abcdef") for c in value
                    )
                return self.hexes[value]
        try:
            if ":" in value and ipaddress.ip_address(value).version == 6:
                if value not in self.ipv6s:
                    prefix = "fe80" if value.startswith("fe80") else "fd00"
                    tail = ":".join(f"{self.rng.randrange(0x10000):x}" for _ in range(4))
                    self.ipv6s[value] = f"{prefix}::{tail}"
                return self.ipv6s[value]
        except ValueError:
            pass
        return value

    # --- key-aware transforms ---------------------------------------------

    def map_name(self, value: str) -> str:
        if value not in self.names:
            self.names[value] = f"node-{len(self.names) + 1:02d}"
        return self.names[value]

    def transform(self, key: str, value):
        if isinstance(value, dict):
            return {k: self.transform(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.transform(key, v) for v in value]
        if not isinstance(value, str) or value == "":
            return value

        if key in NAME_KEYS:
            if value in NETWORK_NAMES:
                return value
            if self.file == "networkconf.json" and key == "name":
                return self.names.setdefault(value, f"wan-{len(self.names) + 1:02d}")
            return self.map_name(value)
        if key == "essid":
            if value in NETWORK_NAMES:
                return value
            return "wifi-" + self.rand_hex(value[6:]) if value.startswith("vwire-") else "wifi-01"
        if key == "wan_username":
            return "pppoe-user@example.invalid"
        if key == "last_1x_identity":
            return self.identities.setdefault(value, f"identity-{len(self.identities) + 1:02d}")
        if key in ("local_dns_record", "direct_connect_domain", "device_domain"):
            return self.records.setdefault(value, f"record-{len(self.records) + 1:02d}.lan")
        if key in ("oui", "dev_vendor") or key.startswith("isp_"):
            return self.vendors.setdefault(value, f"Vendor-{len(self.vendors) + 1:02d}")
        if key == "serial":
            return self.hexes.setdefault(value, self.rand_hex(value))
        if key in ("city", "country_name", "timezone", "country_code", "continent_code"):
            return {
                "city": "Exampletown",
                "country_name": "Nowhere",
                "timezone": "Etc/UTC",
                "country_code": "XX",
                "continent_code": "XX",
            }[key]
        if key.startswith("x_"):
            return "REDACTED"
        return self.map_stringlike(value)

    def transform_number(self, key: str, value):
        # Geo coordinates are the only numbers that identify anything.
        if key in ("latitude", "longitude") and isinstance(value, float):
            return 0.0
        if key == "asn" and isinstance(value, int):
            return 64512  # private-use ASN
        return value

    def walk(self, key: str, value):
        if isinstance(value, dict):
            return {k: self.walk(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.walk(key, v) for v in value]
        if isinstance(value, str):
            return self.transform(key, value)
        return self.transform_number(key, value)


def verify(outdir: Path) -> None:
    """The invariants the tests rely on must survive sanitising."""
    load = lambda n: json.loads((outdir / n).read_text())["data"]  # noqa: E731
    networks, users, stas = load("networkconf.json"), load("user.json"), load("sta.json")
    wans = [n for n in networks if n.get("purpose") == "wan"]
    lans = [n for n in networks if n.get("purpose") == "corporate"]
    fixed = [u for u in users if u.get("use_fixedip")]
    active_rand = [s for s in stas if int(s["mac"].split(":")[0], 16) & 2]
    macs = [u["mac"] for u in users]
    assert len(wans) == 3, len(wans)
    assert len(lans) == 3, len(lans)
    assert len(users) == 87, len(users)
    assert len(fixed) == 17, len(fixed)
    assert len(stas) == 41, len(stas)
    assert len(active_rand) == 8, len(active_rand)
    assert len(set(macs)) == len(macs), "sanitised MACs collided"
    ids = {n["_id"] for n in networks}
    assert all(u["last_connection_network_id"] in ids for u in users), "broken cross-reference"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True, type=Path, help="directory with raw responses")
    ap.add_argument("--output", required=True, type=Path, help="tests/fixtures/unifi")
    args = ap.parse_args()

    san = Sanitiser()
    args.output.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        san.file = name
        raw = json.loads((args.input / name).read_text())
        clean = san.walk("", raw)
        (args.output / name).write_text(json.dumps(clean, indent=1) + "\n")
        print(f"wrote {args.output / name}")
    verify(args.output)
    print("invariants hold: 3 WANs, 3 LANs, 87 clients, 17 reservations, 41 active, 8 randomised")


if __name__ == "__main__":
    main()
