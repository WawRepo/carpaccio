"""Free-address arithmetic, in both languages, plus the view logic.

netcalc.py answers over the API; netcalc.js answers live in the page. The
parity test here runs BOTH over the same scenarios and compares JSON - the
only thing standing between the twins and silent drift.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from carpaccio.importer import parse
from carpaccio.netcalc import free_addresses, reserved_ips
from carpaccio.store import MISSING

NETCALC_JS = Path(__file__).parent.parent / "src" / "carpaccio" / "static" / "netcalc.js"

POOL = {"enabled": True, "start": "192.168.1.6", "stop": "192.168.1.190"}


def flat(result) -> list[str]:
    """Every free address as a string, expanded from the ranges."""
    out = []
    for r in result["free_ranges"]:
        start = [int(x) for x in r["start"].split(".")]
        base = ((start[0] * 256 + start[1]) * 256 + start[2]) * 256 + start[3]
        for i in range(r["size"]):
            v = base + i
            out.append(".".join(str((v >> s) & 255) for s in (24, 16, 8, 0)))
    return out


def test_slash24_home_shape():
    """The owner's own case: /24, pool .6-.190, gateway derived from the
    UniFi gateway/prefix subnet string - candidates are .2-.5 and .191-.254."""
    r = free_addresses("192.168.1.1/24", POOL, [])
    assert r["reason"] is None
    assert r["gateway"] == "192.168.1.1"
    assert r["pool"] == {"start": "192.168.1.6", "end": "192.168.1.190"}
    addresses = flat(r)
    assert r["free_count"] == len(addresses) == 4 + 64  # .2-.5 and .191-.254
    assert addresses[0] == "192.168.1.2" and addresses[-1] == "192.168.1.254"
    assert "192.168.1.1" not in addresses  # gateway
    assert "192.168.1.0" not in addresses  # network
    assert "192.168.1.255" not in addresses  # broadcast
    assert "192.168.1.100" not in addresses  # inside the pool


def test_taken_addresses_split_the_ranges():
    r = free_addresses("192.168.1.1/24", POOL, ["192.168.1.3", "192.168.1.212"])
    addresses = flat(r)
    assert "192.168.1.3" not in addresses and "192.168.1.212" not in addresses
    assert r["free_count"] == 66
    assert r["free_ranges"][0] == {"start": "192.168.1.2", "end": "192.168.1.2", "size": 1}


def test_dhcp_lease_outside_the_pool_is_taken_not_free():
    """A device marked dhcp sitting outside the pool is a stale lease or a
    moved host - handing out its address is the collision this exists to
    prevent. Taken is taken, whatever the assignment said."""
    r = free_addresses("192.168.1.1/24", POOL, ["192.168.1.200"])
    assert "192.168.1.200" not in flat(r)


def test_slash29():
    # 10.0.0.0/29: usable .1-.6, gateway .1 taken from the subnet string.
    r = free_addresses("10.0.0.1/29", None, [])
    assert flat(r) == ["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"]


def test_pool_covering_the_whole_usable_range_means_none_free():
    r = free_addresses(
        "192.168.5.1/24", {"enabled": True, "start": "192.168.5.2", "stop": "192.168.5.254"}, []
    )
    assert r["free_count"] == 0 and r["free_ranges"] == []
    assert r["reason"] is None  # a real answer of zero, not a failure to answer


def test_network_address_subnet_means_gateway_unknown():
    """Host bits zero = a bare network address: the gateway cannot be derived,
    so nothing extra is excluded - .1 shows as free rather than being
    guessed at. Deriving honestly beats hardcoding .1."""
    r = free_addresses("192.168.7.0/24", None, [])
    assert r["gateway"] is None
    assert "192.168.7.1" in flat(r)


def test_degenerate_subnets_refuse_with_a_reason():
    assert "no usable subnet" in free_addresses("", None, [])["reason"]
    assert "no usable subnet" in free_addresses("not-an-ip/24", None, [])["reason"]
    assert "no usable subnet" in free_addresses("192.168.1.1", None, [])["reason"]
    assert "/31" in free_addresses("10.0.0.1/31", None, [])["reason"]
    assert "/32" in free_addresses("10.0.0.1/32", None, [])["reason"]
    assert "too large" in free_addresses("10.0.0.1/8", None, [])["reason"]
    # /16 is exactly the enumeration cap and still answers
    assert free_addresses("10.10.0.1/16", None, [])["reason"] is None


def test_malformed_input_takes_nothing_and_breaks_nothing():
    r = free_addresses("192.168.1.1/24", POOL, ["", "garbage", "300.1.2.3", "1.2.3"])
    assert r["free_count"] == 68
    # a half-typed pool excludes nothing rather than hiding every address
    r2 = free_addresses("192.168.1.1/24", {"enabled": True, "start": "192.", "stop": ""}, [])
    assert r2["pool"] is None and r2["free_count"] == 253  # usable minus gateway


def test_api_endpoint(client, unifi_raw):
    doc = parse(unifi_raw)
    client.put("/api/network", json={"version": MISSING, "document": doc})

    resp = client.get("/api/vlans/home/free")
    assert resp.status_code == 200
    body = resp.json()
    assert body["vlan"] == "home"
    assert body["gateway"] == "192.168.1.1"
    assert body["reason"] is None
    assert 0 < body["free_count"] < 68  # some of .191-.254 are taken by real devices
    taken_ips = {d["ip"] for d in doc["devices"]} | {i["ip"] for i in doc["infrastructure"]}
    assert not (set(flat(body)) & taken_ips), "an address in use was reported free"

    assert client.get("/api/vlans/nope/free").status_code == 404

    # a vlan whose subnet is not decided yet answers with a reason, not a 500
    client.post("/api/vlans", json={"name": "planned", "vlan_id": None, "subnet": ""})
    body2 = client.get("/api/vlans/planned/free").json()
    assert body2["free_count"] == 0 and "no usable subnet" in body2["reason"]


# ---------------------------------------------------------------- the twins

PARITY_SCENARIOS = [
    {"subnet": "192.168.1.1/24", "dhcp": POOL, "taken": ["192.168.1.3", "192.168.1.212"]},
    {"subnet": "192.168.1.1/24", "dhcp": POOL, "taken": []},
    {"subnet": "10.0.0.1/29", "dhcp": None, "taken": ["10.0.0.4"]},
    {"subnet": "192.168.7.0/24", "dhcp": None, "taken": []},
    {"subnet": "", "dhcp": None, "taken": []},
    {"subnet": "10.0.0.1/31", "dhcp": None, "taken": []},
    {"subnet": "10.0.0.1/8", "dhcp": None, "taken": []},
    {
        "subnet": "192.168.5.1/24",
        "dhcp": {"enabled": True, "start": "192.168.5.2", "stop": "192.168.5.254"},
        "taken": ["garbage", ""],
    },
    {"subnet": "192.168.9.1/24", "dhcp": {"enabled": True, "start": "x", "stop": ""}, "taken": []},
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_python_and_javascript_twins_agree():
    """Identical JSON from netcalc.py and netcalc.js for every scenario.
    This is the test that lets two implementations exist at all."""
    script = (
        f"const n = require({json.dumps(str(NETCALC_JS))});"
        f"const scenarios = {json.dumps(PARITY_SCENARIOS)};"
        "console.log(JSON.stringify(scenarios.map("
        "  (s) => n.freeAddresses(s.subnet, s.dhcp, s.taken))));"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    js_results = json.loads(proc.stdout)
    py_results = [free_addresses(sc["subnet"], sc["dhcp"], sc["taken"]) for sc in PARITY_SCENARIOS]
    assert js_results == py_results


RESERVED_SCENARIOS = [
    # a single address, a range, a range written backwards, a typo'd endpoint,
    # an empty row, and a range so wide it must be refused rather than enumerated
    [{"ip": "192.168.1.230", "name": "k3s API VIP"}],
    [{"ip": "192.168.1.240", "ip_end": "192.168.1.244", "name": "MetalLB pool"}],
    [{"ip": "192.168.1.244", "ip_end": "192.168.1.240", "name": "backwards"}],
    [{"ip": "192.168.1.5", "ip_end": "not an address", "name": "typo"}],
    [{"ip": "", "name": "half typed"}],
    [{"ip": "10.0.0.1", "ip_end": "10.9.0.1", "name": "absurd"}],
    [],
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_reserved_expansion_twins_agree():
    """The reserved expansion has a twin too, and it MUST: it feeds the
    free-address answer on both sides. While it lived only in app.py the API
    refused to offer the k3s API VIP and the browser panel offered it anyway,
    from the same document - the bug the reserved section exists to close,
    surviving in the half a person actually clicks."""
    script = (
        f"const n = require({json.dumps(str(NETCALC_JS))});"
        f"const scenarios = {json.dumps(RESERVED_SCENARIOS)};"
        "console.log(JSON.stringify(scenarios.map((s) => n.reservedIps(s))));"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [reserved_ips(sc) for sc in RESERVED_SCENARIOS]


def test_a_reserved_range_is_expanded_and_an_absurd_one_is_not():
    """Stated in its own right, so the twin test cannot pass by both sides
    being wrong in the same way."""
    assert reserved_ips([{"ip": "192.168.1.240", "ip_end": "192.168.1.244"}]) == [
        f"192.168.1.{n}" for n in range(240, 245)
    ]
    # 4096 is the cap; a range past it collapses to its first address rather
    # than enumerating, so a slipped keystroke cannot hang a request.
    assert reserved_ips([{"ip": "10.0.0.1", "ip_end": "10.9.0.1"}]) == ["10.0.0.1"]
    assert reserved_ips([{"ip": "", "name": "half typed"}]) == []
    assert reserved_ips(None) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_view_logic_in_node():
    """The sorting and filtering that app.js uses, exercised where pytest can
    reach them. assert() throws and node exits non-zero on any failure."""
    script = f"""
