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
    assert '"/static/netcalc.js?v=' in body
    assert body.index("netcalc.js") < body.index("app.js")


ICON_LINKS = ("/static/favicon.ico", "/static/icon-192.png", "/static/apple-touch-icon.png")


def test_both_pages_declare_the_icon(client):
    """HOME-383: with no <link rel="icon"> the browser tab shows a generic globe.
    The login page matters most - it is the first page a browser ever sees."""
    for path in ("/", "/login"):
        html = client.get(path).text
        for href in ICON_LINKS:
            assert f'href="{href}?v=' in html, f"{path} does not link {href}"


STATIC_DIR = Path(__file__).parent.parent / "src" / "carpaccio" / "static"


def test_every_static_link_carries_its_content_fingerprint(client):
    """HOME-371: fixed URLs let a browser keep last release's app.js. Every
    link must carry ?v=<first 12 hex of sha256(file)>, so the fingerprint
    CHANGES exactly when the file does - checked against the bytes, not
    against whatever the app happens to print."""
    import hashlib
    import re

    body = client.get("/").text
    links = re.findall(r'(?:href|src)="/static/([^"?]+)\?v=([0-9a-f]+)"', body)
    names = {n for n, _ in links}
    assert {"style.css", "netcalc.js", "app.js", "favicon.ico"} <= names, names
    every = re.findall(r'(?:href|src)="(/static/[^"]*)"', body)
    unversioned = [u for u in every if "?v=" not in u]
    assert not unversioned, f"unversioned /static links: {unversioned}"
    for name, v in links:
        want = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
        assert v == want, (name, v, want)


def test_pages_revalidate_and_versioned_assets_are_immutable(client):
    for path in ("/",):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    body = client.get("/").text
    import re

    versioned = re.search(r'src="(/static/app.js\?v=[0-9a-f]+)"', body).group(1)
    assert "immutable" in client.get(versioned).headers["cache-control"]
    # the bare URL (an old page, a curl) is still served, but never trusted
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js?v=000000000000").headers["cache-control"] == "no-cache"
