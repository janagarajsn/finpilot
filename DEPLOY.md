# Deploying FinPilot

The production setup is the normal stack plus `docker-compose.prod.yml`. It adds **Caddy** (automatic HTTPS) and makes sure nothing else is reachable from outside.

| Reachable from the internet | Internal only |
|---|---|
| Caddy on 80/443 (HTTP → HTTPS redirect, HTTP/2 + HTTP/3), proxying to the app and, if you want, to Langfuse | app, worker, beat, fx-mcp, Postgres, Redis, the Langfuse services |

In prod mode (`APP_ENV=prod`) the app refuses to start unless two things hold:
- `APP_BASE_URL` is `https://…`.
- `SECRET_KEY` is at least 32 random characters.

It then sends `Secure` cookies and HSTS. The app trusts `X-Forwarded-For` only because Caddy is the sole way in (`FORWARDED_ALLOW_IPS=*` in the prod override), so rate limits and lockouts see the real client IP.

> Verified locally before release: `APP_DOMAIN=localhost` with Caddy's internal CA gave HTTPS over HTTP/2, HSTS, Secure cookies, an HTTP → HTTPS redirect, and the real client IP in the app log. A full `make db-restore` into the prod stack was also tested (see *Backups*).

## 1. A single VPS (recommended)

**Requirements**
- Ubuntu 22.04/24.04 (or any Linux with Docker Engine ≥ 24 and Compose v2.24+, needed for `!reset`).
- 4 GB RAM with the self-hosted Langfuse stack (ClickHouse is the heavy part), or 2 GB with Langfuse Cloud.
- A domain whose A/AAAA record points to the host.
- Ports 80 and 443 open.

**Steps**
```bash
git clone <your fork> finpilot && cd finpilot
cp .env.example .env
make prod-secrets            # prints fresh values for every secret
$EDITOR .env                 # paste them over the defaults, and set MESH_API_KEY,
                             # APP_DOMAIN=finpilot.example.com, ACME_EMAIL=you@example.com
make prod-check              # refuses dev defaults / weak values
make prod-up                 # = docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile prod up -d --build
```

**After the first start**
- Caddy obtains a certificate on the first request: open `https://APP_DOMAIN`.
- Migrations run automatically when the app container starts (`alembic upgrade head`).
- The **first account registered becomes the admin**. Register yourself right away (or list your address in `ADMIN_EMAILS`).
- Optional demo accounts (admin@, inr@, usd@, mixed@finpilot.local): `docker compose exec -e DEMO_PASSWORD='<strong value>' app python -m scripts.seed_demo`. It refuses the default dev password in production. The prod database is not published to the host, so `make seed-demo` (which runs on the host) does not apply.

**Upgrades**: `git pull && make prod-up`. Images are rebuilt and migrations run on start. Take a backup first (below).

**Stop**: `make prod-down`. Volumes are kept.