const assert = require("assert");
const n = require({json.dumps(str(NETCALC_JS))});

// numeric IP order, not lexicographic
assert(n.compareDevices({{ip: "192.168.1.9"}}, {{ip: "192.168.1.10"}}, "ip", 1) < 0);
assert(n.compareDevices({{ip: "192.168.1.99"}}, {{ip: "192.168.1.100"}}, "ip", 1) < 0);
// empty and malformed IPs sort LAST IN BOTH DIRECTIONS, never as 0.0.0.0
for (const dir of [1, -1]) {{
  assert(n.compareDevices({{ip: ""}}, {{ip: "192.168.1.1"}}, "ip", dir) > 0);
  assert(n.compareDevices({{ip: "half.typed"}}, {{ip: "192.168.1.1"}}, "ip", dir) > 0);
  assert(n.compareDevices({{ip: "192.168.1.1"}}, {{ip: ""}}, "ip", dir) < 0);
}}
assert(n.compareDevices({{ip: ""}}, {{ip: "junk"}}, "ip", 1) === 0);
// text sort: case-insensitive, empty last both ways
assert(n.compareDevices({{name: "alpha"}}, {{name: "Beta"}}, "name", 1) < 0);
for (const dir of [1, -1]) {{
  assert(n.compareDevices({{name: ""}}, {{name: "alpha"}}, "name", dir) > 0);
}}
// direction actually reverses for present values
assert(n.compareDevices({{ip: "192.168.1.9"}}, {{ip: "192.168.1.10"}}, "ip", -1) > 0);

