import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const distRoot = resolve(appRoot, "dist");
const indexPath = resolve(distRoot, "index.html");
const port = Number(process.env.CALYX_DASHBOARD_PREVIEW_PORT ?? "4173");
const host = process.env.CALYX_DASHBOARD_PREVIEW_HOST ?? "127.0.0.1";
const apiTarget = (process.env.CALYX_WEB_API_PROXY_TARGET ?? "http://127.0.0.1:8121").replace(/\/$/, "");
const bearer = process.env.CALYX_WEB_API_BEARER_SECRET?.trim();

if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  throw new Error("CALYX_DASHBOARD_PREVIEW_PORT must be a TCP port");
}
if (!existsSync(indexPath)) {
  throw new Error("dist/index.html is missing; run VITE_CALYX_WEB_API_BASE_URL=/api npm run build first");
}
if (!bearer) {
  throw new Error("CALYX_WEB_API_BEARER_SECRET is required for the deploy preview proxy");
}

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(payload));
}

function safeStaticPath(urlPath) {
  const decoded = decodeURIComponent(urlPath.split("?")[0]);
  const normalized = normalize(decoded === "/" ? "/index.html" : decoded);
  if (normalized.includes(`..${sep}`) || normalized === "..") {
    return null;
  }
  return resolve(join(distRoot, normalized));
}

async function proxyApi(request, response) {
  const upstreamPath = request.url.replace(/^\/api/, "") || "/";
  let upstream;
  try {
    upstream = new URL(upstreamPath, `${apiTarget}/`);
  } catch (error) {
    sendJson(response, 502, {
      code: "CALYX_DASHBOARD_PROXY_FAILED",
      message: `Invalid upstream URL: ${error.message}`,
      remediation: "Set CALYX_WEB_API_PROXY_TARGET to the loopback calyx-web-api origin.",
    });
    return;
  }

  const headers = { ...request.headers };
  headers.host = upstream.host;
  headers.authorization = `Bearer ${bearer}`;

  try {
    const upstreamResponse = await fetch(upstream, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method ?? "") ? undefined : request,
      duplex: "half",
    });
    const responseHeaders = Object.fromEntries(upstreamResponse.headers.entries());
    responseHeaders["cache-control"] = "no-store";
    response.writeHead(upstreamResponse.status, responseHeaders);
    if (upstreamResponse.body) {
      for await (const chunk of upstreamResponse.body) {
        response.write(chunk);
      }
    }
    response.end();
  } catch (error) {
    sendJson(response, 502, {
      code: "CALYX_DASHBOARD_PROXY_FAILED",
      message: `Unable to reach calyx-web-api: ${error.message}`,
      remediation: "Start calyx-web-api and verify CALYX_WEB_API_PROXY_TARGET.",
    });
  }
}

function serveStatic(request, response) {
  const candidate = safeStaticPath(request.url ?? "/");
  const filePath = candidate && candidate.startsWith(`${distRoot}${sep}`) ? candidate : null;
  const resolvedPath =
    filePath && existsSync(filePath) && statSync(filePath).isFile() ? filePath : indexPath;
  response.writeHead(200, {
    "content-type": contentTypes[extname(resolvedPath)] ?? "application/octet-stream",
    "cache-control": resolvedPath === indexPath ? "no-store" : "public, max-age=31536000, immutable",
  });
  createReadStream(resolvedPath).pipe(response);
}

const server = createServer((request, response) => {
  if (request.url?.startsWith("/api/") || request.url === "/api") {
    void proxyApi(request, response);
    return;
  }
  serveStatic(request, response);
});

server.listen(port, host, () => {
  console.log(
    JSON.stringify({
      url: `http://${host}:${port}/`,
      api_proxy_target: apiTarget,
      static_root: distRoot,
    }),
  );
});
