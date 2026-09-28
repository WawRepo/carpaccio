# HTTP API

**An agent should ask the running server, not this file.** `GET /api/schema`
returns the schema the server actually validates against, so it can never be
out of date with the thing rejecting your write. This page is for a human
reading along, and for the behaviour a schema cannot describe.

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
| `GET /api/export` | the whole document as a file to keep — the PLAIN document, no envelope, so it validates on its own. The version it was taken from rides in `ETag` and `X-Carpaccio-Version` |
| `POST /api/import?version=<token>` | replace everything from a file. JSON body or a multipart `file` part. 400 with the line and column on broken JSON, 422 on an invalid document, 428 without a version, 409 on a stale one |
| `GET /api/history` | stored versions, newest first, each with a plain-English summary of what it changed |
| `GET /api/{vlans,devices,wans,infrastructure,firewall_rules}` | one section plus the current version token |
| `GET /api/vlans/{name}/free` | addresses safe to hand out on that VLAN: inside the subnet, outside the DHCP pool, not the gateway, not on any device or gear. Collapsed ranges plus a count; `reason` says why when there is no answer |
| `POST /api/devices` | add a device (body = the device object; 409 if the MAC exists) |
| `PATCH /api/devices/{mac}` | update some fields of one device |
| `DELETE /api/devices/{mac}` | remove one device |
| `POST /api/vlans` | add a VLAN (always stored as `source: manual`) |
| `PATCH /api/vlans/{name}` | update a VLAN; renaming follows the rules below |
| `DELETE /api/vlans/{name}` | remove a VLAN — refused with 409 while devices still reference it |
| `POST /api/firewall_rules` | add a rule (an `id` is minted if absent) |
| `PATCH /api/firewall_rules/{id}` | update one rule |
| `DELETE /api/firewall_rules/{id}` | remove one rule |

**Import replaces, it does not merge.** Merging two inventories without being
told which one is right is how you get two rows for one machine and no way to
tell which is live. Nothing is lost: every save is a version, so the document
that was there is in `GET /api/history` and can be rolled back.

**Import needs the version token**, from `?version=` or an `If-Match` header.
It is the most destructive call here, so it goes through the same lock as
every other write rather than around it:

```bash
V=$(curl -s $BASE/api/network | jq -r .version)
curl -s $BASE/api/export -o backup.json
curl -s -X POST "$BASE/api/import?version=$V" -F "file=@backup.json"
# -> {"version": "...", "imported": {"vlans": 3, "devices": 118, ...}}
```

Two rules the API enforces rather than documents away:

- **An IMPORTED VLAN cannot be renamed here** (409) — one whose `source` is
  not `manual`. Its name is the merge key on
  re-import, and renaming it would duplicate the segment instead. Rename it
  wherever it came from. Every other field of an imported VLAN edits freely.
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