// IP query matches on octet boundaries: .2 is not a prefix of .25
assert(n.matchesQuery({{ip: "192.168.1.2"}}, "192.168.1.2"));
assert(!n.matchesQuery({{ip: "192.168.1.25"}}, "192.168.1.2"));
assert(n.matchesQuery({{ip: "192.168.1.25"}}, "192.168.1."));
assert(n.matchesQuery({{ip: "192.168.1.25"}}, "192.168"));
// a BARE number is one octet and matches any position, whole: the last-octet
// search the box is actually used for. "230" used to match nothing at all.
assert(n.matchesQuery({{ip: "192.168.1.230"}}, "230"));
assert(n.matchesQuery({{ip: "10.0.230.4"}}, "230"));
assert(!n.matchesQuery({{ip: "192.168.1.23"}}, "230"));
// and the octet boundary still holds in this direction: .2 is not .25
assert(n.matchesQuery({{ip: "192.168.1.2"}}, "2"));
assert(!n.matchesQuery({{ip: "192.168.1.25"}}, "2"));
// a bare number that IS a leading octet keeps working
assert(n.matchesQuery({{ip: "192.168.1.25"}}, "192"));
// free-text hits name, hostname and mac, case-insensitively
assert(n.matchesQuery({{name: "Basement Printer"}}, "printer"));
assert(n.matchesQuery({{hostname: "pi4", name: ""}}, "PI4"));
assert(n.matchesQuery({{mac: "d8:3a:dd:01:02:03", name: ""}}, "d8:3a"));
assert(!n.matchesQuery({{name: "printer", hostname: "", mac: ""}}, "camera"));

