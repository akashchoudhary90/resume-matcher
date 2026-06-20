# Demo on the same VPS as `ati` — separate, zero-touch, trusted cert at :8443

Runs the synthetic-data demo on the **same box** that serves the Kotak trading API, with a **trusted
HTTPS cert** (no browser warning), while leaving the trading stack **completely untouched**.

- URL: **`https://schulich.edufund.ca:8443`** (its own port — 80/443 stay with the trading Caddy)
- Cert: real Let's Encrypt, obtained via **Hostinger DNS-01** (a TXT record), so **no port 80/443
  needed** → ati's Caddy is never touched.
- Isolation: its own Caddy + app, own network `rmdemo_net`, own volumes, capped at 512 MB / 0.5 CPU.

## What it does NOT touch (by design)

- ❌ Does not edit the trading `docker-compose.yml` or `deploy/caddy/Caddyfile`.
- ❌ Does not add a network to the trading Caddy, and does not use ports 80 or 443.
- ❌ Does not share the trading `.env`, secrets, network, or volumes.

## Prerequisites

1. **Hostinger API token** — `edufund.ca` DNS is at Hostinger. In the Hostinger panel, create an API
   token with **DNS management** permission. (Caddy uses it only to write the temporary
   `_acme-challenge.schulich.edufund.ca` TXT record, then renews the same way.)
2. **DNS A record** — add `schulich` → the VPS public IP (`178.105.138.215`, same box as `ati`).
   Confirm: `nslookup schulich.edufund.ca`.
3. **Firewall** — open `8443/tcp` on the VPS firewall and the cloud security group:
   ```bash
   sudo ufw allow 8443/tcp        # if you use ufw
   ```

## Deploy

From the Resume Matching repo root on the VPS:
```bash
export RM_ADMIN_PASSWORD='a-strong-password'                 # the site login
export HOSTINGER_API_TOKEN='<your Hostinger DNS API token>'  # for the trusted cert
docker compose -f deploy/cohost/docker-compose.cohost.yml -p rmdemo up -d --build
```

First boot builds a small custom Caddy image (stock Caddy + the Hostinger DNS plugin) and obtains the
certificate via DNS-01 — watch it happen:
```bash
docker compose -f deploy/cohost/docker-compose.cohost.yml -p rmdemo logs -f caddy
# look for: "certificate obtained successfully" for schulich.edufund.ca
```

**Verify** (no `-k` needed — the cert is trusted):
```bash
curl -u admin:'a-strong-password' https://schulich.edufund.ca:8443/api/health   # {"status":"ok"}
```
Open `https://schulich.edufund.ca:8443` → log in (`admin` / your password). The cert auto-renews.

> The trading stack is never started, stopped, or reconfigured by any of this. The only thing that
> briefly appears in your Hostinger DNS zone is a short-lived `_acme-challenge` TXT record that Caddy
> adds and removes automatically.

## Real-data demo (`/demo`) on this box

The deployed app also serves the **ephemeral "try it with your own data"** flow at
`https://schulich.edufund.ca:8443/demo` — upload 1 job posting + up to 10 resumes and get fully
explained scores. It is **enabled by default** and needs no compose change; just rebuild so the image
picks up the upload/parsing deps (`python-multipart`, `pypdf`, `python-docx`, added to the Dockerfile):

```bash
docker compose -f deploy/cohost/docker-compose.cohost.yml -p rmdemo up -d --build
```

**Privacy posture (decided 2026-06-20):** resumes are parsed **in memory only, never written to
disk**; the full text is **dropped right after scoring**; sessions **auto-delete after 30 min idle**
and clients can **"Delete my data now"** on demand. See **[../../PRIVACY.md](../../PRIVACY.md)**. This
is the conscious, ephemeral exception to "synthetic-only on this VPS" — real PII transits RAM briefly
and is never persisted. For ongoing real-student use, move to a York-controlled host (below).

Tunables (set in the `app` service `environment:` if you want to change defaults):
`RM_DEMO_ENABLED` (set `0` to make this host synthetic-only again), `RM_DEMO_TTL_MINUTES`,
`RM_DEMO_MAX_RESUMES`, `RM_DEMO_MAX_FILE_MB`, `RM_DEMO_MAX_SESSIONS`, `RM_DEMO_BACKEND`.

> Memory headroom: the stack is capped at 512 MB. Up to 10 resumes in RAM (text dropped after
> scoring) is tiny; the cap is not a concern for the demo.

## Fallbacks (if the DNS plugin ever misbehaves)

- **Quick self-signed:** in `Caddyfile`, replace the whole `tls { ... }` block with `tls internal`,
  drop `HOSTINGER_API_TOKEN`, and redeploy. Works instantly; browser shows a one-time warning.
- **acme.sh sidecar:** `dns_hostinger` (env `HOSTINGER_Token`) issues the cert to a volume and Caddy
  serves it with `tls /cert.pem /key.pem`. Heavier but very mature. Ask and I'll wire it.

## Update

**Manual** (after a `git push` from your laptop):
```bash
cd ~/resume-matcher && git pull && docker compose -f deploy/cohost/docker-compose.cohost.yml -p rmdemo up -d --build
```

**Hands-off (recommended)** — install the auto-deploy timer once; then every `git push` goes live on
its own within ~3 minutes:
```bash
sudo bash deploy/cohost/install-autodeploy.sh
```
It pulls with the read-only deploy key and rebuilds **only** the `rmdemo` project — never the trading
stack. Watch it: `tail -f /var/log/rmdemo-autodeploy.log` · status: `systemctl list-timers rmdemo-autodeploy.timer`.
Requires `deploy/cohost/.env` (see `.env.example`).

## Delete it completely (zero residue)
```bash
docker compose -f deploy/cohost/docker-compose.cohost.yml -p rmdemo down -v
# removes the demo's containers, its rmdemo_net network, and its volumes (including the cert).
# Then: remove the schulich DNS record and close port 8443. Nothing of `ati` was ever changed.
```

## Move to the real host later
Re-point `schulich` DNS to the York-controlled host and run the **standalone**
[docker-compose.yml](../../docker-compose.yml) there (it brings its own Caddy on 80/443 with normal
Let's Encrypt — clean `https://schulich.edufund.ca`, no port). Reminder: real student PII only goes on
that governance-cleared institutional host — never this personal VPS.
