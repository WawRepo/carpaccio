/* cARPaccio sheet UI. Plain JS on purpose: no build step, no framework.
 * The document is a single JS object; inputs mutate it in place and Save
 * PUTs the whole thing with the version token for the optimistic lock. */
"use strict";

let doc = JSON.parse(document.getElementById("document-data").textContent);
let version = window.CARPACCIO_VERSION;
// The active tab lives in the URL hash so a tab can be linked or reloaded.
let activeTab = decodeURIComponent(window.location.hash.slice(1)) || null;

const $ = (sel) => document.querySelector(sel);

/* Address arithmetic, sorting and filter matching live in netcalc.js (loaded
 * before this file, and mirrored in Python for the API + tests). */
const macIsRandomised = netcalc.macIsRandomised;

/* VIEW STATE - and only view state. Sort order, filters and which free-range
 * chips are expanded NEVER touch `doc`: reordering doc.devices on a sort
 * click would make Save write a new version and fill the History tab with
 * "87 devices edited" entries that mean nothing. The stored document's order
 * is whatever the importer/API produced; this object decides what the table
 * SHOWS. Deliberately not persisted anywhere either.
 */
const view = {
  sort: { key: "ip", dir: 1 }, // dir 1 = ascending, -1 = descending
  filters: { q: "", assignment: "", rand: "", link: "", active: "" },
  expanded: new Set(), // free-address ranges opened into individual chips
};

function clearFilters() {
  view.filters = { q: "", assignment: "", rand: "", link: "", active: "" };
}

/* TARGETED REFRESH HOOKS - the fix for "every delete scrolls me to the top".
 *
 * The device table scrolls inside .table-wrap (its own overflow container),
 * so a full render() replaces that container and its scrollTop silently
 * resets to 0. Deleting five rows meant five jumps to the top of a 66-row
 * table. The cure is the same one the filter box already uses: add/delete
 * handlers rebuild ONLY the affected tbody (and the small dependent bits -
 * nav counts, the free panel, the firewall matrix) inside the SAME nodes,
 * so the browser has no reason to move the scroll position.
 *
 * Each tab's render registers its refreshers here; render() wipes them.
 * A handler that fires when its tab is not mounted (cannot happen today)
 * falls back to render(). */
let hooks = {};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "checked") node.checked = v;
    else if (k === "value") node.value = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

function textCell(obj, field, { mono = false, placeholder = "" } = {}) {
  const input = el("input", {
    type: "text",
    value: obj[field] ?? "",
    placeholder,
    oninput: (e) => { obj[field] = e.target.value; },
  });
  return el("td", { class: mono ? "mono" : "" }, input);
}

function selectCell(obj, field, options, { onchange } = {}) {
  const sel = el("select", {
    onchange: (e) => { obj[field] = e.target.value; if (onchange) onchange(); },
  });
  for (const o of options) {
    sel.append(el("option", { value: o, ...(obj[field] === o ? { selected: "" } : {}) }, o));
  }
  return el("td", {}, sel);
}

function setStatus(text, cls = "") {
  const s = $("#status");
  s.textContent = text;
  s.className = "status " + cls;
}

function showBanner(nodes) {
  const b = $("#banner");
  b.replaceChildren(...nodes);
  b.classList.remove("hidden");
}
function hideBanner() { $("#banner").classList.add("hidden"); }

/* ---------- tabs ---------- */

function tabList() {
  const tabs = doc.vlans.map((v) => ({
    id: "vlan:" + v.name,
    label: v.name,
    count: doc.devices.filter((d) => d.vlan === v.name).length,
    render: () => renderVlan(v),
  }));
  // Straight after the VLAN tabs, on the far side of the separator: it is a
  // VIEW of the VLANs, not one of them, so it must not sit inside the group
  // the "vlans" caption labels. renderNav()'s splice points are keyed to
  // doc.vlans.length and are unaffected by anything pushed after this line.
  tabs.push({
    id: "all",
    label: "All",
    // Devices AND reserved addresses: this badge sits next to a table that
    // prints "5 rows", and a 3 beside a 5 is a bug report waiting to happen.
    count: doc.devices.length + (doc.reserved || []).length,
    render: renderAll,
  });
  tabs.push({ id: "dhcp", label: "DHCP ranges", render: renderDhcp });
  tabs.push({ id: "firewall", label: "Firewall", count: doc.firewall_rules.length, render: renderFirewall });
  tabs.push({ id: "wans", label: "WANs & infra", render: renderWans });
  tabs.push({ id: "history", label: "History", render: renderHistory });
  return tabs;
}

/* The tab bar alone. Separate from render() so add/delete handlers can
 * refresh the counts in it without rebuilding the page (nav holds no scroll
 * position and no focus, so rebuilding it is always safe). */
function renderNav(tabs) {
  const nav = $("#tabs");
  const tabEls = tabs.map((t) =>
    el(
      "div",
      {
        class: "tab" + (t.id === activeTab ? " active" : ""),
        onclick: () => { activeTab = t.id; render(); },
      },
      t.label,
      t.count !== undefined ? el("span", { class: "count" }, String(t.count)) : null,
    ),
  );
  // The "+ VLAN" sits right after the VLAN tabs, because that is what it
  // adds - and it SAYS what it adds; a bare "+" next to unlabelled names
  // explained nothing.
  tabEls.splice(doc.vlans.length, 0, el("div", {
    class: "tab add-vlan",
    title: "Add a VLAN",
    onclick: () => addVlan(),
  }, "+ VLAN"));
  // A quiet caption before the VLAN group: the bar read "home 66 | iot 10 |
  // guest 13" with nothing saying what those names were. The caption is not
  // a tab and must not grow into a second header - small, muted, no border.
  // The other tabs are self-describing; a thin separator marks the boundary.
  tabEls.unshift(el("span", { class: "tab-caption" }, "vlans"));
  tabEls.splice(doc.vlans.length + 2, 0, el("span", { class: "tab-sep" }));
  nav.replaceChildren(...tabEls);
}

function render() {
  hooks = {}; // whatever was mounted is about to stop existing
  const tabs = tabList();
  if (!tabs.some((t) => t.id === activeTab)) activeTab = tabs.length ? tabs[0].id : null;
  renderNav(tabs);
  const main = $("#content");
  main.replaceChildren();
  if (activeTab) window.history.replaceState(null, "", "#" + encodeURIComponent(activeTab));
  const tab = tabs.find((t) => t.id === activeTab);
  if (tab) tab.render();
  else main.append(
    el("div", { class: "empty" },
      "Empty document - run carpaccio-import, or ",
      el("button", { class: "small", onclick: () => addVlan() }, "+ Add VLAN")));
}

/* ---------- VLAN field helpers (shared by the VLAN tab and DHCP ranges) ----

   A VLAN's NAME is special: unifi rows are merged BY NAME on re-import, so
   renaming one would not match the next import and would DUPLICATE the
   segment instead of renaming it. Manual rows are never matched against an
   import and rename freely - and the rename must chase every device that
   references the VLAN by name, or they fall off every tab. */

function isManualVlan(v) { return v.source === "manual"; }

