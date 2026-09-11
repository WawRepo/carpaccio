"""The frontend is one hand-written .js file with no build step, so NOTHING
checks it. That is the price of having no toolchain, and it was paid in full:
a missing closing paren shipped to the cluster, the whole script failed to
parse, and the page rendered as a header over an empty void — no tabs, no
table, no error visible to anyone not opening devtools.

ruff checks Python. pytest checks Python. helm lint checks the chart. A broken
frontend passed every gate this project had.

These tests are cheap and they close that hole: a syntax check, and a
smoke test that the served page actually contains the things it promises.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "src" / "carpaccio" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_parses():
    """`node --check` on every shipped .js file.

    Skipped rather than failed when node is absent, because the CI image has it
    and a developer without it should not be blocked -- but see the CI workflow,
    where it is NOT optional.
    """
    for js in sorted(STATIC.glob("*.js")):
        proc = subprocess.run(
            ["node", "--check", str(js)], capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, f"{js.name} does not parse:\n{proc.stderr}"


def test_page_promises_what_it_delivers(client):
    """The served HTML carries the mount points the script expects."""
    body = client.get("/").text
    for needle in ('id="tabs"', 'id="content"', 'id="save"', 'id="document-data"'):
        assert needle in body, f"index.html no longer contains {needle}"


def test_netcalc_loads_before_app_js(client):
    """app.js reads the `netcalc` global at parse time; if the script order
    ever flips, the page dies exactly like the missing-paren incident -
    silently, with every gate green except this one."""
    body = client.get("/").text
    assert '"/static/netcalc.js"' in body
    assert body.index("netcalc.js") < body.index("app.js")