// dropdown filters AND together
const dev = {{name: "cam", mac: "b6:00:00:00:00:01", wired: false, active: true,
             assignment: "dhcp", ip: "192.168.2.5", hostname: ""}};
const none = {{q: "", assignment: "", rand: "", link: "", active: ""}};
assert(n.matchesFilters(dev, none));
assert(n.matchesFilters(dev, {{...none, rand: "yes", link: "wifi", active: "yes"}}));
assert(!n.matchesFilters(dev, {{...none, rand: "no"}}));
assert(!n.matchesFilters(dev, {{...none, assignment: "static"}}));
assert(!n.matchesFilters(dev, {{...none, rand: "yes", link: "wired"}}));
// an ENTIRELY empty row is a misclick; any content at all is data
assert(n.deviceIsEmpty({{name: "", ip: "", mac: "", hostname: "", notes: ""}}));
assert(n.deviceIsEmpty({{name: "  ", ip: "", mac: "", hostname: "", notes: ""}}));
assert(n.deviceIsEmpty({{assignment: "static", wired: true, active: false}}));
assert(!n.deviceIsEmpty({{name: "x", ip: "", mac: "", hostname: "", notes: ""}}));
assert(!n.deviceIsEmpty({{name: "", ip: "192.168.1.5", mac: "", hostname: "", notes: ""}}));
assert(!n.deviceIsEmpty({{name: "", ip: "", mac: "aa:bb:cc:dd:ee:ff", hostname: "", notes: ""}}));
assert(!n.deviceIsEmpty({{name: "", ip: "", mac: "", hostname: "h", notes: ""}}));
assert(!n.deviceIsEmpty({{name: "", ip: "", mac: "", hostname: "", notes: "keep me"}}));
console.log("view logic ok");
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_last_seen_label_and_sort_in_node():
    """HOME-370: an offline device says WHEN it was last seen, and the Active
    column sorts by that. TZ is pinned so the local-date formatting is exact."""
    script = f"""
const assert = require("assert");
const n = require({json.dumps(str(NETCALC_JS))});

const now = Date.UTC(2026, 8, 15, 12, 0);             // 2026-09-15 12:00
const nvr = Date.UTC(2026, 2, 21, 20, 8) / 1000;       // 2026-03-21 20:08
const l = n.lastSeenLabel(nvr, now);
assert.strictEqual(l.date, "2026-03-21");
assert.strictEqual(l.full, "2026-03-21 20:08");
assert.strictEqual(l.ago, "5 months ago");
assert.strictEqual(n.lastSeenLabel(now / 1000 - 3600, now).ago, "today");
assert.strictEqual(n.lastSeenLabel(now / 1000 - 86400, now).ago, "yesterday");
assert.strictEqual(n.lastSeenLabel(now / 1000 - 10 * 86400, now).ago, "10 days ago");
assert.strictEqual(n.lastSeenLabel(now / 1000 - 800 * 86400, now).ago, "2 years ago");
// no usable date is null, never 1970-01-01
for (const bad of [null, undefined, 0, -5, "1788729046", NaN]) {{
  assert.strictEqual(n.lastSeenLabel(bad, now), null);
}}

// Active sort: up counts as now; ascending = longest-gone first;
// an offline row with no date is last in BOTH directions.
const up = {{active: true, last_seen: 1}};
const old = {{active: false, last_seen: nvr}};
const recent = {{active: false, last_seen: nvr + 86400 * 100}};
const nodate = {{active: false, last_seen: null}};
const order = (dir) => [nodate, up, recent, old]
  .sort((a, b) => n.compareDevices(a, b, "active", dir));
assert.deepStrictEqual(order(1), [old, recent, up, nodate]);
assert.deepStrictEqual(order(-1), [up, recent, old, nodate]);
console.log("last seen ok");
"""
    env = {"PATH": os.environ.get("PATH", ""), "TZ": "UTC"}
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=False, env=env
    )
    assert proc.returncode == 0, proc.stderr
