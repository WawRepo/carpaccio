/* cARPaccio sheet UI. Plain JS on purpose: no build step, no framework.
 * The document is a single JS object; inputs mutate it in place and Save
 * PUTs the whole thing with the version token for the optimistic lock. */
"use strict";

let doc = JSON.parse(document.getElementById("document-data").textContent);
let version = window.CARPACCIO_VERSION;
// The active tab lives in the URL hash so a tab can be linked or reloaded.
let activeTab = decodeURIComponent(window.location.hash.slice(1)) || null;

const $ = (sel) => document.querySelector(sel);

/* Randomised = locally-administered bit of the first octet. Computed from the
 * address, never a vendor list. It decides whether a DHCP reservation
 * survives the device rejoining the network. */
function macIsRandomised(mac) {
  const first = parseInt(String(mac).split(":")[0], 16);
  return Number.isFinite(first) && (first & 2) !== 0;
}

function ipSortKey(ip) {
  const m = String(ip).match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (!m) return Number.MAX_SAFE_INTEGER;
  return ((+m[1] * 256 + +m[2]) * 256 + +m[3]) * 256 + +m[4];
}

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
  tabs.push({ id: "dhcp", label: "DHCP ranges", render: renderDhcp });
  tabs.push({ id: "firewall", label: "Firewall", count: doc.firewall_rules.length, render: renderFirewall });
  tabs.push({ id: "wans", label: "WANs & infra", render: renderWans });
  return tabs;
}

function render() {
  const tabs = tabList();
  if (!tabs.some((t) => t.id === activeTab)) activeTab = tabs.length ? tabs[0].id : null;
  const nav = $("#tabs");
  nav.replaceChildren(
    ...tabs.map((t) =>
      el(
        "div",
        {
          class: "tab" + (t.id === activeTab ? " active" : ""),
          onclick: () => { activeTab = t.id; render(); },
        },
        t.label,
        t.count !== undefined ? el("span", { class: "count" }, String(t.count)) : null,
      ),
    ),
  );
  const main = $("#content");
  main.replaceChildren();
  if (activeTab) window.history.replaceState(null, "", "#" + encodeURIComponent(activeTab));
  const tab = tabs.find((t) => t.id === activeTab);
  if (tab) tab.render();
  else main.append(el("div", { class: "empty" }, "Empty document - run carpaccio-import or add a VLAN in DHCP ranges."));
}

/* ---------- per-VLAN device sheet ---------- */

const ASSIGNMENTS = ["reservation", "static", "dhcp"];

function renderVlan(vlan) {
  const main = $("#content");
  const dhcp = vlan.dhcp || {};
  main.append(
    el(
      "div",
      { class: "vlan-summary" },
      summaryItem("subnet", vlan.subnet),
      summaryItem("802.1q", vlan.vlan_id === null || vlan.vlan_id === undefined ? "untagged" : String(vlan.vlan_id)),
      summaryItem("dhcp", dhcp.enabled ? `${dhcp.start} – ${dhcp.stop}` : "off"),
      summaryItem("dns", (dhcp.dns || []).join(", ") || "—"),
      // Say it on the VLAN's own tab too, not only in the firewall matrix.
      // Whether this network can talk to the others is the first thing anyone
      // asks about a segment, and it is invisible everywhere else in UniFi.
      summaryItem("isolation", vlan.isolated
        ? "ON — cannot reach other local networks"
        : "off — can reach other local networks"),
    ),
  );

  const devices = doc.devices
    .filter((d) => d.vlan === vlan.name)
    .sort((a, b) => ipSortKey(a.ip) - ipSortKey(b.ip));

  const tbody = el("tbody");
  for (const d of devices) tbody.append(deviceRow(d));

  main.append(
    el("div", { class: "table-wrap" },
      el("table", { class: "devices" },
        el("thead", {},
          el("tr", {},
            ...["Description", "IP", "Assignment", "MAC", "Random MAC?", "Hostname", "Link", "Active", "Notes", ""]
              .map((h) => el("th", {}, h)))),
        tbody)),
    el("div", { class: "toolbar" },
      el("button", {
        onclick: () => {
          doc.devices.push({
            mac: "", name: "", hostname: "", ip: "", vlan: vlan.name,
            // "static" is the default for hand-added rows: the whole reason
            // to add one by hand is that no API will ever report it.
            assignment: "static", wired: true, active: false, oui: "",
            last_seen: null, notes: "", source: "manual",
          });
          render();
        },
      }, "+ Add device"),
      el("span", { class: "hint" },
        "static = configured on the host itself; the controller cannot see it, so write it down here.")),
  );
}

function summaryItem(k, v) {
  return el("span", {}, el("span", { class: "k" }, k), el("span", { class: "v" }, v ?? ""));
}

function deviceRow(d) {
  const tr = el("tr", { class: d.active ? "" : "inactive" });
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
  tr.append(el("td", {}, el("span", { class: "pill " + (d.active ? "up" : "down") }, d.active ? "up" : "—")));
  tr.append(textCell(d, "notes"));
  tr.append(el("td", {}, el("button", {
    class: "small danger",
    onclick: () => { doc.devices.splice(doc.devices.indexOf(d), 1); render(); },
  }, "✕")));
  return tr;
}

/* ---------- DHCP ranges ---------- */

