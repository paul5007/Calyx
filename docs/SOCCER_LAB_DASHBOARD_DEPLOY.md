# Soccer Lab dashboard deployment

The Soccer Lab dashboard is a static React build. The browser calls a same-origin
`/api` path, and the deployment proxy forwards that path to the loopback
`calyx-web-api` origin while injecting the bearer secret. Do not expose
`CALYX_WEB_API_BEARER_SECRET` through `VITE_` environment variables in a
public build.

## Verified local deployment URL

The local deployment preview used for validation is:

```text
http://127.0.0.1:4173/
```

It serves `apps/soccer-lab-dashboard/dist` and proxies `/api/*` to:

```text
http://127.0.0.1:8121
```

## Build

From `apps/soccer-lab-dashboard`:

```bash
VITE_CALYX_WEB_API_BASE_URL=/api npm run build
```

`VITE_CALYX_WEB_API_BASE_URL=/api` is the deploy-safe value because it keeps the
browser on same-origin requests. The built JavaScript does not need a bearer
secret.

## Origin API

Start `calyx-web-api` on loopback with the real vault and prediction export:

```bash
CALYX_WEB_API_VAULT_DIR=/path/to/calyx_home/vaults/01KWND5F2PJ4ZMB9V8BMZDH44T \
CALYX_WEB_API_VAULT_NAME=soccer-rebuild-search-index \
CALYX_WEB_API_PREDICTION_EXPORT=/path/to/Calyx/docs/data/soccer_lab_prediction_export.json \
CALYX_WEB_API_BEARER_SECRET=<origin-shared-secret> \
CALYX_WEB_API_CACHE_TTL_SECS=0 \
/path/to/Calyx/target/debug/calyx-web-api
```

See `docs/SOCCER_LAB_WEB_API_ENV.md` for the full origin contract.

## Local deployment preview

After building, run the production-like static server:

```bash
CALYX_WEB_API_PROXY_TARGET=http://127.0.0.1:8121 \
CALYX_WEB_API_BEARER_SECRET=<origin-shared-secret> \
CALYX_DASHBOARD_PREVIEW_PORT=4173 \
npm run serve:deploy-preview
```

Then verify the deployed static app and proxy path:

```bash
CALYX_DASHBOARD_PREVIEW_URL=http://127.0.0.1:4173 npm run verify:deploy-preview
```

The preview server intentionally fails startup if `dist/index.html` or
`CALYX_WEB_API_BEARER_SECRET` is missing.

## Production proxy shape

Serve the static `dist` directory from the public hostname. Route `/api/*` to
the private `calyx-web-api` origin, strip the `/api` prefix, and set:

```text
Authorization: Bearer <CALYX_WEB_API_BEARER_SECRET>
```

The origin should remain loopback-only or private-network-only. TLS, client
identity, request policy, and internet exposure belong at the reverse proxy,
edge Worker, or equivalent deployment boundary.
