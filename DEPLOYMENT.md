# VIDA Desktop Agent — Operational Guide

## Architecture

```
inventory-service (NestJS, server)
  → vida-agent (FastAPI, Docker, :8000)
    → WebSocket bridge (:8765)
      ← Traefik TLS (piglet.genesisailab.com:443)
        ← piglet.exe (Windows desktop)
          → VIDA application
```

## Components

| Component | Description |
|---|---|
| **piglet** | Zig binary on Windows desktop. Captures screenshots, controls mouse/keyboard. [GitHub releases v0.0.7](https://github.com/pig-dot-dev/piglet/releases/tag/v0.0.7). |
| **vida-agent** | Python FastAPI service in Docker. Orchestrates VIDA automation via Claude AI. |
| **Traefik** | Reverse proxy handling TLS termination for the WebSocket tunnel. |
| **inventory-service** | Volvo tenant NestJS service that calls vida-agent for part lookups. |

## Server Setup (already deployed)

**Deploy vida-agent:**
```bash
cd /root/repos/quote-commerce
docker compose -f tenants/docker-compose.tenant.yml \
  --env-file tenants/.env.volvo -p volvo \
  --profile vida up -d --build vida-agent
```

Container name: `volvo-vida-agent`, profile: `vida`.

**TLS certificate:**
- Issued via certbot (not Traefik's ACME resolver), injected into Traefik's `acme.json`.
- Must be **RSA** (not ECDSA) — piglet's Zig TLS implementation requires it.
- Renewal: `certbot renew` (auto-scheduled), then re-inject into `acme.json` and restart Traefik.
- Manual re-issue:
  ```bash
  certbot certonly --dns-cloudflare \
    --dns-cloudflare-credentials /root/.cloudflare/credentials.ini \
    -d piglet.genesisailab.com --force-renewal
  ```

**DNS:** `piglet.genesisailab.com` — Cloudflare DNS-only (NOT proxied).
Cloudflare API token for DNS challenges: `CF_DNS_API_TOKEN` in `/root/repo/pulse.tenant/tenants/.env`.

## Windows Desktop Setup

**1. Install piglet:**
```powershell
$toolDir = "$env:USERPROFILE\.piglet"
New-Item -ItemType Directory -Force -Path $toolDir
Invoke-WebRequest -Uri "https://github.com/pig-dot-dev/piglet/releases/download/v0.0.7/piglet.exe" -OutFile "$toolDir\piglet.exe"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$toolDir*") {
    [Environment]::SetEnvironmentVariable("Path", $userPath + ";" + $toolDir, "User")
}
```

**2. Connect to server:**
```powershell
piglet join --host piglet.genesisailab.com --secret vida_bridge_volvo_s3cr3t
```

**3. Keep VIDA application open on the desktop.**

## Configuration

**`.env.volvo`** (quote-commerce tenant):
```
VIDA_ENABLED=true
VIDA_AGENT_SERVICE_URL=http://vida-agent:8000
VIDA_BRIDGE_SECRET=vida_bridge_volvo_s3cr3t
VIDA_BRIDGE_DOMAIN=piglet.genesisailab.com
```

**vida-agent env:**
```
ANTHROPIC_API_KEY=<key>
BRIDGE_PORT=8765
BRIDGE_SECRET=vida_bridge_volvo_s3cr3t
API_PORT=8000
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/vida/health` | Returns piglet connection status |
| `POST` | `/api/vida/search-parts` | `{"query": "brake pad", "max_steps": 30}` |
| `POST` | `/api/vida/calibrate` | `{"screen_name": "HOME"}` |

## Local Development (without tunnel)

Set `PIGLET_DIRECT_URL=http://localhost:3000` in vida-service `.env`, then:
```bash
piglet start --port 3000
```
The `DirectPigletClient` bypasses the WebSocket tunnel entirely.

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `"no_piglet"` in health response | piglet not connected. Verify it's running on the desktop and can reach `piglet.genesisailab.com:443`. |
| WebSocket 400 errors | Duplicate `Sec-WebSocket-Version` header — fixed in `ws_bridge.py` `process_request` hook. |
| TLS handshake failures | Cert may be ECDSA. Re-issue as RSA and re-inject into `acme.json`. |
| Cert expired | Run certbot re-issue command above, re-inject into `acme.json`, restart Traefik. |
