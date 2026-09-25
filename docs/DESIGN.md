# Why cARPaccio is shaped like this

Background reading. You do not need any of it to use the tool — start with the
[README](../README.md), which is the whole of what you need.

## Why it exists

**A network's real state is never in one system, and the parts no system
knows are the parts that bite.**

- **Addresses a host holds by its own configuration** (`assignment: static`)
  appear in no controller, no lease table and no API. A dozen cluster nodes
  pinning `192.168.1.212–224` are invisible — and they are exactly the
  addresses at risk of being handed out twice.
- **Addresses no device owns at all**: a virtual IP, a load-balancer pool.
  ARP attributes them to whichever machine answered last, and during a
  failover nothing answers, so "is this address free?" says **yes** about the
  address your cluster's API lives on. That nearly happened here.
- **Which clients sit on a randomised MAC** — the locally-administered bit,
  computed from the address itself, not a vendor list. A DHCP reservation on
  one of those silently stops applying.
- **A network with zero firewall rules looks exactly like a segmented one**
  until somebody draws the matrix.

None of that is a gap in any particular vendor. It is the difference between
what machines report and what is true, and closing it needs something that
writes things down.

## Reserved addresses — the ones no device owns

Some addresses are in use and belong to no machine: a virtual IP that floats
between nodes, a load-balancer pool handed out to services. They are invisible
to everything else this tool reads.

- **No controller reports them.** There is no lease and no device record.
- **ARP attributes them to whichever machine answered last**, so they read as a
  duplicate of a host already in the inventory.
- **They can be completely silent.** During a failover, or when nothing has
  spoken to one recently, there is no ARP entry at all.

That last case is why this section exists. "Is this address free?" would answer
**yes** about an address that is very much in use. On 2026-09-24 the Kubernetes
API VIP at `192.168.1.230` was nearly handed to a new node; it was caught only
because a pre-flight ping happened to answer, and answered as the machine that
held the VIP at that moment.

Put them in the **DHCP ranges** tab, under *Reserved — addresses no device
owns*. They are excluded from every free-address answer, and an import never
drops them. They also appear, read-only, in the **All** tab below — because
somebody looking for `.230` types the number, and is not thinking "that will be
filed under DHCP ranges".

**This list is hand-maintained, and that is the honest answer rather than a
limitation to apologise for.** An address no API reports is one a human has to
write down, which is the same reason the tool already carries an
`assignment: static` case for host-configured addresses.

## The All tab, and searching for an address

Each VLAN gets its own tab, which answers *what is on this segment*. The **All**
tab answers the other question — *where is this address* — with every VLAN in
one table, a VLAN column, and one filter box over the lot. Reserved addresses
are in that table too, read-only and marked; they are edited on the DHCP ranges
tab, which stays their home.

There is no *+ Add device* on the All tab, on purpose: a device belongs to a
VLAN, and this table spans all of them, so there is no honest answer to which
one a new row would join.

**The filter box reads a number as one whole octet, in any position.**

| You type | It finds |
|---|---|
| `230` | `192.168.1.230`, and `10.0.230.4` — any octet that IS 230 |
| `2` | `192.168.1.2`, and **not** `192.168.1.25` |
| `192.168.1.` | the whole /24 (a trailing dot is an explicit prefix) |
| `192.168.1.2` | that address only, never `.25` |
| `printer` | name, hostname and MAC, case-insensitively |

The rule being protected is the **octet boundary**: `.2` must never match `.25`,
because that is precisely the wrong row to show somebody checking whether `.2`
is free. A bare number matching any position is new (0.7.0) — reading one as a
prefix made the box useless on a network where every address starts `192.168.1`,
and it is how `.230` looked absent from the document when it had been recorded
all along.

A reserved **range** answers for every address inside it: `242` finds the
MetalLB pool written as `.240`–`.244`, even though 242 appears nowhere.


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

