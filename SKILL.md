---
name: carpaccio
description: Read and maintain a network inventory — VLANs, devices, DHCP ranges, reserved addresses and firewall rules — over cARPaccio's HTTP API. Use when asked where an address is, whether one is free, to record a host, or to reconcile the document against reality.
---

# cARPaccio

A document describing a network, and an API built for you to maintain it.
The human reads the web UI. You do the writing.

**If you are sitting in the cARPaccio repository, you probably do not need
this file.** Read `README.md`, `src/carpaccio/app.py` and the 98 tests — they
are more precise than any summary, and they cannot drift from the code.

This file exists for the other case, which is the normal one: **you are
running somebody's network and cARPaccio is one tool you have been handed.**
You have a URL and a password. You will never see this repository. That was
true of the session this tool was built for — the agent that added two missing
hosts had an endpoint and nothing else.

So everything below is deliberately what you CANNOT get by asking the server:
what it does not know about itself, what only a person can tell you, and what
has already gone wrong here. **For anything mechanical — routes, fields,
types, required properties — ask the server, never this file.**

## The one thing to understand first

**The document is the truth, and it is only as true as the last thing written
into it.** cARPaccio does not discover anything. It does not poll, scan or
watch. Everything in it was put there by an importer, a human, or you.

So the failure mode is never a wrong answer — it is a *confident* answer about
something nobody wrote down. `GET /api/vlans/home/free` will happily offer an
address that a machine is using right now, if nothing ever recorded that
machine. Keeping the document current IS the job; answering questions from it
is the easy part.

## Before you can do anything, you need two things

**Neither is in this repository. Both are site-specific. If you have not been
told them, ask — do not guess, and do not scan for them.**

### 1. Where cARPaccio is, and how to authenticate

```
BASE=https://carpaccio.example.internal      # the instance for THIS network
USER=... PASS=...                            # HTTP Basic
```

Credentials come from the environment or the operator's secret store — never
a flag, never a file in this repo. An unauthenticated call gets a **401 with
JSON**, never an HTML redirect, so a failure looks like a failure.

Check you are talking to the right thing before you write anything:

```sh
curl -sf "$BASE/healthz"                       # liveness, and whether S3 answers
curl -sf -u "$USER:$PASS" "$BASE/api/schema"   # what the server will accept
```

### 2. Where the TRUTH lives — and this is the part people forget

cARPaccio **discovers nothing**. To correct it you need somewhere real to
compare it against, and that place is different at every site. Ask the
operator, in these words if it helps:

> *"When you want to know what is actually on this network — what do you
> look at? A controller, an inventory file, a cluster, a DHCP server, a
> spreadsheet? And how do I reach it?"*

Typical answers, none of which you should assume:

| What | Gives you | Reach it how |
|---|---|---|
| A router or controller (UniFi, OPNsense, Mikrotik…) | leases, reservations, clients | an API, usually read-only credentials |
| An infrastructure inventory (Ansible, Terraform, a hosts file) | machines and their intended addresses | a file in a repo you were given |
| A cluster | nodes, load-balancer addresses, virtual IPs | `kubectl`, a kubeconfig |
| The hosts themselves | the address actually configured, and the MAC | SSH |
| A person | everything nothing else knows | ask them |

**Write the answer down where the next session will find it** — the
operator's agent instructions, a project file, whatever they use. Being told
once and losing it means asking again, and an operator who answers the same
question three times stops trusting the loop.

**If you were given nothing:** you can still read and answer questions from
the document. Say plainly that you cannot verify it against anything, because
an answer from an unverified document sounds exactly like a verified one.

## The write contract, which is the whole API

**Get the route list and the document model from the running server, not from
here** — `GET /api/schema` is the schema it actually validates against, so it
can never be out of date with the thing rejecting your write. The behaviour
below is the part the schema does not describe:

1. Every read hands you a **version token**.
2. Every write must present it.
3. A stale token is **409**, with a description of what changed.
4. **There is no server-side retry.** On 409: re-read, redo, write again.

