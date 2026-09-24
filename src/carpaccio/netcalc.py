"""Free-address arithmetic: which addresses are safe to hand out.

The question this answers, in the owner's words: "easily find a free reserved
IP out of the DHCP pool". An address is SAFE to use for a reservation or a
host-configured static when it is inside the subnet's usable range but
OUTSIDE the DHCP pool (the DHCP server will never lease it to someone else),
and nothing in the document already sits on it.

Assumptions, stated rather than implied:

- The subnet string is UniFi's ip_subnet, which is GATEWAY/prefix (e.g.
  "192.168.1.1/24"), not network/prefix. The gateway is therefore derived
  from the host bits of that address - not hardcoded ".1". If the host bits
  are zero (a plain network address from some other source), the gateway is
  unknown and nothing extra is excluded.
- The network and broadcast addresses are never usable.
- Anything with an IP is "taken", whatever its assignment: a device marked
  dhcp whose address sits OUTSIDE the pool is a stale lease or a moved host,
  and handing its address out again is exactly the collision this tool
  exists to prevent. Infrastructure (APs, switches) counts too.
- A malformed or empty device IP takes nothing.
- DHCP disabled, or an unparseable pool, means the whole usable range is a
  candidate (still useful for statics).

THIS FILE HAS A JAVASCRIPT TWIN: src/carpaccio/static/netcalc.js renders the
same answer live in the browser as the document is edited. A pytest runs both
over the same scenarios and compares JSON, so they cannot drift silently -
change one, change both, run tests/test_netcalc.py.
"""

# Refuse to enumerate anything wider than a /16. Nobody hand-reserves
# addresses in a /8, and an honest refusal beats a hung tab (the JS twin
# would be building 16M chips).
MAX_ENUMERABLE = 65536


def parse_ip(text) -> int | None:
    """Dotted quad to int, or None. No exceptions: malformed input is an
    everyday value here (a half-typed cell), not an error."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if len(parts) != 4:
        return None
    value = 0
    for p in parts:
        if not p.isdigit() or len(p) > 3:
            return None
        n = int(p)
        if n > 255:
            return None
        value = value * 256 + n
    return value


def format_ip(value: int) -> str:
    return ".".join(str((value >> shift) & 255) for shift in (24, 16, 8, 0))


def collapse_ranges(addresses: list[int]) -> list[dict]:
    """Sorted ints -> [{start, end, size}] of consecutive runs, as strings.
    60 free addresses read as three ranges, not sixty lines."""
    ranges = []
    for addr in addresses:
        if ranges and addr == ranges[-1]["_end"] + 1:
            ranges[-1]["_end"] = addr
        else:
            ranges.append({"_start": addr, "_end": addr})
    return [
        {
            "start": format_ip(r["_start"]),
            "end": format_ip(r["_end"]),
            "size": r["_end"] - r["_start"] + 1,
        }
        for r in ranges
    ]


def reserved_ips(reserved: list | None) -> list[str]:
    """Every address in the reserved section, ranges expanded.

    A range is stored as two endpoints rather than one row per address,
    because MetalLB's pool is written down as ".240-.244" by the person who
    configured it, and a list that does not read like the thing it mirrors is
    a list that drifts from it.

    THIS LIVES IN netcalc, not in the API layer, because it feeds a free-address
    answer and there are TWO of those: the server's, and the one the page
    computes live so the panel tracks unsaved edits. It was in app.py alone,
    so the API excluded the k3s API VIP from "free" while the browser sitting
    in front of the same document went on offering it — the exact bug the
    reserved section was built to close, surviving in the half a human looks
    at. The twin in netcalc.js is checked against this one by
    tests/test_netcalc.py.
    """
    out: list[str] = []
    for entry in reserved or []:
        start = parse_ip(entry.get("ip", ""))
        if start is None:
            continue
        end = parse_ip(entry.get("ip_end", "")) if entry.get("ip_end") else start
        if end is None or end < start:
            end = start
        # A typo must not hang the request enumerating four billion addresses.
        if end - start > 4096:
            end = start
        out.extend(format_ip(a) for a in range(start, end + 1))
    return out


def free_addresses(subnet: str, dhcp: dict | None, taken_ips: list[str]) -> dict:
    """The free-address answer for one VLAN.

    Returns {"free_count", "free_ranges", "gateway", "pool", "reason"};
    "reason" is non-None exactly when the answer could not be computed, and
    says why in words meant for the UI.
    """
    result = {"free_count": 0, "free_ranges": [], "gateway": None, "pool": None, "reason": None}

    addr_str, _, prefix_str = (subnet or "").partition("/")
    addr = parse_ip(addr_str)
    prefix = int(prefix_str) if prefix_str.isdigit() else None
    if addr is None or prefix is None or not 0 <= prefix <= 32:
        result["reason"] = "no usable subnet on this VLAN - set one in the details panel"
        return result
    if prefix >= 31:
        result["reason"] = f"a /{prefix} has no room for reservations"
        return result
    span = 1 << (32 - prefix)
    if span > MAX_ENUMERABLE:
        result["reason"] = f"a /{prefix} is too large to enumerate address by address"
        return result

    mask = ((1 << prefix) - 1) << (32 - prefix)
    network = addr & mask
    broadcast = network | (~mask & 0xFFFFFFFF)
    # UniFi's ip_subnet is gateway/prefix; host bits present = that IS the
    # gateway. Host bits zero = a bare network address, gateway unknown.
    gateway = addr if addr != network else None
    result["gateway"] = format_ip(gateway) if gateway is not None else None

    pool_start = pool_stop = None
    if dhcp and dhcp.get("enabled"):
        pool_start = parse_ip(dhcp.get("start"))
        pool_stop = parse_ip(dhcp.get("stop"))
        if pool_start is None or pool_stop is None or pool_start > pool_stop:
            # A half-filled or backwards pool excludes nothing; saying the
            # whole subnet is inside a pool that does not parse would hide
            # every free address for a typo.
            pool_start = pool_stop = None
        else:
            result["pool"] = {"start": format_ip(pool_start), "end": format_ip(pool_stop)}

    taken = {ip for ip in (parse_ip(t) for t in taken_ips) if ip is not None}

    free = [
        a
        for a in range(network + 1, broadcast)
        if not (pool_start is not None and pool_start <= a <= pool_stop)
        and a != gateway
        and a not in taken
    ]
    result["free_count"] = len(free)
    result["free_ranges"] = collapse_ranges(free)
    return result
