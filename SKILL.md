---
name: carpaccio
description: Read and maintain a network inventory — VLANs, devices, DHCP ranges, reserved addresses and firewall rules — over cARPaccio's HTTP API. Use when asked where an address is, whether one is free, to record a host, or to reconcile the document against reality.
---

# cARPaccio

A document describing a network, and an API built for you to maintain it.
The human reads the web UI. You do the writing.

## The one thing to understand first

**The document is the truth, and it is only as true as the last thing written
into it.** cARPaccio does not discover anything. It does not poll, scan or
watch. Everything in it was put there by an importer, a human, or you.

So the failure mode is never a wrong answer — it is a *confident* answer about
something nobody wrote down. `GET /api/vlans/home/free` will happily offer an
address that a machine is using right now, if nothing ever recorded that
machine. Keeping the document current IS the job; answering questions from it
is the easy part.

## Connect

```
BASE=https://carpaccio.example.internal
curl -sf -u "$USER:$PASS" "$BASE/api/network"
```

HTTP Basic. Credentials come from the environment — never a flag, never a
file in the repo. An unauthenticated API call gets a **401 with JSON**, never
an HTML redirect, so a failure looks like a failure and not like a page.

Start with `GET /api/schema`. It is the schema the server actually validates
against, so it is never out of date with the thing rejecting your write.

## The write contract, which is the whole API

1. Every read hands you a **version token**.
2. Every write must present it.
3. A stale token is **409**, with a description of what changed.
4. **There is no server-side retry.** On 409: re-read, redo, write again.

That is deliberate. Last-writer-wins on a firewall table is how a VLAN quietly
stops being separated.

```sh
# whole document
curl -sf -u "$USER:$PASS" "$BASE/api/network"        # -> {version, document}
curl -sf -u "$USER:$PASS" -X PUT "$BASE/api/network" \
     -H 'content-type: application/json' \
     -d '{"version":"<token>","document":{...}}'

# one resource — same conflict behaviour, less to send
curl -sf -u "$USER:$PASS" -X POST "$BASE/api/devices" \
     -H 'content-type: application/json' -d '{...device...}'

# pin a write to a version you read earlier
curl -sf -u "$USER:$PASS" -X PATCH "$BASE/api/devices/$MAC" \
     -H "If-Match: <token>" -H 'content-type: application/json' -d '{...}'
```

Per-resource writes read, change one thing, and save against the version they
just read. Prefer them. Send the whole document only when you are changing
many things at once.

An invalid document is **422** and the stored object is left untouched. You
cannot corrupt it with a bad write; you can only fail.

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
them.

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

The useful sweep is comparing cARPaccio against a list you trust — an
inventory file, a cluster's node list, a DHCP config — and writing down the
difference. Two directions, and the second is the one that gets forgotten:

1. **Things that exist but are not recorded** — hosts missing from the sheet.
2. **Addresses in use that belong to no device** — VIPs, pools, anything
   advertised by whichever machine answers. These never show up by comparing
   host lists, because no host owns them.

Report what you changed and what you chose not to. An address you decided not
to record is a decision someone else will inherit.