function vlanNameControl(v) {
  if (!isManualVlan(v)) {
    return el("span", {
      title: "from UniFi: this name is how the VLAN is matched on re-import, "
           + "so it cannot be edited here. Rename it on the controller.",
    }, v.name, " ", el("span", { class: "pill stable" }, "unifi"));
  }
  return el("input", {
    type: "text", value: v.name,
    onchange: (e) => {
      const newName = e.target.value.trim();
      if (!newName || (newName !== v.name
          && doc.vlans.some((x) => x !== v && x.name === newName))) {
        e.target.value = v.name; // empty or duplicate names would corrupt the sheet
        return;
      }
      if (newName === v.name) return;
      for (const d of doc.devices) if (d.vlan === v.name) d.vlan = newName;
      if (activeTab === "vlan:" + v.name) activeTab = "vlan:" + newName;
      v.name = newName;
      render();
    },
  });
}

function vlanIdInput(v) {
  return el("input", {
    type: "text", class: "narrow", value: v.vlan_id ?? "", placeholder: "untagged",
    onchange: (e) => {
      const n = parseInt(e.target.value, 10);
      v.vlan_id = Number.isFinite(n) ? n : null; // empty / junk = the untagged LAN
      e.target.value = v.vlan_id ?? "";
    },
  });
}

function subnetInput(v) {
  return el("input", {
    type: "text", value: v.subnet || "", placeholder: "192.168.9.0/24",
    oninput: (e) => { v.subnet = e.target.value.trim(); },
  });
}

function ensureDhcp(v) {
  v.dhcp = v.dhcp || { enabled: false, start: "", stop: "", lease_seconds: null, dns: [] };
  return v.dhcp;
}

function leaseInput(v) {
  const dhcp = ensureDhcp(v);
  return el("input", {
    type: "text", class: "narrow", value: dhcp.lease_seconds ?? "", placeholder: "86400",
    onchange: (e) => {
      const n = parseInt(e.target.value, 10);
      dhcp.lease_seconds = Number.isFinite(n) ? n : null;
      e.target.value = dhcp.lease_seconds ?? "";
    },
  });
}

function dnsInput(v) {
  const dhcp = ensureDhcp(v);
  return el("input", {
    type: "text", value: (dhcp.dns || []).join(", "), placeholder: "comma-separated",
    onchange: (e) => {
      dhcp.dns = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
      e.target.value = dhcp.dns.join(", ");
    },
  });
}

function addVlan(opts) {
  let n = 1;
  let name = "new-vlan";
  while (doc.vlans.some((v) => v.name === name)) { n += 1; name = "new-vlan-" + n; }
  // subnet starts EMPTY (the schema allows it): a segment being planned has
  // no addressing yet, and the add button must never create a document that
  // cannot be saved.
  const vlan = {
    name, vlan_id: null, subnet: "", isolated: false,
    dhcp: { enabled: false, start: "", stop: "", lease_seconds: null, dns: [] },
    notes: "", source: "manual",
  };
  doc.vlans.push(vlan);
  if (opts && opts.stay && hooks.dhcp) {
    // From the DHCP ranges tab: appending a row there and jumping to a
    // different tab would BE the scroll-loss bug in another coat. Stay,
    // refresh the table in place, put the cursor in the new row's name.
    hooks.dhcp.refresh();
    renderNav(tabList());
    hooks.dhcp.focusVlan(vlan);
    return;
  }
  // From the "+ VLAN" tab button: creating a tab and going to it IS the
  // point, so a full render (a navigation) is correct here.
  activeTab = "vlan:" + name;
  render();
}

function deleteVlanGuarded(v) {
  const still = doc.devices.filter((d) => d.vlan === v.name).length;
  if (still > 0) {
    // Refuse, loudly. Deleting the VLAN would orphan these rows off every
    // tab - they would still exist in the document but be visible nowhere.
    showBanner([
      el("strong", {}, `Not deleted - ${still} device${still === 1 ? "" : "s"} still on `
        + `"${v.name}".`),
      el("div", {}, "Move them to another VLAN (or delete them) first; "
        + "nothing is ever dropped silently."),
    ]);
    return;
  }
  if (!confirm(`Delete VLAN "${v.name}"? (It has no devices.)`)) return;
  doc.vlans.splice(doc.vlans.indexOf(v), 1);
  hideBanner();
  if (activeTab === "dhcp" && hooks.dhcp) {
    // Deleting a row from the DHCP table stays on the DHCP table, scroll
    // untouched - only the row and the tab-bar entry disappear.
    hooks.dhcp.refresh();
    renderNav(tabList());
    return;
  }
  // Deleting the VLAN whose own tab is open: that tab stops existing, so
  // moving away is not a bug, it is the only honest outcome.
  activeTab = null;
  render();
}

/* ---------- per-VLAN device sheet ---------- */

const ASSIGNMENTS = ["reservation", "static", "dhcp"];

function isolationText(vlan) {
  return vlan.isolated
    ? " ON — cannot reach other local networks"
    : " off — can reach other local networks";
}

