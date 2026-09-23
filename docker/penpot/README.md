# Penpot on ZimaOS

`docker-compose.zimaos.yml` is the ZimaOS/CasaOS-format stack for the Penpot
instance Odysseus designs are exported to (`http://homelab.nas:9001`).

## Why exports failed

Exporting (PNG/SVG/PDF) goes browser -> frontend nginx `/api/export` ->
`penpot-exporter:6061`, whose headless browser then loads the app again from
the frontend to render it. Two things in the ZimaOS export of the stack broke
that chain:

1. **No `PENPOT_SECRET_KEY` on the exporter.** Penpot 2.x's exporter config
   schema requires it (it derives the export-token key from it), so the
   container never came up and nginx returned `502 Bad Gateway` for every
   export.
2. **Wrong port for the internal frontend address.** The exporter was told
   `PENPOT_PUBLIC_URI=http://big-bear-penpot-frontend:9001`. Port 9001 only
   exists on the NAS; inside the Docker network the frontend listens on 8080.
   `PENPOT_INTERNAL_URI=http://penpot-frontend:8080` now names it explicitly.

The backend's public URI (used for links and cookies) and the frontend's
flags were also missing or internal-only; they are set to the real user-facing
URL now.

## `invalid port in upstream "penpot-mcp:4401:4402"`

The frontend was `penpotapp/frontend:latest`, and the NAS's cached `latest`
was older than 2.16. Before 2.16 the frontend's nginx template appended
`:4401`/`:4402` to `PENPOT_MCP_URI` itself. From 2.16 on it expects the full
URL with the port, which is what this file sets. The images are now pinned to
2.17.0, so the template and the env values match. Upgrading from an older
frontend/backend migrates the database forward on first boot. Take a copy of
`/DATA/AppData/big-bear-penpot/pgdata` first if you want a way back.

## Installing / updating on ZimaOS

1. Fill in the two placeholders (`__PENPOT_SECRET_KEY__` on backend and
   exporter, `__PENPOT_DB_PASSWORD__` on backend and postgres). The DB
   password must be the one the existing `pgdata` was created with; the
   secret key can be anything, as long as backend and exporter agree.
2. ZimaOS -> App Store -> **Install a customized app** -> **Import** (top
   right) -> paste the file.
3. Keep the app name `penpot-backend` so it replaces the existing app and
   reuses `/DATA/AppData/big-bear-penpot/{assets,pgdata}`.
4. Submit. Postgres and Redis come up first (healthchecks), then backend,
   exporter, frontend.

## MCP in Odysseus (always available, no browser)

Penpot's own MCP (`penpot-mcp` in this stack, or Account → Integrations → MCP
Server) sends every tool call to the MCP plugin running in an open Penpot tab.
When nobody has a file open with the plugin connected, it cannot do anything,
and there is no headless mode for it.

For an MCP that is always available, Odysseus runs
[`@zcubekr/penpot-mcp-server`](https://github.com/zcube/penpot-mcp-server) as a
**stdio** server. It talks to Penpot's RPC API with a personal access token,
so it needs no browser, plugin or sign-in:

1. This stack has `enable-access-tokens` in `PENPOT_FLAGS` (frontend and
   backend). After re-importing, open Penpot → **Your account → Access
   tokens** → create a token, with no expiry or a long one.
2. Odysseus → Settings → MCP servers → add a server:
   - Name: `penpot`
   - Transport: `stdio`
   - Command: `npx`
   - Args: `-y` `@zcubekr/penpot-mcp-server@1.0.0`
   - Env:
     - `PENPOT_API_URL=http://homelab.nas:9001` (origin only, no `/api` and
       no trailing slash; use the NAS IP if the Odysseus container cannot
       resolve `homelab.nas`)
     - `PENPOT_ACCESS_TOKEN=<token from step 1>`
3. Connect. Odysseus starts the process itself and restarts it whenever it
   reconnects, so it comes back after reboots without any clicks.

Why stdio and not a container on the NAS: in HTTP mode that server accepts
only one MCP session per process. After Odysseus restarts, it gets
`Server already initialized` until the container is restarted as well. It
would also publish your Penpot token on an unauthenticated port.

What each option can do:

| | Always available | Tools |
|---|---|---|
| Token server (stdio in Odysseus) | Yes | Projects, files, pages, create and update shapes (rectangles, frames, text, SVG), components, comments. Export is not implemented in 1.0.0. |
| Penpot's own MCP (`penpot-mcp`) | Only while a tab has the plugin connected | `execute_code` over the full plugin API, `export_shape`, `import_image` |

You can keep both connected. Odysseus uses the token server when no tab is
open.

## Verifying

```bash
# 502 = exporter still down. 401/400 = nginx reached the exporter (good).
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://homelab.nas:9001/api/export

# Frontend flags took effect if this prints a non-empty penpotFlags line.
curl -s http://homelab.nas:9001/js/config.js
```

From the ZimaOS terminal (or SSH) when something is still off:

```bash
docker logs --tail 100 big-bear-penpot-exporter
docker logs --tail 100 big-bear-penpot-backend
```

## Notes

- Secrets are never committed; only placeholders. Rotating
  `PENPOT_SECRET_KEY` (same value on backend and exporter) logs everyone out
  once, nothing else.
- `PENPOT_PUBLIC_URI` is `http://homelab.nas:9001`. If you open Penpot by IP
  instead, links in emails will still say `homelab.nas`; the app itself works.
- The token needs the frontend/backend flag `enable-access-tokens`. If
  Account → Access tokens is missing, the flag didn't take effect. Check
  `curl -s http://homelab.nas:9001/js/config.js`.
- The MCP service publishes 19400 (plugin server), 19401 (MCP HTTP `/mcp`),
  19402 (plugin websocket) on the NAS, and the frontend also proxies
  `/mcp/stream`, `/mcp/sse`, `/mcp/ws` on 9001 via the `enable-mcp` flag.
