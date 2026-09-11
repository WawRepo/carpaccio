/* Address arithmetic and view logic, shared between the page and node.
 *
 * THIS FILE HAS A PYTHON TWIN: src/carpaccio/netcalc.py serves the same
 * free-address answer over the API. tests/test_netcalc.py runs BOTH over the
 * same scenarios and compares JSON, so they cannot drift silently - change
 * one, change both. The view helpers (sorting, filtering) live here rather
 * than app.js so the same pytest harness can exercise them in node.
 *
 * Plain script + module in one: the browser gets a `netcalc` global from the
 * script tag (loaded BEFORE app.js), node gets module.exports for the tests.
 * No build step, same as everything else here.
 */
"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.netcalc = api;
})(globalThis, function () {
  // Refuse to enumerate anything wider than a /16: nobody hand-reserves
  // addresses in a /8, and an honest refusal beats 16 million chips.
  const MAX_ENUMERABLE = 65536;

  function parseIp(text) {
    if (typeof text !== "string") return null;
    const parts = text.trim().split(".");
    if (parts.length !== 4) return null;
    let value = 0;
    for (const p of parts) {
      if (!/^\d{1,3}$/.test(p)) return null;
      const n = Number(p);
      if (n > 255) return null;
      value = value * 256 + n;
    }
    return value;
  }

  function formatIp(value) {
    return [24, 16, 8, 0].map((s) => (value >>> s) & 255).join(".");
  }

  function collapseRanges(addresses) {
    const runs = [];
    for (const addr of addresses) {
      if (runs.length && addr === runs[runs.length - 1].e + 1) runs[runs.length - 1].e = addr;
      else runs.push({ s: addr, e: addr });
    }
    return runs.map((r) => ({ start: formatIp(r.s), end: formatIp(r.e), size: r.e - r.s + 1 }));
  }

  /* The free-address answer. Mirrors carpaccio.netcalc.free_addresses -
   * see that file's docstring for the stated assumptions (gateway derived
   * from UniFi's gateway/prefix subnet string, network+broadcast unusable,
   * ANY recorded IP is taken whatever its assignment, malformed IPs take
   * nothing, no pool = whole usable range is candidate). */
  function freeAddresses(subnet, dhcp, takenIps) {
    const result = { free_count: 0, free_ranges: [], gateway: null, pool: null, reason: null };
    const [addrStr, prefixStr] = String(subnet || "").split("/");
    const addr = parseIp(addrStr);
    const prefix = /^\d{1,2}$/.test(prefixStr || "") ? Number(prefixStr) : null;
    if (addr === null || prefix === null || prefix > 32) {
      result.reason = "no usable subnet on this VLAN - set one in the details panel";
      return result;
    }
    if (prefix >= 31) {
      result.reason = "a /" + prefix + " has no room for reservations";
      return result;
    }
    if (Math.pow(2, 32 - prefix) > MAX_ENUMERABLE) {
      result.reason = "a /" + prefix + " is too large to enumerate address by address";
      return result;
    }

    // >>> 0 keeps every mask operation in unsigned-32 land; JS bitwise ops
    // otherwise produce signed ints and 192.x addresses go negative.
    const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
    const network = (addr & mask) >>> 0;
    const broadcast = (network | (~mask >>> 0)) >>> 0;
    const gateway = addr !== network ? addr : null;
    result.gateway = gateway !== null ? formatIp(gateway) : null;

    let poolStart = null;
    let poolStop = null;
    if (dhcp && dhcp.enabled) {
      poolStart = parseIp(dhcp.start);
      poolStop = parseIp(dhcp.stop);
      if (poolStart === null || poolStop === null || poolStart > poolStop) {
        poolStart = poolStop = null;
      } else {
        result.pool = { start: formatIp(poolStart), end: formatIp(poolStop) };
      }
    }

    const taken = new Set();
    for (const t of takenIps) {
      const ip = parseIp(t);
      if (ip !== null) taken.add(ip);
    }

    const free = [];
    for (let a = network + 1; a < broadcast; a++) {
      if (poolStart !== null && a >= poolStart && a <= poolStop) continue;
      if (a === gateway || taken.has(a)) continue;
      free.push(a);
    }
    result.free_count = free.length;
    result.free_ranges = collapseRanges(free);
    return result;
  }

  /* Randomised = locally-administered bit of the first octet. Computed from
   * the address, never a vendor list. Single source - app.js uses this one. */
  function macIsRandomised(mac) {
    const first = parseInt(String(mac).split(":")[0], 16);
    return Number.isFinite(first) && (first & 2) !== 0;
  }

  /* ---- sorting -----------------------------------------------------------
   * IP order is NUMERIC (the packed 32-bit value): .9 before .10, .99 before
   * .100. Rows with an empty or malformed value sort LAST IN BOTH DIRECTIONS
   * - a half-typed row is noise, and reversing the sort must not promote it
   * to the top as if it were 0.0.0.0. That is why this is a comparator
   * rather than a key: "last either way" cannot be expressed as a key. */
  function compareDevices(a, b, key, dir) {
    let va;
    let vb;
    let missingA;
    let missingB;
    if (key === "ip") {
      va = parseIp(a.ip);
      vb = parseIp(b.ip);
      missingA = va === null;
      missingB = vb === null;
    } else if (key === "rand") {
      va = macIsRandomised(a.mac) ? 1 : 0;
      vb = macIsRandomised(b.mac) ? 1 : 0;
      missingA = missingB = false;
    } else if (key === "wired" || key === "active") {
      va = a[key] ? 1 : 0;
      vb = b[key] ? 1 : 0;
      missingA = missingB = false;
    } else {
      va = String(a[key] || "").toLowerCase();
      vb = String(b[key] || "").toLowerCase();
      missingA = va === "";
      missingB = vb === "";
    }
    if (missingA && missingB) return 0;
    if (missingA) return 1; // last, regardless of dir - deliberately not *dir
    if (missingB) return -1;
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  }

  /* ---- filtering ---------------------------------------------------------
   * One text box across the free-form fields; the fixed-value fields get
   * dropdowns (handled by matchesFilters below). All conditions AND. */

  /* An IP-looking query matches on OCTET BOUNDARIES: "192.168.1.2" matches
   * 192.168.1.2 and 192.168.1.2 only - NOT 192.168.1.25, which a naive
   * startsWith would also catch and which is exactly the wrong row to show
   * someone checking whether .2 is taken. A query ending in "." is an
   * explicit prefix ("192.168.1." = the whole /24). */
  function ipQueryMatches(ip, query) {
    if (!ip) return false;
    if (query.endsWith(".")) return ip.startsWith(query);
    if (ip === query) return true;
    return ip.startsWith(query) && ip.charAt(query.length) === ".";
  }

  function matchesQuery(device, query) {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return true;
    if (/^[\d.]+$/.test(q)) return ipQueryMatches(String(device.ip || ""), q);
    return ["name", "hostname", "mac"].some((f) =>
      String(device[f] || "").toLowerCase().includes(q));
  }

  /* An ENTIRELY empty device row: no name, no IP, no MAC, no hostname, no
   * notes. That is not data, it is a misclick - usually a stray "+ Add
   * device". The UI marks such rows and offers one explicit click to remove
   * them all; it never strips them silently on Save, because silently
   * deleting a user's rows - even useless ones - is worse than leaving
   * them. Fields with defaults (assignment, wired, ...) say nothing about
   * intent, so they do not count. */
  function deviceIsEmpty(d) {
    return ["name", "ip", "mac", "hostname", "notes"].every(
      (f) => String(d[f] || "").trim() === "");
  }

  /* filters: {q, assignment, rand, link, active} - "" means "any". */
  function matchesFilters(device, filters) {
    if (!matchesQuery(device, filters.q)) return false;
    if (filters.assignment && device.assignment !== filters.assignment) return false;
    if (filters.rand === "yes" && !macIsRandomised(device.mac)) return false;
    if (filters.rand === "no" && macIsRandomised(device.mac)) return false;
    if (filters.link === "wired" && !device.wired) return false;
    if (filters.link === "wifi" && device.wired) return false;
    if (filters.active === "yes" && !device.active) return false;
    if (filters.active === "no" && device.active) return false;
    return true;
  }

  return {
    deviceIsEmpty,
    parseIp,
    formatIp,
    collapseRanges,
    freeAddresses,
    macIsRandomised,
    compareDevices,
    ipQueryMatches,
    matchesQuery,
    matchesFilters,
  };
});
