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
- The MCP service publishes 19400 (plugin server), 19401 (MCP HTTP `/mcp`),
  19402 (plugin websocket) on the NAS, and the frontend also proxies
  `/mcp/stream`, `/mcp/sse`, `/mcp/ws` on 9001 via the `enable-mcp` flag.
  That official MCP drives the Penpot plugin, so it needs a browser tab open.

## Odysseus MCP without a browser tab

Odysseus talks to Penpot's HTTP API instead, through the stdio server
`@zcubekr/penpot-mcp-server` (Settings > MCP):

```
command: npx
args:    -y @zcubekr/penpot-mcp-server
env:     PENPOT_API_URL=http://192.168.1.122:9001
         PENPOT_ACCESS_TOKEN=<Penpot > Your account > Access tokens>
```

- `PENPOT_API_URL` is the base URL only: no trailing `/api` (that becomes
  `/api/api/rpc`, a 404) and plain `http` (9001 is not TLS).
- The server runs **inside the Odysseus container**, so `localhost:9001` is
  the Odysseus container, not the NAS. A green "connected" dot only means the
  process started and listed its tools; it never contacts Penpot. An
  unreachable URL shows up on the first tool call as
  `Tool execution failed: fetch failed`. A bad token shows up as an HTTP 401
  (`authentication-required`) instead.
- Test with `list_teams`, not `get_profile`: Penpot answers `get-profile` for
  anyone, with "Anonymous User" when the token is missing or wrong.
- The backend needs `enable-access-tokens` in `PENPOT_FLAGS` (it is in the
  compose above), or it ignores the token and every call is a 401.

Check what the Odysseus container can reach, from the ZimaOS terminal:

```bash
C=$(docker ps --filter publish=7000 --format '{{.Names}}' | head -1); echo "container: $C"
for u in http://192.168.1.122:9001 http://homelab.nas:9001 http://host.docker.internal:9001 http://localhost:9001; do
  docker exec "$C" node -e 'const u=process.argv[1];fetch(u+"/api/rpc/command/get-teams",{method:"POST",headers:{"Content-Type":"application/json",Accept:"application/json"},body:"{}",signal:AbortSignal.timeout(8000)}).then(r=>console.log(u,"-> HTTP",r.status,r.status===401?"(reachable; 401 is expected without a token)":"")).catch(e=>console.log(u,"-> FAIL:",e.message,(e.cause&&(e.cause.code||e.cause.message))||""))' "$u"
done
```

Use the first URL that prints `HTTP 401` as `PENPOT_API_URL`.
