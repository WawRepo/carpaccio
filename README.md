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

| Route | What it does |
|---|---|
| `GET /` | the sheet UI |
| `GET /healthz` | liveness, plus whether S3 answers |
| `GET /api/network` | `{version, document}` — version is the lock token |
| `PUT /api/network` | `{version, document}` — 409 on a stale version, 422 on an invalid document |
| `GET /api/schema` | the JSON Schema |

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