### Langfuse in production
Pick one of three options:
- **Self-hosted, internal (default):** Langfuse runs and receives traces, but its UI is not reachable from outside. Use the next option when you want the UI.
- **Self-hosted, exposed:** set `LANGFUSE_DOMAIN=langfuse.example.com` (DNS → this host). Caddy serves it with its own certificate. Sign in with `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD` from `.env`.
- **Langfuse Cloud:** set `LANGFUSE_HOST_INTERNAL=https://cloud.langfuse.com` (or the US region), plus `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` from your Cloud project. Then start only FinPilot's services:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile prod \
    up -d --build caddy app worker beat fx-mcp finpilot-postgres finpilot-redis
  ```

The app never fails because Langfuse is down. Tracing is a no-op when the host is unreachable.

### Delivery channels (optional)
- **Email:** set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`. Without them, mail goes to `outbox/` (the log sink).
- **Telegram:**
  1. Create a bot with @BotFather and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME`.
  2. Get the webhook path secret (it is derived from `SECRET_KEY`):
     ```bash
     docker compose exec app python -c "from finpilot.delivery.telegram import webhook_secret; print(webhook_secret())"
     ```
  3. Register the webhook:
     ```bash
     curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://$APP_DOMAIN/telegram/webhook/<secret>"
     ```
  4. Without a public URL, set `TELEGRAM_POLLING=true` instead; a beat task polls `getUpdates` every minute.

## 2. Backups

All durable state lives in Postgres. Redis holds caches, locks and LangGraph checkpoints of in-flight conversations, which can be rebuilt or are disposable.

| Command | What it does |
|---|---|
| `make db-backup` | `pg_dump -Fc` of the `finpilot` database → `backups/finpilot-<timestamp>.dump` |
| `make db-backup DB=langfuse` | the same for the Langfuse metadata DB (traces themselves live in ClickHouse) |
| `make db-verify-backup FILE=backups/x.dump` | restores into a scratch database, compares row counts of the main tables with the live DB, then drops the scratch DB |
| `make db-restore FILE=backups/x.dump` | stops app/worker/beat, restores in one transaction (`--clean --if-exists`), starts them again |

**On the prod stack**, point the targets at the prod compose files:
```bash
make db-backup DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile prod"
```

**Schedule backups and copy them off the host.** For example, in the host's crontab:
```cron
15 2 * * * cd /srv/finpilot && make db-backup DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile prod" >> backups/backup.log 2>&1 && find backups -name '*.dump' -mtime +14 -delete
```
Then sync `backups/` to object storage (`rclone`, `aws s3 sync`, …) and run `make db-verify-backup` on a recent file now and then.

**Disaster recovery on a new host:**
1. Clone, restore `.env` (keep the same `SECRET_KEY`, or every session and Telegram link becomes invalid) and `make prod-up`.
2. The fresh volume runs `deploy/postgres/init.sh`, which creates the roles.
3. `make db-restore FILE=… DC="…prod…"`.

**Volumes** (`docker volume ls`):
- `finpilot_pg_data`: the one that matters.
- `finpilot_redis_data`.
- `caddy_data`: certificates; keep it to avoid rate limits from re-issuing.
- The `langfuse_*` volumes.

## 3. Render / Fly.io / other PaaS (notes)

Requirements on any platform:
- **Postgres 16 with `vector`, `pg_trgm` and `citext`**, and the roles from `deploy/postgres/init.sh`:
  - `finpilot_app`: no RLS bypass. The app connects with it.
  - `finpilot_admin`: BYPASSRLS, for system jobs.
  - The owner role: migrations.

  Managed providers that do not allow `BYPASSRLS` roles cannot run the scheduler's cross-user ticks.
- **Redis 8** (or Redis Stack). The LangGraph checkpointer needs the built-in query engine (RediSearch), and it must use DB 0.
- **Services:** `app` (web; the start command runs migrations), `worker`, exactly **one** `beat`, and `fx-mcp` (private).
- **Proxy:** set `FORWARDED_ALLOW_IPS` to the platform's proxy range (or `*` if the app is only reachable through it), `APP_ENV=prod`, `APP_BASE_URL=https://…` and a strong `SECRET_KEY`.
- **Fly.io:** one app per process group (`[processes] app/worker/beat`), Fly Postgres with the extensions enabled, Upstash or a Redis 8 machine, and a private `.internal` URL for fx-mcp (`FX_MCP_URL`).
- **Render:** a web service, two background workers (worker, beat) and a private service (fx-mcp), plus Render Postgres (pgvector is supported). Redis needs RediSearch, so use Redis Stack/8 on a private service rather than Render Key Value.

## 4. Security checklist

- [ ] `make prod-check` passes; `.env` is readable only by the deploy user (`chmod 600 .env`) and never committed.
- [ ] Only ports 80/443 are open on the host firewall.
- [ ] `ADMIN_EMAILS` (or the first registration) is you; register before announcing the URL.
- [ ] Backups are scheduled, copied off-host and restore-tested (`make db-verify-backup`).
- [ ] Mesh spending limits are set on the API key; Langfuse shows cost per model.
- [ ] Keep `SECRET_KEY` stable. Rotating it signs everyone out and invalidates Telegram link URLs.