function renderVlan(vlan) {
  const main = $("#content");
  const dhcp = ensureDhcp(vlan);

  // One detail per LINE, labels and values in two aligned columns. The old
  // single-line bar packed six facts into one string of prose and the eye
  // had to parse it every time; a column of labels reads at a glance. It is
  // also where the VLAN itself is edited now - grid layout in .vlan-details.
  const detail = (label, ...value) => [
    el("span", { class: "k" }, label),
    el("span", { class: "v" }, ...value),
  ];
  const detailsPanel = el(
      "div",
      { class: "vlan-details" },
      ...detail("name", vlanNameControl(vlan)),
      ...detail("802.1q", vlanIdInput(vlan)),
      ...detail("subnet", subnetInput(vlan)),
      ...detail("dhcp",
        el("label", { class: "inline" },
          el("input", {
            type: "checkbox", checked: !!dhcp.enabled,
            onchange: (e) => { dhcp.enabled = e.target.checked; },
          }),
          " enabled"),
        el("span", { class: "range" },
          rangeInput(dhcp, "start"), " – ", rangeInput(dhcp, "stop"))),
      ...detail("lease", leaseInput(vlan), el("span", { class: "unit" }, "seconds")),
      ...detail("dns", dnsInput(vlan)),
      // Say it on the VLAN's own tab too, not only in the firewall matrix.
      // Whether this network can talk to the others is the first thing anyone
      // asks about a segment, and it is invisible everywhere else in UniFi.
      ...detail("isolation", (() => {
        // The label text swaps in place; a render() for one string would
        // reset the device table's scroll below.
        const text = el("span", {}, isolationText(vlan));
        return el("label", { class: "inline" },
          el("input", {
            type: "checkbox", checked: !!vlan.isolated,
            onchange: (e) => {
              vlan.isolated = e.target.checked;
              text.textContent = isolationText(vlan);
            },
          }),
          text);
      })()),
      ...detail("notes", notesInput(vlan)),
      ...detail("", el("button", {
        class: "small danger",
        onclick: () => deleteVlanGuarded(vlan),
      }, "Delete VLAN")),
  );
  let freeBox = freePanel(vlan);
  main.append(el("div", { class: "vlan-top" }, detailsPanel, freeBox));

  // ---- the device sheet: filters and sort are VIEW state, never doc ------
  const tbody = el("tbody");
  const countEl = el("span", { class: "hint count-hint" });
  const emptyBtn = el("button", { class: "small danger" });
  let lastRows = [];
  const refreshRows = () => {
    const all = doc.devices.filter((d) => d.vlan === vlan.name);
    lastRows = all
      .filter((d) => netcalc.matchesFilters(d, view.filters))
      // sort() on a COPY (filter() already copied): doc.devices keeps the
      // stored order, or Save would write a meaningless new version.
      .sort((a, b) => netcalc.compareDevices(a, b, view.sort.key, view.sort.dir));
    tbody.replaceChildren(...lastRows.map(deviceRow));
    countEl.textContent = lastRows.length === all.length
      ? `${all.length} devices`
      : `${lastRows.length} of ${all.length} devices match`;
    updateEmptyControl();
  };

  const emptyOnes = () =>
    doc.devices.filter((d) => d.vlan === vlan.name && netcalc.deviceIsEmpty(d));
  const updateEmptyControl = () => {
    const n = emptyOnes().length;
    emptyBtn.hidden = n === 0; // only ever visible when there is work to do
    emptyBtn.textContent = `remove ${n} empty row${n === 1 ? "" : "s"}`;
  };
  emptyBtn.addEventListener("click", () => {
    // Explicit, counted, one click - and NEVER done silently on Save:
    // deleting a user's rows behind their back, even useless ones, is worse
    // than leaving them.
    for (const d of emptyOnes()) doc.devices.splice(doc.devices.indexOf(d), 1);
    devicesMutated();
    setStatus("empty rows removed", "ok");
  });

  // Everything that must follow a device add/delete, WITHOUT a render():
  // rows, the free panel (the address supply changed), the tab-bar count.
  // The .table-wrap node is untouched, so its scrollTop stays where the
  // user put it - that is the whole fix.
  const devicesMutated = () => {
    refreshRows();
    const fresh = freePanel(vlan);
    freeBox.replaceWith(fresh);
    freeBox = fresh;
    renderNav(tabList());
  };

  const focusDevice = (d) => {
    const tr = tbody.children[lastRows.indexOf(d)];
    if (!tr) return;
    const input = tr.querySelector("input");
    if (input) input.focus();
    // The one case where moving IS correct: a new row off-screen scrolls
    // into view - the row, never the top. "nearest" keeps an already
    // visible row exactly where it is.
    tr.scrollIntoView({ block: "nearest" });
  };

  const addDevice = (preset) => {
    const d = {
      mac: "", name: "", hostname: "", ip: "", vlan: vlan.name,
      // "static" is the default for hand-added rows: the whole reason to
      // add one by hand is that no API will ever report it.
      assignment: "static", wired: true, active: false, oui: "",
      last_seen: null, notes: "", source: "manual",
      ...preset,
    };
    doc.devices.push(d);
    if (!netcalc.matchesFilters(d, view.filters)) {
      // A row born hidden by the active filter would look like a silent
      // failure. Clearing the filter is a visible, stated side effect -
      // never a silent one.
      clearFilters();
      if (hooks.filterReset) hooks.filterReset();
      setStatus("filters cleared to show the new row", "ok");
    }
    devicesMutated();
    focusDevice(d);
    return d;
  };

  hooks.devices = { refreshRows, devicesMutated, focusDevice, addDevice };
  refreshRows();

  // Typing into any row can change its emptiness (and, via the IP column,
  // the free-address list). Input events bubble; one listener per table.
  tbody.addEventListener("input", (e) => {
    const tr = e.target.closest("tr");
    const idx = Array.prototype.indexOf.call(tbody.children, tr);
    const d = lastRows[idx];
    if (d) tr.classList.toggle("row-empty", netcalc.deviceIsEmpty(d));
    updateEmptyControl();
    // Typing an IP changes the free-address supply; the panel is replaced
    // in place, which cannot steal focus from the cell being typed in.
    const td = e.target.closest("td");
    if (td && td.cellIndex === 1) {
      const fresh = freePanel(vlan);
      freeBox.replaceWith(fresh);
      freeBox = fresh;
    }
  });

  // Column headers sort on click; the arrow shows which one, which way.
  // A sort click may full-render (nothing is mid-edit in a header), but the
  // FILTER inputs above only call refreshRows(): a full render on every
  // keystroke would steal focus from the box being typed in - the same bug
  // class as the MAC-pill repaint, solved the same way.
  const SORTABLE = [
    ["Description", "name"], ["IP", "ip"], ["Assignment", "assignment"],
    ["MAC", "mac"], ["Random MAC?", "rand"], ["Hostname", "hostname"],
    ["Link", "wired"], ["Active", "active"], ["Notes", null], ["", null],
  ];
  const headRow = el("tr", {}, ...SORTABLE.map(([label, key]) => {
    if (!key) return el("th", {}, label);
    const active = view.sort.key === key;
    return el("th", {
      class: "sortable" + (active ? " sorted" : ""),
      title: "click to sort" + (active ? ", again to reverse" : ""),
      onclick: () => {
        if (view.sort.key === key) view.sort.dir = -view.sort.dir;
        else view.sort = { key, dir: 1 };
        render();
      },
    }, label + (active ? (view.sort.dir === 1 ? " ▲" : " ▼") : ""));
  }));

  main.append(
    filterBar(refreshRows, countEl),
    el("div", { class: "table-wrap" },
      el("table", { class: "devices" },
        el("thead", {}, headRow),
        tbody)),
    el("div", { class: "toolbar" },
      el("button", { onclick: () => addDevice({}) }, "+ Add device"),
      emptyBtn,
      el("span", { class: "hint" },
        "static = configured on the host itself; the controller cannot see it, so write it down here.")),
  );
}

/* ---------- "All" - every VLAN in one table -------------------------------

   A VLAN tab answers "what is on this segment". This one answers "WHERE IS
   THIS ADDRESS", which is the question actually asked: somebody has an IP in
   their hand and does not yet know which segment owns it. Same rows, same
   editing, one extra VLAN column, one filter box over the lot.

   RESERVED ADDRESSES ARE IN THIS TABLE TOO, read-only. They are the addresses
   no device owns - the k3s API VIP, the MetalLB pool - and they are EDITED on
   the DHCP tab, which is the right home for them. But somebody searching for
   .230 is not thinking "that will be filed under DHCP ranges"; they type the
   number and expect the sheet to say what holds it. Leaving them out of the
   one table called "All" is exactly how .230 read as missing from the whole
   document (owner, 2026-09-24) when it had been recorded all along.

   No "+ Add device" here on purpose: a device belongs to a VLAN, and this
   table spans all of them, so there is no honest answer to which one a new
   row would join. Add it on the VLAN's own tab. */