That is deliberate. Last-writer-wins on a firewall table is how a VLAN quietly
stops being separated.

Per-resource writes (`POST/PATCH/DELETE /api/devices` and friends) read the
document, change one thing, and save against the version they just read, so
they conflict exactly like a whole-document `PUT`. Prefer them; send the whole
document only when changing many things at once. An optional
`If-Match: <version>` pins a write to a version you read earlier.

An invalid document is **422** and the stored object is left untouched. **You
cannot corrupt it with a bad write — you can only fail.** Write freely.

## What only a human or an agent can know

These are the parts no controller reports, and the reason this tool exists.
**If you learn one of these facts while doing something else, write it down.**

### `assignment: static` — an address configured on the host
A machine with a static address in its own netplan/interfaces file appears in
no lease table. Record it with the MAC, the address, and a note saying *where*
the address actually lives, so the next person knows what to edit.

```json
{"mac":"aa:bb:cc:dd:ee:ff","name":"node4","ip":"10.0.0.29","vlan":"home",
 "assignment":"static","wired":true,"source":"manual",
 "notes":"set on the host in /etc/netplan/60-static.yaml, not a reservation"}
```

### `reserved` — an address no device owns
A virtual IP, a load-balancer pool. **These are more dangerous than an
unrecorded host**: a host answers a ping, so someone taking its address gets a
warning. A VIP mid-failover answers nothing, so "is it free?" says yes about
the address a whole cluster depends on.

Stored by address, not MAC, because there is no MAC. A range uses `ip_end`
and covers every address inside it.

```json
{"ip":"10.0.0.240","ip_end":"10.0.0.244","name":"load-balancer pool",
 "notes":"advertised over ARP by whichever node answers; owned by no machine"}
```

Both are excluded from every free-address answer, and an import never drops
them. The examples above show the FIELDS THAT CARRY MEANING, not the full
shape — `GET /api/schema` is the authority on what is required and what is
allowed.

## Asking where an address is

`GET /api/vlans/{name}/free` returns addresses safe to hand out: inside the
subnet, outside the DHCP pool, not the gateway, not on any device, not
infrastructure, not reserved. Collapsed into ranges with a count. When there
is no answer, `reason` says why in words.

Searching the sheet: a bare number is **one octet, matched whole, in any
position** — `230` finds `192.168.1.230`, and `2` refuses `192.168.1.25`. A
trailing dot is an explicit prefix: `192.168.1.` is the whole /24.

## Traps that have actually bitten

- **A randomised MAC cannot hold a DHCP reservation.** The locally-administered
  bit (`int(mac[:2],16) & 2`) says so. Phones, tablets and laptops rotate it,
  and the reservation silently stops applying. cARPaccio flags it per row;
  the controller gives no hint. Do not "fix" such a reservation — say why it
  cannot work.
- **"No firewall rules" is not "no policy".** Segmentation can be enforced by a
  per-network isolation toggle that appears in no rule table. Check the VLAN's
  `isolated` flag before reporting a network as unsegmented — telling someone
  to rebuild a wall that is already standing is worse than saying nothing.
- **A UniFi VLAN cannot be renamed here** (409). Its name is the merge key on
  re-import; renaming would duplicate the segment. Rename it at the source.
- **A VLAN with devices cannot be deleted** (409, with the count). Move the
  devices first. Nothing is dropped silently.
- **An import never overwrites human knowledge.** Hand-added `static` rows,
  notes, names and the whole `reserved` section survive. You may write freely;
  the next sync will not eat it.

## When you are reconciling against reality

Compare cARPaccio against the sources the operator named (see above — if
nobody named any, that is the question to ask before starting) and write down
the difference. Two directions, and the second is the one that gets forgotten:

1. **Things that exist but are not recorded** — hosts missing from the sheet.
2. **Addresses in use that belong to no device** — VIPs, pools, anything
   advertised by whichever machine answers. These never show up by comparing
   host lists, because no host owns them.

Report what you changed and what you chose not to. An address you decided not
to record is a decision someone else will inherit.
