<p align="center">
  <img src="docs/images/logo-readme.png" alt="cARPaccio — a fish sliced thin: every VLAN, every address, written down" width="420">
</p>

# cARPaccio

**Write your network down, and let an agent keep it written down.**

You say what is wrong in one sentence. An agent reads the document, works out
what changed, and writes it. You do not open a config file.

```
You:    two new machines turned up, perf1 and perf2, I set their addresses
        on the hosts themselves

Agent:  Recorded both as static. They were not in the document, and .231/.232
        were still being offered as free to hand out — to the machines already
        using them. Free count 36 → 34.
```

That is a real exchange, shortened. The person never opened the sheet.

## What it looks like

**One screen per VLAN** — every device, how it holds its address, and which
clients are on a randomised MAC (a DHCP reservation on one of those silently
stops applying). The panel on the right is what is genuinely free to hand out.

![A VLAN tab: device table, free-address panel, randomised-MAC pills](docs/images/screenshot-vlan.png)

**One table for everything**, when you have an address in your hand and do not
know which segment owns it. The amber rows are addresses **no device owns** —
a virtual IP, a load-balancer pool. Nothing answers for them during a
failover, which is how they get handed out twice.

![The All tab: every VLAN in one table, with reserved addresses marked](docs/images/screenshot-all.png)

**Who can reach whom.** A network with no firewall rules looks identical to a
segmented one until something draws this. A segment can be closed by a rule,
or by an `isolated` flag the gateway enforces without writing any rule — so
this matrix reads both, and an audit that only reads rules calls a closed
segment wide open.

![The firewall matrix: VLAN-to-VLAN reachability, with isolation shown](docs/images/screenshot-firewall.png)

*(Screenshots use invented data — the addresses are examples and the MACs are
from the range RFC 7042 reserves for documentation.)*

## Get started

**1. Run it.**

```sh
git clone <this repo> && cd carpaccio
docker compose up
```

MinIO, a versioned bucket, and the sheet on <http://localhost:8000>. The first
screen is empty — that is correct, not an error. No auth locally.

**2. Give your agent the skill.** Copy [`SKILL.md`](SKILL.md) to wherever your
agent reads skills from — for Claude Code that is
`~/.claude/skills/carpaccio/SKILL.md`, or `.claude/skills/` inside a project.

**3. Tell it two things: where cARPaccio is, and where the truth lives.**

> cARPaccio is at http://localhost:8000, no auth. My router is an OPNsense at
> 192.168.1.1, the API key is in `~/.opnsense`, and I can SSH to the machines.
> Write down every VLAN and every device.

That second half is the one people leave out. **cARPaccio discovers nothing.**
It is a document, not a scanner. The agent needs somewhere real to look —
your router, an inventory file, SSH, or just you answering questions.

**No importer for your router? Then the agent is the importer.** It reads
whatever you point it at and writes the document through the API. That is the
normal path, not a fallback.

## What it records that your router does not

- **Addresses set on the machine itself.** They are in no lease table and no
  API, and they are exactly the ones at risk of being handed out twice.
- **Addresses no device owns** — a virtual IP, a load-balancer pool. Nothing
  answers for them during a failover, so "is this free?" says *yes* about an
  address your cluster depends on.
- **Which clients use a randomised MAC**, computed from the address itself. A
  DHCP reservation on one of those silently stops applying.
- **Whether your VLANs are actually separated.** A network with no firewall
  rules looks identical to a segmented one until something draws the matrix.

## Importers

An importer fills part of the document from a system that already knows
something. **UniFi is the only one written so far**, because it is what the
author runs:

```sh
export UNIFI_HOST=192.168.1.1 UNIFI_USER=readonly UNIFI_PASSWORD=...
uv run carpaccio-import            # merges into the stored document
uv run carpaccio-import --dry-run  # prints the parsed document instead
```

It is read-only against the controller and merges on MAC, so notes, names and
hand-added entries survive every re-import.

**Running something else?** You do not need an importer. Point your agent at
it (step 3). If you want one anyway, write something that produces a document
matching `GET /api/schema` and merge it. One wart to know about: the schema's
`source` field is currently `enum: ["unifi", "manual"]`, so a new importer has
to record itself as `manual` until that enum is widened.

## Everything else

| | |
|---|---|
| [SKILL.md](SKILL.md) | what the agent reads. Also worth skimming yourself |
| [docs/DESIGN.md](docs/DESIGN.md) | why it is shaped like this, and what it refuses to do |
| [docs/API.md](docs/API.md) | the HTTP API, for humans reading along |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | env vars, auth, Kubernetes, releases |
| [CONTRIBUTING.md](CONTRIBUTING.md) | working on cARPaccio itself |

The name is styled **cARPaccio** — ARP, sliced thin.