function renderDhcp() {
  const main = $("#content");
  const tbody = el("tbody");
  for (const v of doc.vlans) {
    v.dhcp = v.dhcp || { enabled: false, start: "", stop: "", lease_seconds: null, dns: [] };
    const tr = el("tr");
    tr.append(el("td", {}, v.name));
    tr.append(el("td", { class: "mono" }, v.vlan_id === null || v.vlan_id === undefined ? "untagged" : String(v.vlan_id)));
    tr.append(el("td", { class: "mono" }, v.subnet));
    tr.append(el("td", {}, el("input", {
      type: "checkbox", checked: !!v.dhcp.enabled,
      onchange: (e) => { v.dhcp.enabled = e.target.checked; },
    })));
    tr.append(textCell(v.dhcp, "start", { mono: true }));
    tr.append(textCell(v.dhcp, "stop", { mono: true }));
    tr.append(el("td", { class: "mono" }, v.dhcp.lease_seconds ? `${v.dhcp.lease_seconds}s` : "—"));
    tr.append(el("td", { class: "mono" }, (v.dhcp.dns || []).join(", ")));
    tr.append(textCell(v, "notes"));
    tbody.append(tr);
  }
  main.append(
    el("div", { class: "table-wrap" },
      el("table", {},
        el("thead", {}, el("tr", {},
          ...["VLAN", "802.1q", "Subnet", "DHCP", "Start", "Stop", "Lease", "DNS", "Notes"]
            .map((h) => el("th", {}, h)))),
        tbody)),
  );
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
          title: `${src} has UniFi Network Isolation on: it cannot reach any `
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
  main.append(el("div", { class: "table-wrap", style: "display:inline-block" }, matrix));
  main.append(el("div", { class: "matrix-legend" },
    el("b", {}, "isolated"),
    " = UniFi Network Isolation is on for that source network. It cannot reach any "
    + "other local network, and this is enforced by the gateway rather than by a "
    + "rule — so it shows up in no rule list, and a rules-only audit will wrongly "
    + "report the segment as wide open. ",
    el("br", {}),
    "“no rule” means whatever the router defaults to - which on most UniFi setups is ",
    el("span", { class: "mono" }, "allow"),
    ". Click a cell to add a rule for that pair."));

  main.append(el("h2", {}, "Rules (evaluated top-down)"));
  const tbody = el("tbody");
  for (const r of doc.firewall_rules) tbody.append(ruleRow(r, names));
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

function addRule(src, dst) {
  doc.firewall_rules.push({
    id: "manual-" + Date.now(),
    name: src && dst ? `${src} to ${dst}` : "",
    action: "drop",
    src: src || (doc.vlans[0] ? doc.vlans[0].name : "any"),
    dst: dst || "any",
    protocol: "any",
    ports: "",
    enabled: true,
    description: "",
  });
  render();
}

function ruleRow(r, names) {
  const opts = [...names, "any"];
  const tr = el("tr");
  tr.append(textCell(r, "name"));
  tr.append(selectCell(r, "action", ["allow", "drop", "reject"], { onchange: render }));
  tr.append(selectCell(r, "src", opts, { onchange: render }));
  tr.append(selectCell(r, "dst", opts, { onchange: render }));
  tr.append(textCell(r, "protocol"));
  tr.append(textCell(r, "ports", { mono: true }));
  tr.append(el("td", {}, el("input", {
    type: "checkbox", checked: r.enabled !== false,
    onchange: (e) => { r.enabled = e.target.checked; render(); },
  })));
  tr.append(textCell(r, "description"));
  tr.append(el("td", {}, el("button", {
    class: "small danger",
    onclick: () => { doc.firewall_rules.splice(doc.firewall_rules.indexOf(r), 1); render(); },
  }, "✕")));
  return tr;
}

/* ---------- WANs & infrastructure ---------- */

function renderWans() {
  const main = $("#content");
  main.append(el("h2", {}, "WAN uplinks"));
  const wbody = el("tbody");
  for (const w of doc.wans) {
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
          onclick: () => { doc.wans.splice(doc.wans.indexOf(w), 1); render(); },
        }, "✕")
      : el("span", { class: "pill stable", title: "reported by the controller" }, "unifi")));
    wbody.append(tr);
  }
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
        render();
      },
    }, "+ Add WAN"),
    el("span", { class: "hint" },
      "for an uplink the controller does not know about — a 5G dongle not plugged "
      + "in yet, a second line on order, an ISP account worth writing down. "
      + "Hand-added uplinks are renameable; the controller's are not.")));

  main.append(el("h2", {}, "Network gear"));
  const ibody = el("tbody");
  for (const i of doc.infrastructure) {
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
      onclick: () => { doc.infrastructure.splice(doc.infrastructure.indexOf(i), 1); render(); },
    }, "✕")));
    ibody.append(tr);
  }
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
        render();
      },
    }, "+ Add gear"),
    el("span", { class: "hint" },
      "for anything UniFi does not manage — an unmanaged switch, a patch panel, "
      + "a powerline adapter. The controller will never report it, so here is the "
      + "only place it can exist.")));
}

/* ---------- save / conflict handling ---------- */

function normalise() {
  for (const d of doc.devices) d.mac = String(d.mac).trim().toLowerCase();
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
