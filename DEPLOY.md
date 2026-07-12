# Deploying Resume Matcher to `schulich.edufund.ca`

This deploys the **synthetic-data demo**, gated behind an admin password. It is appropriate because
no real student data is involved. **Do not load real student PII on this host** — that requires
York-side governance (FIPPA notice, PIA, consent) and an institution-controlled deployment first
(see "Before real data" below).

---

## What you need

- A Linux server (a VM / EC2 instance) you control, reachable on ports **80** and **443**.
- DNS control for `edufund.ca` (you have this — it's your domain).
- Docker + Docker Compose on the server.

`edufund.ca` currently resolves to an AWS IP (`3.13.95.135`). You can deploy on that box **or** a
separate instance — just point the subdomain at whichever one runs this app.

---

## Step 1 — DNS: create the subdomain

In your `edufund.ca` DNS zone, add an **A record**:

| Type | Name       | Value (server public IP)        | TTL  |
|------|------------|---------------------------------|------|
| A    | `schulich` | `<your server's public IPv4>`   | 300  |

This makes `schulich.edufund.ca` resolve to your server. Wait for it to propagate
(`nslookup schulich.edufund.ca` should return your IP) **before** step 3 — Caddy needs working DNS to
obtain the TLS certificate.

> If you deploy on the same EC2 box as `edufund.ca`, use that instance's public IP. Make sure its
> **security group** allows inbound **80** and **443**.

## Step 2 — Get the code on the server

```bash
git clone <your repo>   # or scp the project directory up
cd "Resume Matching"
```

## Step 3 — Deploy (Docker + automatic HTTPS)

```bash
export RM_ADMIN_PASSWORD='choose-a-strong-password'   # the admin login for the whole site
export RM_ADMIN_USER='admin'                          # optional, defaults to "admin"
docker compose up -d --build
```

That starts two containers:
- **app** — the FastAPI app (Mock backend, synthetic data auto-loaded), password-gated.
- **caddy** — reverse proxy that fetches a Let's Encrypt certificate for `schulich.edufund.ca`
  automatically and serves it over HTTPS.

Open **https://schulich.edufund.ca** — your browser will prompt for the admin username/password.

Check logs / status:
```bash
docker compose logs -f caddy      # watch certificate issuance
docker compose ps
```

---

## Configuration (environment variables)

| Variable               | Default | Purpose                                                        |
|------------------------|---------|----------------------------------------------------------------|
| `RM_ADMIN_PASSWORD`    | (unset) | **Required for deploy.** If unset, the app runs OPEN (no auth). |
| `RM_ADMIN_USER`        | `admin` | Admin username for Basic auth.                                 |
| `RM_INFERENCE_BACKEND` | `mock`  | LLM backend. Keep `mock` for the demo (no model needed).       |
| `RM_AUTOLOAD`          | `1`     | Pre-load the synthetic dataset on startup so the page isn't empty. |
| `RM_HOST` / `RM_PORT`  | `0.0.0.0` / `8000` | Bind address inside the container.                  |

To change the hostname, edit the domain line in [deploy/Caddyfile](deploy/Caddyfile).

**Platform (Handshake replacement, docs/PLATFORM.md):** `RM_PLATFORM_ENABLED=1` mounts
`/employer`, `/coordinator`, and the postings API (default **off** — deploys stay demo-only until
you flip it deliberately). Before flipping on a real host: (1) make the DB path persistent
(`RM_PLATFORM_DB` or the existing `RM_ACCOUNTS_DB` volume — the schema migrates in place),
(2) seed a coordinator inside the container:
`python scripts/create_user.py you@york.ca --password … --role coordinator`,
(3) note the platform routes carry their own per-user auth and are exempt from the shared admin
gate while the flag is on.

---

## Updating

```bash
git pull
docker compose up -d --build
```

## Quick local test (no DNS/TLS)

```bash
docker build -t resume-matcher .
docker run --rm -p 8000:8000 -e RM_ADMIN_PASSWORD=test resume-matcher
# open http://127.0.0.1:8000  (login admin / test)
```

---

## Security notes (read these)

- **Synthetic data only.** This deployment is a demo. The dashboard's data comes from
  `scripts/gen_synthetic.py` — fake students. Do not upload real resumes here.
- **Admin password + HTTPS** are the perimeter. Use a strong password; rotate it; never commit it
  (pass it via the environment, not the repo).
- **Single process by design.** App state is in-memory, so the container runs one worker. If you
  later need multiple workers / replicas, externalize state (DB/cache) first.
- **The `/api/score` path does not re-run redaction** (redaction happens at ingestion). Keep that in
  mind before wiring any external caller to it.

### Before real data (do NOT skip)

Hosting real York student PII — on this domain or any other — first requires, per the project plan:
FIPPA notice of collection + a Privacy Impact Assessment + student consent; an institution-controlled
host (e.g. a `*.yorku.ca` deployment via York/Schulich IT) rather than a personal domain; a
data-processing/hosting agreement; real authentication (SSO) and encryption at rest; and audit
logging. Until all of that exists, keep this to synthetic data.