function reservedAsRow(r) {
  // A device-SHAPED view of a reserved address, so one sort and one filter
  // can run over both kinds without either growing a special case.
  return {
    __reserved: r,
    name: r.name || "",
    ip: r.ip || "",
    hostname: "",
    mac: "",
    vlan: "",
    assignment: "reserved",
    wired: true,
    active: false,
    notes: r.notes || "",
    last_seen: null,
  };
}

/* Does a numeric query hit this reserved entry? A RANGE has to answer for
 * every address inside it - searching "242" must find the MetalLB pool
 * written as .240-.244, or the pool is as invisible as the VIP was. The
 * expansion (and its 4096 cap against a fat-fingered ip_end) is netcalc's,
 * so this asks the same question the free-address answer does. */
function reservedIpMatches(r, q) {
  return netcalc.reservedIps([r]).some((ip) => netcalc.ipQueryMatches(ip, q));
}

function reservedMatchesFilters(r) {
  // The dropdowns all describe a DEVICE - how it is assigned, whether its MAC
  // is randomised, wired or wifi, up or down. A reserved address has none of
  // those properties, so any dropdown set HIDES these rows rather than
  // inventing an answer for them.
  const f = view.filters;
  if (f.assignment || f.rand || f.link || f.active) return false;
  const q = String(f.q || "").trim().toLowerCase();
  if (!q) return true;
  if (/^[\d.]+$/.test(q)) return reservedIpMatches(r, q);
  return [r.name, r.notes].some((x) => String(x || "").toLowerCase().includes(q));
}

function allReservedRow(r) {
  const tr = el("tr", { class: "reserved-row" });
  const span = r.ip_end ? `${r.ip} – ${r.ip_end}` : (r.ip || "");
  tr.append(el("td", { class: "vlan-col" }, "—"));
  tr.append(el("td", {}, r.name || el("span", { class: "hint" }, "unnamed")));
  tr.append(el("td", { class: "mono" }, span));
  tr.append(el("td", {}, el("span", {
    class: "pill reserved",
    title: "Not a device: an address no machine owns, so no API will ever "
      + "report it. Excluded from every free-address answer. Edit it on the "
      + "DHCP ranges tab.",
  }, "reserved")));
  // MAC, randomised?, hostname, link, active - none of them mean anything
  // for an address with no machine behind it. Blank, not "unknown".
  for (let i = 0; i < 5; i += 1) tr.append(el("td", {}, ""));
  tr.append(el("td", {}, r.notes || ""));
  tr.append(el("td", {}, el("span", { class: "hint" }, "DHCP tab")));
  return tr;
}

function renderAll() {
  const main = $("#content");
  const tbody = el("tbody");
  const countEl = el("span", { class: "hint count-hint" });

  const refreshRows = () => {
    const devices = doc.devices.filter((d) => netcalc.matchesFilters(d, view.filters));
    const reserved = (doc.reserved || []).filter(reservedMatchesFilters);
    const rows = devices
      .concat(reserved.map(reservedAsRow))
      .sort((a, b) => netcalc.compareDevices(a, b, view.sort.key, view.sort.dir));
    tbody.replaceChildren(...rows.map((row) => {
      if (row.__reserved) return allReservedRow(row.__reserved);
      // deviceRow() is the VLAN tabs' row, unchanged - the VLAN column is
      // pushed on the front here rather than threaded through it, so the
      // two tables cannot drift apart.
      const tr = deviceRow(row);
      tr.insertBefore(el("td", { class: "vlan-col" }, row.vlan || ""), tr.firstChild);
      return tr;
    }));
    const total = doc.devices.length + (doc.reserved || []).length;
    countEl.textContent = rows.length === total
      ? `${total} rows`
      : `${rows.length} of ${total} rows match`;
  };

  // A delete from any row here must refresh THIS table, not a VLAN's.
  hooks.devices = {
    refreshRows,
    devicesMutated: () => { refreshRows(); renderNav(tabList()); },
    focusDevice: () => {},
    addDevice: () => {},
  };
  refreshRows();

  const SORTABLE = [
    ["VLAN", "vlan"], ["Description", "name"], ["IP", "ip"],
    ["Assignment", "assignment"], ["MAC", "mac"], ["Random MAC?", "rand"],
    ["Hostname", "hostname"], ["Link", "wired"], ["Active", "active"],
    ["Notes", null], ["", null],
  ];
  const headRow = el("tr", {}, ...SORTABLE.map(([label, key]) => {
    if (!key) return el("th", {}, label);
    const active = view.sort.key === key;
    return el("th", {
      class: "sortable" + (active ? " sorted" : ""),
      title: "click to sort" + (active ? ", again to reverse" : ""),
      onclick: () => {
        if (view.sort.key === key) view.sort.dir = -view.sort.dir;
        else view.sort = { key, dir: 1 };
        render();
      },
    }, label + (active ? (view.sort.dir === 1 ? " ▲" : " ▼") : ""));
  }));

  main.append(
    filterBar(refreshRows, countEl),
    el("div", { class: "table-wrap" },
      el("table", { class: "devices" },
        el("thead", {}, headRow),
        tbody)),
    el("div", { class: "toolbar" },
      el("span", { class: "hint" },
        "Every VLAN in one table, plus the reserved addresses no device owns. "
        + "Add a device on its own VLAN tab; edit a reserved address on the DHCP ranges tab.")),
  );
}

/* ---------- "which address can I actually hand out?" ----------------------
   The question this whole batch serves. Free = usable in the subnet, OUTSIDE
   the DHCP pool, not the gateway, not recorded on any device (any assignment
   - a dhcp lease outside the pool is a stale lease, not an invitation) or
   infrastructure. Computed live from `doc` by netcalc.js so the answer
   tracks unsaved edits; the API serves the same answer for agents from
   netcalc.py, and a test keeps the twins equal. */
function freePanel(vlan) {
  // Reserved addresses are TAKEN, exactly as the API counts them. Leaving
  // them out here meant the panel a person actually clicks offered the k3s
  // API VIP as "free to reserve" while the same document's API refused it.
  const taken = doc.devices.map((d) => d.ip || "")
    .concat(doc.infrastructure.map((i) => i.ip || ""))
    .concat(netcalc.reservedIps(doc.reserved));
  const ans = netcalc.freeAddresses(vlan.subnet, vlan.dhcp, taken);
  const box = el("div", { class: "free-panel" });
  box.append(el("div", { class: "free-title" }, "Free to reserve"));
  if (ans.reason) {
    box.append(el("div", { class: "hint" }, ans.reason));
    return box;
  }
  box.append(el("div", { class: "hint" },
    `${ans.free_count} address${ans.free_count === 1 ? "" : "es"} outside the DHCP pool`
    + `${ans.pool ? ` (${ans.pool.start} – ${ans.pool.end})` : ""} and taken by nothing `
    + "in this document. Click one to write it down as a reservation."));
  const chips = el("div", { class: "chips" });
  for (const r of ans.free_ranges) {
    // A run of one is an address; click it. A longer run shows collapsed
    // (60 chips nobody asked for are noise) and expands on click. The
    // expansion key is the run's first address, so once an address inside
    // it is taken the runs re-split and the expansion collapses - fine,
    // the list just changed.
    if (r.size === 1) {
      chips.append(addressChip(vlan, r.start));
    } else if (view.expanded.has(vlan.name + "|" + r.start)) {
      const startInt = netcalc.parseIp(r.start);
      for (let i = 0; i < r.size; i++) {
        chips.append(addressChip(vlan, netcalc.formatIp(startInt + i)));
      }
    } else {
      chips.append(el("button", {
        class: "chip range",
        title: r.size + " free addresses - click to expand",
        onclick: () => {
          view.expanded.add(vlan.name + "|" + r.start);
          if (hooks.devices) hooks.devices.devicesMutated();
          else render();
        },
      }, `${r.start} – ${r.end}`, el("span", { class: "chip-count" }, String(r.size))));
    }
  }
  box.append(chips);
  return box;
}

