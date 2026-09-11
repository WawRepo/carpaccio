"""Real-browser checks for the scroll/focus behaviour of add and delete.

The bug this guards: the device table scrolls inside .table-wrap, and a full
render() used to replace that container, so every add/delete reset its
scrollTop to 0 - "every time i delete it scrolls me to up". No amount of
node-level testing can see that; it is a property of a live DOM. So this
builds a harness page around the REAL netcalc.js + app.js, drives clicks in
headless Chrome, and reads the scroll positions and focus back out of the
serialized DOM (--dump-dom after the inline script has run).

Skipped when Chrome is absent (it is present on the GitHub ubuntu runners
and on the dev Mac). Where Chrome is missing, there is NO browser coverage -
the pytest skip says so rather than pretending.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = (Path(__file__).parent.parent / "src" / "carpaccio" / "static").resolve()

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome"),
    shutil.which("google-chrome-stable"),
    shutil.which("chromium-browser"),
    shutil.which("chromium"),
]
CHROME = next((c for c in CHROME_CANDIDATES if c and Path(c).exists()), None)


def synthetic_doc() -> dict:
    """One VLAN, 40 devices - enough rows that the table really scrolls."""
    devices = [
        {
            "mac": f"aa:bb:cc:dd:ee:{i:02x}",
            "name": f"dev-{i:02d}",
            "hostname": "",
            "ip": f"192.168.1.{i + 10}",
            "vlan": "home",
            "assignment": "dhcp",
            "wired": True,
            "active": True,
            "oui": "",
            "last_seen": None,
            "notes": "",
            "source": "unifi",
        }
        for i in range(40)
    ]
    return {
        "schema_version": 1,
        "wans": [],
        "vlans": [
            {
                "name": "home",
                "vlan_id": None,
                "subnet": "192.168.1.1/24",
                "isolated": False,
                "dhcp": {
                    "enabled": True,
                    "start": "192.168.1.6",
                    "stop": "192.168.1.190",
                    "lease_seconds": 86400,
                    "dns": [],
                },
                "notes": "",
                "source": "unifi",
            }
        ],
        "devices": devices,
        "infrastructure": [],
        "firewall_rules": [],
    }


HARNESS_SCRIPT = """
const out = {};
const wrap = document.querySelector(".table-wrap");
const tbody = wrap.querySelector("tbody");
const rows = () => tbody.querySelectorAll("tr").length;
out.initial_rows = rows();

// ---- deleting must not move the scroll position -------------------------
wrap.scrollTop = 400;
out.scroll_before = wrap.scrollTop;
const del = (i) => tbody.querySelectorAll("button.danger")[i].click();
del(20);
out.same_wrap_after_delete = document.contains(wrap);
out.scroll_after_one_delete = wrap.scrollTop;
del(20); del(20); del(20); del(20); // five deletions must feel like five deletions
out.scroll_after_five_deletes = wrap.scrollTop;
out.rows_after_deletes = rows();

// ---- adding: scroll kept, cursor in the new row's first field ------------
const addBtn = [...document.querySelectorAll(".toolbar button")]
  .find((b) => b.textContent.includes("Add device"));
addBtn.click();
out.rows_after_add = rows();
out.focus_is_input = document.activeElement.tagName === "INPUT";
const focusRow = document.activeElement.closest("tr");
out.focused_row_is_empty_row = focusRow.classList.contains("row-empty");
out.focused_row_visible =
  focusRow.offsetTop >= wrap.scrollTop &&
  focusRow.offsetTop <= wrap.scrollTop + wrap.clientHeight;

// ---- the empty-rows control ----------------------------------------------
addBtn.click(); addBtn.click(); // three empty rows now
const emptyBtn = [...document.querySelectorAll(".toolbar button")]
  .find((b) => b.textContent.includes("empty row"));
out.empty_button_text = emptyBtn.textContent;
out.empty_button_visible = !emptyBtn.hidden;
emptyBtn.click();
out.rows_after_empty_cleanup = rows();
out.empty_button_hidden_after = emptyBtn.hidden;

// ---- tab bar labelling -----------------------------------------------------
out.caption = (document.querySelector(".tab-caption") || {}).textContent || "";
out.add_vlan_tab = [...document.querySelectorAll(".tab.add-vlan")]
  .map((t) => t.textContent)[0] || "";
out.separator_present = !!document.querySelector(".tab-sep");

// ---- firewall tab: add focuses the new rule, matrix refreshes in place ----
[...document.querySelectorAll(".tab")].find((t) => t.textContent.startsWith("Firewall")).click();
const addRuleBtn = [...document.querySelectorAll(".toolbar button")]
  .find((b) => b.textContent.includes("Add rule"));
addRuleBtn.click();
out.rule_focus_is_input = document.activeElement.tagName === "INPUT";
out.rule_rows = document.querySelectorAll("tbody tr").length;

const pre = document.createElement("pre");
pre.id = "test-results";
pre.textContent = JSON.stringify(out);
document.body.append(pre);
"""


@pytest.mark.skipif(CHROME is None, reason="no Chrome/Chromium on this machine")
def test_add_and_delete_keep_scroll_and_focus(tmp_path):
    doc_json = json.dumps(synthetic_doc()).replace("</", "<\\/")
    harness = tmp_path / "harness.html"
    harness.write_text(
        f"""<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="file://{STATIC}/style.css">
<style>body {{ height: 100vh; }}</style>
</head><body>
<header><span id="status"></span><button id="save"></button></header>
<div id="banner" class="banner hidden"></div>
<nav id="tabs"></nav>
<main id="content"></main>
<script id="document-data" type="application/json">{doc_json}</script>
<script>window.CARPACCIO_VERSION = "test";</script>
<script src="file://{STATIC}/netcalc.js"></script>
<script src="file://{STATIC}/app.js"></script>
<script>{HARNESS_SCRIPT}</script>
</body></html>"""
    )
    proc = subprocess.run(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--allow-file-access-from-files",
            "--window-size=1400,800",
            "--virtual-time-budget=4000",
            "--dump-dom",
            f"file://{harness}",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    marker = 'id="test-results">'
    assert marker in proc.stdout, (
        "harness produced no results - the page script crashed:\n" + proc.stdout[-2000:]
    )
    raw = proc.stdout.split(marker, 1)[1].split("</pre>", 1)[0]
    # --dump-dom HTML-escapes text content
    raw = raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    out = json.loads(raw)

    assert out["initial_rows"] == 40

    # THE bug: deletes used to reset the wrap's scrollTop to 0.
    assert out["same_wrap_after_delete"] is True, "delete rebuilt the scroll container"
    assert out["scroll_after_one_delete"] == out["scroll_before"]
    assert out["scroll_after_five_deletes"] == out["scroll_before"], (
        "five deletions must feel like five deletions, not five jumps"
    )
    assert out["rows_after_deletes"] == 35

    # Adding: cursor lands in the new row's first field, row visible.
    assert out["rows_after_add"] == 36
    assert out["focus_is_input"] is True
    assert out["focused_row_is_empty_row"] is True
    assert out["focused_row_visible"] is True

    # The empty-rows control: counted, explicit, gone when done.
    assert out["empty_button_text"] == "remove 3 empty rows"
    assert out["empty_button_visible"] is True
    assert out["rows_after_empty_cleanup"] == 35
    assert out["empty_button_hidden_after"] is True

    # Tab-bar labelling.
    assert out["caption"] == "vlans"
    assert out["add_vlan_tab"] == "+ VLAN"
    assert out["separator_present"] is True

    # Firewall add focuses the new rule row.
    assert out["rule_focus_is_input"] is True
    assert out["rule_rows"] == 1
