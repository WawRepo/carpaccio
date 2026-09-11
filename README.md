# cARPaccio

**Every VLAN, every address, written down.**

cARPaccio is a home-network inventory sheet: a place to write your network
down and keep it tidy. One screen per VLAN showing every device — fixed IPs
with descriptions, DHCP clients with their MAC (and whether that MAC is
randomised, which decides whether a reservation will ever stick), the DHCP
ranges, and the firewall rules that separate the VLANs, including a
VLAN-to-VLAN matrix of who can reach whom.

The name is styled **cARPaccio** — ARP, sliced thin.

## Why it exists

A UniFi controller knows a lot, but not everything, and it tells you nothing
in one screen:

- It knows about **DHCP reservations**, but not about addresses hosts hold
  **by their own configuration** (`assignment: static`) — a dozen cluster
  nodes pinning 192.168.1.212–224 are invisible to it and are exactly the
  addresses at risk of being handed out twice. Those entries are written here
  by hand, because no API will ever report them.
- It shows clients, but not **which of them sit on a randomised MAC** — the
  locally-administered bit, computed from the address itself, not a vendor
  list.
- A network with **zero firewall rules** looks the same as a segmented one
  until you draw the matrix.

## How it works

- **FastAPI** backend, server-rendered Jinja2 + a small amount of plain
  JavaScript. No node, no build step.
- **State is one JSON document in S3 (MinIO).** Not a database, not git. The
  bucket has object versioning switched on — that *is* the history/rollback
  mechanism.
- **Two writers never clobber each other.** Every read hands out a version
  token; every write must present it. A stale write is refused with HTTP 409
  and a description of what changed — never silently merged. Last-writer-wins
  on a firewall table is how a VLAN quietly stops being separated.
- **Every write is validated against a JSON Schema**
  (`src/carpaccio/schema/network.schema.json`, `GET /api/schema`). An invalid
  document is refused with 422 and the stored object is left untouched.

## Quickstart

```sh
docker compose up
```

brings up MinIO, creates the versioned bucket, and serves the sheet on
<http://localhost:8000>. First run shows an empty document — not an error.

To fill it from a UniFi controller (UDM / UDM SE, UniFi OS on port 443):

```sh
export UNIFI_HOST=192.168.1.1 UNIFI_USER=readonly UNIFI_PASSWORD=...
uv run carpaccio-import            # merges into the stored document
uv run carpaccio-import --dry-run  # prints the parsed document instead
```

The importer is **read-only against the controller** (the login POST is the
only non-GET it ever sends) and **merges, keyed on MAC**: hand-written
notes, names and `static` entries survive every re-import.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CARPACCIO_S3_ENDPOINT` | — | MinIO/S3 endpoint URL |
| `CARPACCIO_S3_ACCESS_KEY` | — | S3 access key |
| `CARPACCIO_S3_SECRET_KEY` | — | S3 secret key |
| `CARPACCIO_S3_BUCKET` | `carpaccio` | Bucket (versioning should be enabled) |
| `CARPACCIO_S3_KEY` | `network.json` | Object key of the document |
| `UNIFI_HOST` | — | Importer only: controller host (port 443) |
| `UNIFI_USER` | — | Importer only: controller username |
| `UNIFI_PASSWORD` | — | Importer only: controller password |

Credentials come from the environment only — never from flags, files in this
repo, or defaults.

## API

**The contract is the whole document.** `GET /api/network` hands out a
version token; `PUT /api/network` must present it; a stale write is refused
with 409 and a description of what changed. Everything else is convenience
on top of that contract — each per-resource write reads the document, applies
one change, and saves against the version it just read, so it conflicts
exactly like the whole-document PUT. There is deliberately no server-side
retry on conflict: on a 409, re-read and redo.

| Route | What it does |
|---|---|
| `GET /` | the sheet UI |
| `GET /healthz` | liveness, plus whether S3 answers |
| `GET /api/network` | `{version, document}` — version is the lock token |
| `GET /api/network?version=<id>` | one historical version: `{version_id, historical, document}` (no lock token, on purpose) |
| `PUT /api/network` | `{version, document}` — 409 on a stale version, 422 on an invalid document |
| `GET /api/schema` | the JSON Schema the server validates with |
| `GET /api/history` | stored versions, newest first, each with a plain-English summary of what it changed |
| `GET /api/{vlans,devices,wans,infrastructure,firewall_rules}` | one section plus the current version token |
| `POST /api/devices` | add a device (body = the device object; 409 if the MAC exists) |
| `PATCH /api/devices/{mac}` | update some fields of one device |
| `DELETE /api/devices/{mac}` | remove one device |
| `POST /api/vlans` | add a VLAN (always stored as `source: manual`) |
| `PATCH /api/vlans/{name}` | update a VLAN; renaming follows the rules below |
| `DELETE /api/vlans/{name}` | remove a VLAN — refused with 409 while devices still reference it |
| `POST /api/firewall_rules` | add a rule (an `id` is minted if absent) |
| `PATCH /api/firewall_rules/{id}` | update one rule |
| `DELETE /api/firewall_rules/{id}` | remove one rule |

Two rules the API enforces rather than documents away:

- **A UniFi VLAN cannot be renamed here** (409): its name is the merge key on
  re-import, and renaming it would duplicate the segment instead. Rename it
  on the controller. Every other field of a UniFi VLAN edits freely.
- **A VLAN with devices on it cannot be deleted** (409, with the count):
  deleting it would orphan those rows off every tab. Move or delete the
  devices first — nothing is dropped silently.

Per-resource writes accept an optional `If-Match: <version>` header to pin
the write to a version read earlier; without it the write is pinned to the
version read inside the request. Either way a concurrent save produces a 409,
never a lost update.

### Worked examples

```sh
BASE=http://localhost:8000