function addressChip(vlan, ip) {
  // The shortest path from "I see a free address" to "it is written down":
  // one click creates the reservation row, pre-filled, sorted into place.
  // The chip disappears on the re-render because the address is now taken.
  return el("button", {
    class: "chip",
    title: "add a reservation row with this address",
    onclick: () => {
      if (hooks.devices) {
        hooks.devices.addDevice({ ip, assignment: "reservation" });
      }
      setStatus("added " + ip + " — fill in MAC and description, then Save", "ok");
    },
  }, ip);
}

function filterBar(refreshRows, countEl) {
  const filterSelect = (field, options) => {
    const sel = el("select", {
      onchange: (e) => { view.filters[field] = e.target.value; refreshRows(); },
    });
    for (const [value, label] of options) {
      sel.append(el("option",
        { value, ...(view.filters[field] === value ? { selected: "" } : {}) }, label));
    }
    return sel;
  };
  const bar = el("div", { class: "filter-bar" },
    el("input", {
      type: "text", class: "filter-q", value: view.filters.q,
      placeholder: "filter: name / hostname / MAC, or an IP — 230 finds .230, 192.168.1. the whole /24",
      // refreshRows() only - a full render() here would rebuild this very
      // input and steal focus on every keystroke.
      oninput: (e) => { view.filters.q = e.target.value; refreshRows(); },
    }),
    filterSelect("assignment", [["", "assignment: any"], ["reservation", "reservation"],
      ["static", "static"], ["dhcp", "dhcp"]]),
    filterSelect("rand", [["", "MAC: any"], ["yes", "randomised"], ["no", "burned-in"]]),
    filterSelect("link", [["", "link: any"], ["wired", "wired"], ["wifi", "wifi"]]),
    filterSelect("active", [["", "active: any"], ["yes", "up"], ["no", "down"]]),
    el("button", {
      class: "small",
      onclick: () => {
        clearFilters();
        if (hooks.filterReset) hooks.filterReset();
        refreshRows(); // tbody only - the wrap keeps its scroll position
      },
    }, "clear filters"),
    countEl,
  );
  // Lets addDevice() blank these boxes when it clears filters so a new row
  // is not born hidden - without rebuilding the bar (focus, scroll).
  hooks.filterReset = () => {
    for (const input of bar.querySelectorAll("input")) input.value = "";
    for (const sel of bar.querySelectorAll("select")) sel.selectedIndex = 0;
  };
  return bar;
}

function rangeInput(dhcp, field) {
  return el("input", {
    type: "text", class: "mono range-bound", value: dhcp[field] ?? "", placeholder: "0.0.0.0",
    oninput: (e) => { dhcp[field] = e.target.value.trim(); },
  });
}

function notesInput(obj) {
  return el("input", {
    type: "text", class: "wide", value: obj.notes ?? "",
    oninput: (e) => { obj.notes = e.target.value; },
  });
}

/* Up, or WHEN it was last seen. The date and the up/down pill are both a
   snapshot from the last import, and the hover says so: a device that came
   back after that import still reads as offline until the next one. */
function activeCell(d) {
  if (d.active) return el("td", {}, el("span", { class: "pill up" }, "up"));
  const seen = netcalc.lastSeenLabel(d.last_seen, Date.now());
  if (!seen) return el("td", {}, el("span", { class: "pill down" }, "—"));
  return el("td", {
    class: "last-seen",
    title: `offline - the controller last saw it ${seen.full} (as of the last import)`,
  }, el("div", { class: "mono" }, seen.date), el("div", { class: "ago" }, seen.ago));
}

function deviceRow(d) {
  const tr = el("tr", {
    class: (d.active ? "" : "inactive")
      + (netcalc.deviceIsEmpty(d) ? " row-empty" : ""),
  });
  tr.append(textCell(d, "name", { placeholder: "describe it" }));
  tr.append(textCell(d, "ip", { mono: true, placeholder: "0.0.0.0" }));
  tr.append(selectCell(d, "assignment", ASSIGNMENTS));
  const macTd = textCell(d, "mac", { mono: true, placeholder: "aa:bb:cc:dd:ee:ff" });
  tr.append(macTd);
  const randTd = el("td", {});
  const paintRand = () => randTd.replaceChildren(
    macIsRandomised(d.mac)
      ? el("span", { class: "pill rand", title: "Locally-administered bit set: this address can change when the device rejoins. A DHCP reservation on it will not stick." }, "randomised")
      : el("span", { class: "pill stable" }, "burned-in"));
  paintRand();
  // Repaint just the pill while typing a MAC; a full render() here would
  // steal focus from the input on every keystroke.
  macTd.querySelector("input").addEventListener("input", paintRand);
  tr.append(randTd);
  tr.append(el("td", { class: "mono" }, d.hostname || ""));
  tr.append(el("td", {}, d.wired ? "wired" : "wifi"));
  tr.append(activeCell(d));
  tr.append(textCell(d, "notes"));
  tr.append(el("td", {}, el("button", {
    class: "small danger",
    onclick: () => {
      doc.devices.splice(doc.devices.indexOf(d), 1);
      // Targeted refresh, not render(): the whole reason the page stopped
      // jumping to the top on every delete. See the hooks comment up top.
      if (hooks.devices) hooks.devices.devicesMutated();
      else render();
    },
  }, "✕")));
  return tr;
}

/* ---------- DHCP ranges ---------- */

function renderDhcp() {
  const main = $("#content");
  const tbody = el("tbody");
  const buildRows = () => {
    tbody.replaceChildren();
    for (const v of doc.vlans) tbody.append(dhcpRow(v));
  };
  hooks.dhcp = {
    refresh: buildRows,
    focusVlan: (v) => {
      const tr = tbody.children[doc.vlans.indexOf(v)];
      if (!tr) return;
      const input = tr.querySelector("input");
      if (input) input.focus();
      tr.scrollIntoView({ block: "nearest" });
    },
  };
  buildRows();
  main.append(
    el("div", { class: "table-wrap" },
      el("table", {},
        el("thead", {}, el("tr", {},
          ...["VLAN", "802.1q", "Subnet", "DHCP", "Start", "Stop", "Lease (s)", "DNS", "Free", "Notes", ""]
            .map((h) => el("th", {}, h)))),
        tbody)),
    el("div", { class: "toolbar" },
      el("button", { onclick: () => addVlan({ stay: true }) }, "+ Add VLAN"),
      el("span", { class: "hint" },
        "a UniFi VLAN's name is its merge key and stays read-only; everything else "
        + "is editable. Hand-added VLANs rename freely and may leave the subnet "
        + "empty while the segment is still being planned.")),
    reservedSection(),
  );
}

