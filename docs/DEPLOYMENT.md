# Running cARPaccio for real

`docker compose up` needs none of this. This is for putting it somewhere that
stays up.

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
repo, or defaults. Auth adds three more:

| Variable | Default | Purpose |
|---|---|---|
| `CARPACCIO_BASIC_USER` / `CARPACCIO_BASIC_PASSWORD_HASH` | — | see [Security](#security); both unset = auth off |
| `CARPACCIO_SESSION_SECRET` | — | signs the browser session cookie. Unset does not stop the boot: a random per-boot secret is used and a log warning says sessions will not survive a restart |
| `CARPACCIO_COOKIE_SECURE` | auto | force the session cookie's `Secure` flag (`true`/`false`). Auto = on exactly when the request arrived over HTTPS (`X-Forwarded-Proto` when the proxy sets it, else the request scheme) |


## Security

**Two doors, one lock.** Scripts and agents authenticate with HTTP Basic;
a browser gets a real login form at `/login` and carries a signed session
cookie afterwards (`HttpOnly`, `SameSite=Lax`, one-week expiry, `Secure`
whenever the request actually arrived over HTTPS). Both paths satisfy the
same middleware, and the split is precise: an unauthenticated **API-shaped**
request (path under `/api/`, or no `text/html` preference) always gets the
401 JSON with `WWW-Authenticate` — never a redirect, so an agent can never
mistake a login page for data. Only a browser navigation is sent to the form,
and returns to where it was heading. Log out with the header button; agents
on Basic are unaffected.

Set two environment variables and auth is on; leave either unset and it is
**off**, which is fine on a laptop but never silent — `/healthz` reports
`"auth": "off"` so an unauthenticated instance is a thing you can see rather
than a thing you have to remember.

```bash
CARPACCIO_BASIC_USER=waw
CARPACCIO_BASIC_PASSWORD_HASH='$2b$12$...'     # bcrypt, never a password
```

Generate the hash with `htpasswd -nbBC 12 <user> <password>` (take the part
after the colon) or:

```bash
python -c 'import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt(12)).decode())' 'your password'
```

**`/healthz`, `/login` and `/static/*` are deliberately open.** Kubernetes
liveness and readiness probes send no credentials; behind auth they get 401,
the pod is declared unhealthy, and a working app is restarted for ever.
`/login` must be reachable or nobody can ever log in, and `/static/*` is the
login page's own stylesheet — committed CSS/JS from a public repo, nothing to
guard.

**What Basic auth is and is not.** It is a password over TLS, appropriate for
one household on one LAN. It is not per-user identity, it has no audit trail
beyond the change history, and every caller shares one credential — including
any agent using the API. Do not put this on the public internet and consider
the password sufficient.

**The fixtures in this repo are synthetic.** `tests/fixtures/sanitise.py`
generates them; no real MAC, address or device name from anyone's network is
committed here. The real data lives only in object storage.



## Deploying to Kubernetes

The chart is in `charts/carpaccio`. The image is published to **GHCR** by
`.github/workflows/release.yml` when a `v*` tag is pushed: multi-arch
(amd64 + arm64), with build provenance and an SBOM attached.

```sh
git tag v0.8.0 && git push origin v0.8.0
# -> ghcr.io/wawrepo/carpaccio:v0.8.0  (+ :latest)
```

**The package is public even though this repository is not.** Those are two
separate settings on GHCR, and separating them is deliberate: nodes pull
anonymously with no pull secret, while the source stays private. Publishing
the code is a different decision and has not been taken.

Pin by digest in whatever deploys it — the workflow prints the exact line in
its run summary. A tag can be moved; a digest cannot, so what runs is what was
built and attested.

**A release shows up on a normal reload.** Every `/static` link carries a
fingerprint of the file's content (`app.js?v=<12 hex>`), so a changed file is
a new URL that can be cached for ever, and the pages themselves are sent with
`Cache-Control: no-cache`. Link static files through `static_url()` in the
templates, never as a bare `/static/...` path — a test fails if you do.

A Helm chart lives in `charts/carpaccio/`. The pod is stateless (all state is
in S3), so it carries no PVC and no nodeSelector and can schedule on any
node. S3 credentials are read from an existing Secret — see
`charts/carpaccio/values.yaml`.