# read one section (the version token rides along)
curl -s $BASE/api/vlans | jq

# add a static entry the controller will never know about
curl -s -X POST $BASE/api/devices -H 'Content-Type: application/json' -d '{
  "mac": "02:00:00:00:00:01", "name": "k3s node pi7",
  "ip": "192.168.1.218", "vlan": "home", "assignment": "static"
}' | jq

# annotate a device / fix a wrong field
curl -s -X PATCH $BASE/api/devices/02:00:00:00:00:01   -H 'Content-Type: application/json' -d '{"notes": "rack 2, top"}' | jq

# add a firewall rule; the id comes back minted
curl -s -X POST $BASE/api/firewall_rules -H 'Content-Type: application/json' -d '{
  "name": "guest cannot reach home", "action": "drop", "src": "guest", "dst": "home"
}' | jq

# an agent that wants the strict mode: pin the write to a version it read
V=$(curl -s $BASE/api/network | jq -r .version)
curl -s -X DELETE $BASE/api/devices/02:00:00:00:00:01 -H "If-Match: $V" | jq

# history, and reading one old version
curl -s $BASE/api/history | jq '.versions[0]'
curl -s "$BASE/api/network?version=<version_id>" | jq .document

# validate before writing: the served schema is the one the server enforces
curl -s $BASE/api/schema > network.schema.json
# ... check your document against it (e.g. python -m jsonschema, ajv) ...
```

Restoring an old version is a normal save: `GET /api/network?version=<id>`
for the content, `GET /api/network` for the current token, then `PUT` the old
content with that token. It lands as a **new** version — history is never
rewritten or deleted, and the History tab's Restore button does exactly this.

## Security

**cARPaccio has no authentication.** That is a deliberate v1 decision, not an
oversight. Anyone who can reach the port can read and rewrite your network
inventory. Keep it on the LAN (or behind your own reverse-proxy auth); do not
expose it to the internet.

## Development

```sh
uv sync --all-groups
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The test fixtures under `tests/fixtures/unifi/` are **synthetic**: generated
by `tests/fixtures/sanitise.py` from real controller responses that never
entered the repo. They keep the exact structure and counts of the real API
payloads (87 clients, 17 reservations, 8 active randomised MACs, an empty
firewall-policy list) with every MAC, address, name, token and coordinate
replaced. The real data lives only in the owner's S3 bucket.

## Deploying to Kubernetes

A Helm chart lives in `charts/carpaccio/`. The pod is stateless (all state is
in S3), so it carries no PVC and no nodeSelector and can schedule on any
node. S3 credentials are read from an existing Secret — see
`charts/carpaccio/values.yaml`.