/* Addresses that are in use but belong to NO device (HOME-445).
 *
 * Shown here, in the view where someone asks "what is free", because that is
 * where the mistake gets made. A kube-vip VIP or a MetalLB pool address is
 * invisible to everything else this tool reads: no controller reports it, and
 * ARP attributes it to whichever node answered last — so it reads as a
 * duplicate of a host already listed, or, when nothing has spoken to it
 * recently, as nothing at all. That silent case is the dangerous one: the
 * Kubernetes API VIP nearly got handed to a new node on 2026-09-24.
 *
 * Hand-maintained on purpose. There is no source to import these from, and
 * saying so plainly is better than implying a sync that cannot exist. */
function reservedSection() {
  const list = doc.reserved || (doc.reserved = []);
  const tbody = el("tbody");

  const buildRows = () => {
    tbody.replaceChildren();
    if (!list.length) {
      tbody.append(el("tr", {},
        el("td", { colspan: 5, class: "hint" },
          "Nothing reserved yet. Add the addresses no device owns — virtual IPs, "
          + "load-balancer pools — or they will be offered as free.")));
      return;
    }
    for (const r of list) tbody.append(reservedRow(r, buildRows));
  };
  buildRows();

  return el("div", {},
    el("h2", {}, "Reserved — addresses no device owns"),
    el("div", { class: "table-wrap" },
      el("table", {},
        el("thead", {}, el("tr", {},
          ...["Address", "Last of range", "What holds it", "Why", ""].map((h) => el("th", {}, h)))),
        tbody)),
    el("div", { class: "toolbar" },
      el("button", {
        onclick: () => {
          list.push({ ip: "", ip_end: "", name: "", notes: "" });
          buildRows();
        },
      }, "+ Add reserved address"),
      el("span", { class: "hint" },
        "These are excluded from every free-address answer. Leave 'Last of range' "
        + "empty for a single address.")),
  );
}

function reservedRow(r, rebuild) {
  const tr = el("tr");
  const field = (key, cls) => el("input", {
    class: cls || "", value: r[key] || "",
    oninput: (e) => { r[key] = e.target.value; },
  });
  tr.append(el("td", { class: "mono" }, field("ip", "mono")));
  tr.append(el("td", { class: "mono" }, field("ip_end", "mono")));
  tr.append(el("td", {}, field("name")));
  tr.append(el("td", {}, field("notes")));
  tr.append(el("td", {}, el("button", {
    onclick: () => {
      const list = doc.reserved;
      list.splice(list.indexOf(r), 1);
      rebuild();
    },
  }, "Delete")));
  return tr;
}

function dhcpRow(v) {
  const dhcp = ensureDhcp(v);
  const tr = el("tr");
  tr.append(el("td", {}, vlanNameControl(v)));
  tr.append(el("td", { class: "mono" }, vlanIdInput(v)));
  tr.append(el("td", { class: "mono" }, subnetInput(v)));
  tr.append(el("td", {}, el("input", {
    type: "checkbox", checked: !!dhcp.enabled,
    onchange: (e) => { dhcp.enabled = e.target.checked; },
  })));
  tr.append(el("td", { class: "mono" }, rangeInput(dhcp, "start")));
  tr.append(el("td", { class: "mono" }, rangeInput(dhcp, "stop")));
  tr.append(el("td", { class: "mono" }, leaseInput(v)));
  tr.append(el("td", { class: "mono" }, dnsInput(v)));
  {
    // The same free-address arithmetic as the VLAN tab's panel, as a count:
    // this tab is where someone compares pools across VLANs.
    const taken = doc.devices.map((d) => d.ip || "")
      .concat(doc.infrastructure.map((i) => i.ip || ""));
    const ans = netcalc.freeAddresses(v.subnet, v.dhcp, taken);
    tr.append(el("td", { class: "mono", title: ans.reason || "outside the DHCP pool and unused" },
      ans.reason ? "—" : String(ans.free_count)));
  }
  tr.append(textCell(v, "notes"));
  tr.append(el("td", {}, el("button", {
    class: "small danger",
    onclick: () => deleteVlanGuarded(v),
  }, "✕")));
  return tr;
}

/* ---------- firewall: matrix first, then the rule sheet ---------- */

function firstMatch(src, dst) {
  for (const r of doc.firewall_rules) {
    if (r.enabled === false) continue; // absent = enabled, matching the schema's optional field
    if ((r.src === src || r.src === "any") && (r.dst === dst || r.dst === "any")) return r;
  }
  return null;
}

function renderFirewall() {
  const main = $("#content");
  const names = doc.vlans.map((v) => v.name);

  main.append(el("h2", {}, "Who can reach whom (first matching enabled rule)"));
  // The matrix and the rule rows refresh independently and in place: a rule
  // edit repaints the matrix, an add/delete rebuilds the tbody - never the
  // page, so neither table's scroll position moves. Same fix as the device
  // sheet; see the hooks comment up top.
  let matrixBox = buildMatrix(names);
  const tbody = el("tbody");
  const buildRules = () => {
    tbody.replaceChildren(...doc.firewall_rules.map((r) => ruleRow(r, names)));
  };
  hooks.firewall = {
    refreshMatrix: () => {
      const fresh = buildMatrix(names);
      matrixBox.replaceWith(fresh);
      matrixBox = fresh;
    },
    refreshRules: buildRules,
    focusRule: (r) => {
      const tr = tbody.children[doc.firewall_rules.indexOf(r)];
      if (!tr) return;
      const input = tr.querySelector("input");
      if (input) input.focus();
      tr.scrollIntoView({ block: "nearest" });
    },
  };
  buildRules();

  main.append(matrixBox);
  main.append(el("div", { class: "matrix-legend" },
    el("b", {}, "isolated"),
    " = network isolation is on for that source network. It cannot reach any "
    + "other local network, and this is enforced by the gateway rather than by a "
    + "rule — so it shows up in no rule list, and a rules-only audit will wrongly "
    + "report the segment as wide open. ",
    el("br", {}),
    "“no rule” means whatever the router defaults to - which is commonly ",
    el("span", { class: "mono" }, "allow"),
    ". Click a cell to add a rule for that pair."));

  main.append(el("h2", {}, "Rules (evaluated top-down)"));
  main.append(
    el("div", { class: "table-wrap" },
      el("table", {},
        el("thead", {}, el("tr", {},
          ...["Name", "Action", "Source", "Destination", "Protocol", "Ports", "On", "Description", ""]
            .map((h) => el("th", {}, h)))),
        tbody)),
    el("div", { class: "toolbar" },
      el("button", { onclick: () => addRule("", "") }, "+ Add rule")),
  );
}

function buildMatrix(names) {
  const matrix = el("table", { class: "matrix" });
  matrix.append(el("tr", {},
    el("th", { class: "axis" }, "src ↓ dst →"),
    ...names.map((n) => el("th", {}, n))));
  for (const src of names) {
    const tr = el("tr", {}, el("th", {}, src));
    for (const dst of names) {
      if (src === dst) { tr.append(el("td", { class: "self" }, "·")); continue; }
      // ISOLATION IS CHECKED FIRST, and it is not a rule. UniFi's per-network
      // "Network Isolation" switch blocks a network from reaching every other
      // local network, and it appears in no rule list at all. Before this, the
      // matrix showed "no rule" for iot → home on a network where iot is in
      // fact isolated: the tool reported real segmentation as missing, which
      // is the worst answer it could give, because it invites someone to
      // "fix" a wall that is already there.
      const srcVlan = doc.vlans.find((v) => v.name === src);
      if (srcVlan && srcVlan.isolated) {
        tr.append(el("td", {
          class: "drop isolated",
          title: `${src} has network isolation on: it cannot reach any `
               + `other local network. Enforced by the gateway, not by a rule, `
               + `so it appears in no rule list.`,
        }, "isolated"));
        continue;
      }
      const rule = firstMatch(src, dst);
      const cls = rule ? rule.action : "none";
      tr.append(el("td", {
        class: cls,
        title: rule ? `${rule.name}` : `no rule: click to add ${src} → ${dst}`,
        onclick: () => { addRule(src, dst); },
      }, rule ? rule.action : "no rule"));
    }
    matrix.append(tr);
  }
  return el("div", { class: "table-wrap", style: "display:inline-block" }, matrix);
}

function addRule(src, dst) {
  const rule = {
    id: "manual-" + Date.now(),
    name: src && dst ? `${src} to ${dst}` : "",
    action: "drop",
    src: src || (doc.vlans[0] ? doc.vlans[0].name : "any"),
    dst: dst || "any",
    protocol: "any",
    ports: "",
    enabled: true,
    description: "",
  };
  doc.firewall_rules.push(rule);
  if (hooks.firewall) {
    hooks.firewall.refreshRules();
    hooks.firewall.refreshMatrix();
    hooks.firewall.focusRule(rule);
    renderNav(tabList()); // the Firewall tab shows a rule count
  } else {
    render();
  }
}

function ruleRow(r, names) {
  const opts = [...names, "any"];
  // Editing a rule repaints only the matrix above; the row itself already
  // shows the new value (it is the input), so rebuilding it would just
  // throw away focus.
  const repaint = () => { if (hooks.firewall) hooks.firewall.refreshMatrix(); };
  const tr = el("tr");
  tr.append(textCell(r, "name"));
  tr.append(selectCell(r, "action", ["allow", "drop", "reject"], { onchange: repaint }));
  tr.append(selectCell(r, "src", opts, { onchange: repaint }));
  tr.append(selectCell(r, "dst", opts, { onchange: repaint }));
  tr.append(textCell(r, "protocol"));
  tr.append(textCell(r, "ports", { mono: true }));
  tr.append(el("td", {}, el("input", {
    type: "checkbox", checked: r.enabled !== false,
    onchange: (e) => { r.enabled = e.target.checked; repaint(); },
  })));
  tr.append(textCell(r, "description"));
  tr.append(el("td", {}, el("button", {
    class: "small danger",
    onclick: () => {
      doc.firewall_rules.splice(doc.firewall_rules.indexOf(r), 1);
      if (hooks.firewall) {
        hooks.firewall.refreshRules();
        hooks.firewall.refreshMatrix();
        renderNav(tabList());
      } else {
        render();
      }
    },
  }, "✕")));
  return tr;
}

/* ---------- WANs & infrastructure ---------- */

function renderWans() {
  const main = $("#content");
  main.append(el("h2", {}, "WAN uplinks"));
  const wbody = el("tbody");
  const ibody = el("tbody");
  // Same in-place refresh discipline as every other table: add/delete
  // rebuilds only the tbody it touched, scroll and in-progress edits in the
  // OTHER table survive. See the hooks comment up top.
  const buildWans = () => { wbody.replaceChildren(...doc.wans.map(wanRow)); };
  const buildInfra = () => { ibody.replaceChildren(...doc.infrastructure.map(infraRow)); };
  const focusLastInput = (tbody) => {
    const tr = tbody.lastElementChild;
    if (!tr) return;
    const input = tr.querySelector("input");
    if (input) input.focus();
    tr.scrollIntoView({ block: "nearest" });
  };
  hooks.wans = { buildWans, buildInfra, focusLastInput };
  buildWans();
  buildInfra();
  main.append(el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        ...["Name", "Type", "Failover priority", "Notes", ""].map((h) => el("th", {}, h)))),
      wbody)));
  main.append(el("div", { class: "toolbar" },
    el("button", {
      class: "small",
      onclick: () => {
        // A default NAME, not an empty one: the schema requires a non-empty
        // WAN name, so a blank row would make the whole document invalid and
        // the next Save would come back 422 pointing at a field the user had
        // not filled in yet. A button that creates an unsaveable document is
        // a bad button. Rename it in place.
        doc.wans.push({
          name: "new uplink", wan_type: "", failover_priority: null,
          notes: "", source: "manual",
        });
        buildWans();
        focusLastInput(wbody);
      },
    }, "+ Add WAN"),
    el("span", { class: "hint" },
      "for an uplink the controller does not know about — a 5G dongle not plugged "
      + "in yet, a second line on order, an ISP account worth writing down. "
      + "Hand-added uplinks are renameable; the controller's are not.")));

  main.append(el("h2", {}, "Network gear"));
  main.append(el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        ...["Name", "Model", "Kind", "IP", "MAC", "Notes", ""].map((h) => el("th", {}, h)))),
      ibody)));
  main.append(el("div", { class: "toolbar" },
    el("button", {
      class: "small",
      onclick: () => {
        doc.infrastructure.push({
          name: "", unifi_name: "", model: "", kind: "", ip: "", mac: "", notes: "",
        });
        buildInfra();
        focusLastInput(ibody);
      },
    }, "+ Add gear"),
    el("span", { class: "hint" },
      "for anything UniFi does not manage — an unmanaged switch, a patch panel, "
      + "a powerline adapter. The controller will never report it, so here is the "
      + "only place it can exist.")));
}

/* ---------- history: the bucket's versions, made readable ---------- */

function renderHistory() {
  const main = $("#content");
  main.append(el("div", { class: "empty", id: "history-loading" }, "loading history…"));
  fetch("/api/history")
    .then((r) => { if (!r.ok) throw new Error("history returned " + r.status); return r.json(); })
    .then((body) => {
      if (activeTab !== "history") return; // the user moved on mid-fetch
      main.replaceChildren();
      main.append(el("div", { class: "matrix-legend" },
        "Every Save is a version in the S3 bucket (object versioning), and every entry "
        + "below is recoverable. Restoring writes the old content as a ",
        el("b", {}, "new"), " version — history is never deleted."));
      const tbody = el("tbody");
      for (const v of body.versions) {
        const tr = el("tr");
        tr.append(el("td", { class: "mono" }, new Date(v.last_modified).toLocaleString()));
        tr.append(el("td", {},
          el("ul", { class: "changes" }, ...v.changes.map((c) => el("li", {}, c)))));
        tr.append(el("td", { class: "mono" }, (v.size / 1024).toFixed(1) + " KB"));
        tr.append(el("td", { class: "mono dim" }, v.version_id.slice(0, 8)));
        tr.append(el("td", {}, v.is_latest
          ? el("span", { class: "pill res" }, "current")
          : el("button", {
              class: "small",
              onclick: () => restoreVersion(v.version_id),
            }, "Restore")));
        tbody.append(tr);
      }
      main.append(el("div", { class: "table-wrap" },
        el("table", {},
          el("thead", {}, el("tr", {},
            ...["When", "What changed vs the previous version", "Size", "Version", ""]
              .map((h) => el("th", {}, h)))),
          tbody)));
    })
    .catch((err) => {
      if (activeTab !== "history") return;
      main.replaceChildren(el("div", { class: "empty" }, "could not load history: " + err.message));
    });
}

async function restoreVersion(versionId) {
  if (!confirm("Restore this version?\n\nIt will be written as a NEW version through the "
      + "normal save path - nothing in the history is deleted. Unsaved edits on this "
      + "page are discarded.")) return;
  setStatus("restoring…");
  const oldResp = await fetch("/api/network?version=" + encodeURIComponent(versionId));
  if (!oldResp.ok) { setStatus("restore failed: " + oldResp.status, "err"); return; }
  const historical = await oldResp.json();
  // The restore goes through the SAME optimistic lock as any save: read the
  // current token, write against it, surface a 409 like any other conflict.
  const current = await (await fetch("/api/network")).json();
  const resp = await fetch("/api/network", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version: current.version, document: historical.document }),
  });
  if (resp.ok) {
    const body = await resp.json();
    doc = historical.document;
    version = body.version;
    setStatus("restored", "ok");
    hideBanner();
    render();
    return;
  }
  const body = await resp.json().catch(() => ({}));
  setStatus("restore failed", "err");
  showBanner([
    el("strong", {}, "Not restored (" + resp.status + "): "
      + (body.detail || resp.statusText)),
    el("ul", {}, ...(body.changes || body.errors || []).map((c) => el("li", {}, c))),
  ]);
}

function wanRow(w) {
  const tr = el("tr");
  const manual = w.source === "manual";
  // A UniFi uplink's name is its MERGE KEY, so renaming it here would not
  // match the next import and would duplicate the row instead of renaming
  // it — read-only, annotate with Notes instead. A hand-added one is never
  // matched against an import, so it renames freely.
  if (manual) {
    tr.append(textCell(w, "name", { placeholder: "name this uplink" }));
    tr.append(textCell(w, "wan_type", { mono: true, placeholder: "pppoe / dhcp / static" }));
    tr.append(textCell(w, "failover_priority", { mono: true, placeholder: "4" }));
  } else {
    tr.append(el("td", { title: "from UniFi: this name is how the row is matched on "
                              + "re-import, so it cannot be edited here. Use Notes." }, w.name));
    tr.append(el("td", { class: "mono" }, w.wan_type || ""));
    tr.append(el("td", { class: "mono" }, w.failover_priority != null ? String(w.failover_priority) : ""));
  }
  tr.append(textCell(w, "notes"));
  tr.append(el("td", {}, manual
    ? el("button", {
        class: "small danger",
        onclick: () => {
          doc.wans.splice(doc.wans.indexOf(w), 1);
          if (hooks.wans) hooks.wans.buildWans(); else render();
        },
      }, "✕")
    : el("span", { class: "pill stable", title: "reported by the controller" }, "unifi")));
  return tr;
}

function infraRow(i) {
  const tr = el("tr");
  // Name, model, kind, IP and MAC are all editable now. They were read-only
  // because the controller reports them — but the controller only reports
  // gear it MANAGES. An unmanaged switch, a patch panel or a powerline
  // adapter is invisible to UniFi and can only ever be typed in here, and a
  // table you cannot type into is a report, not an inventory.
  tr.append(textCell(i, "name"));
  tr.append(textCell(i, "model", { mono: true }));
  tr.append(textCell(i, "kind", { placeholder: "switch / ap / other" }));
  tr.append(textCell(i, "ip", { mono: true, placeholder: "0.0.0.0" }));
  tr.append(textCell(i, "mac", { mono: true, placeholder: "aa:bb:cc:dd:ee:ff" }));
  tr.append(textCell(i, "notes"));
  tr.append(el("td", {}, el("button", {
    class: "small danger",
    onclick: () => {
      doc.infrastructure.splice(doc.infrastructure.indexOf(i), 1);
      if (hooks.wans) hooks.wans.buildInfra(); else render();
    },
  }, "✕")));
  return tr;
}

/* ---------- save / conflict handling ---------- */

function normalise() {
  for (const d of doc.devices) d.mac = String(d.mac).trim().toLowerCase();
  for (const i of doc.infrastructure) i.mac = String(i.mac).trim().toLowerCase();
}

async function save() {
  normalise();
  hideBanner();
  setStatus("saving…");
  const resp = await fetch("/api/network", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version, document: doc }),
  });
  if (resp.ok) {
    const body = await resp.json();
    version = body.version;
    setStatus("saved", "ok");
    setTimeout(() => setStatus(""), 3000);
    return;
  }
  const body = await resp.json().catch(() => ({}));
  if (resp.status === 409) {
    /* The other writer won. Never silently merge, never overwrite: show what
     * moved and let the human reload and redo. */
    setStatus("conflict", "err");
    showBanner([
      el("strong", {}, "Not saved - someone else saved first."),
      el("ul", {}, ...(body.changes || []).map((c) => el("li", {}, c))),
      el("button", { onclick: reload }, "Reload their version (discards your unsaved edits)"),
    ]);
  } else if (resp.status === 422) {
    setStatus("invalid", "err");
    showBanner([
      el("strong", {}, "Not saved - the document failed validation. Nothing was written."),
      el("ul", {}, ...(body.errors || []).map((c) => el("li", {}, c))),
    ]);
  } else if (resp.status === 401) {
    /* The session cookie expired mid-edit (they live a week). Deliberately a
     * banner with a link, NOT an automatic redirect: navigating away here
     * would throw away every unsaved edit on the page. Log in from another
     * tab, come back, Save again - nothing is lost. */
    setStatus("session expired", "err");
    showBanner([
      el("strong", {}, "Not saved - your session has expired."),
      el("div", {},
        el("a", { href: "/login", target: "_blank", rel: "noopener" },
          "Log in again in a new tab"),
        ", then come back and press Save - your edits are still here."),
    ]);
  } else {
    setStatus("error " + resp.status, "err");
    showBanner([el("strong", {}, "Save failed: " + (body.detail || resp.statusText))]);
  }
}

async function reload() {
  const resp = await fetch("/api/network");
  const body = await resp.json();
  doc = body.document;
  version = body.version;
  hideBanner();
  setStatus("reloaded", "ok");
  render();
}

$("#save").addEventListener("click", save);
render();
