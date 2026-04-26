/**
 * Domain-Fronting Relay — Code.gs
 * ─────────────────────────────────────────────────────────────────────────────
 * CHANGE THE AUTH KEY BELOW TO YOUR OWN SECRET!
 */

const AUTH_KEY = "129266AmQn#";

// ── Header filter ─────────────────────────────────────────────────────────────
// Headers that must never be forwarded to the target (hop-by-hop / proxy-only).
const SKIP_HEADERS = new Set([
  "host", "connection", "content-length", "transfer-encoding",
  "proxy-connection", "proxy-authorization", "te", "trailer", "upgrade",
]);

// ── URL validation ────────────────────────────────────────────────────────────
// Compiled once; reused for every request.
const URL_RE = /^https?:\/\//i;

// ── Cache ─────────────────────────────────────────────────────────────────────
// Short-lived in-memory cache for idempotent GETs (survives within one
// script execution, ~30 s). Keeps frequently hit resources out of quota.
const _cache = {};
const CACHE_TTL_MS = 5000; // 5 s micro-cache

// ── Entry points ──────────────────────────────────────────────────────────────

function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    if (req.k !== AUTH_KEY) return _json({ e: "unauthorized" });

    if (Array.isArray(req.q)) return _doBatch(req.q);   // batch mode
    return _doSingle(req);                               // single mode
  } catch (err) {
    return _json({ e: String(err) });
  }
}

function doGet(e) {
  return HtmlService.createHtmlOutput(
    "<!DOCTYPE html><html><head>" +
    "<title>Relay</title>" +
    "<style>body{font-family:sans-serif;max-width:640px;margin:60px auto;" +
    "color:#222}h1{color:#1a73e8}code{background:#f1f3f4;padding:2px 6px;" +
    "border-radius:3px;font-size:.9em}</style></head><body>" +
    "<h1>✓ Relay Active</h1>" +
    "<p>This script relay is running normally.</p>" +
    "<p><code>POST</code> requests are authenticated and proxied to their targets.</p>" +
    "</body></html>"
  );
}

// ── Single request ────────────────────────────────────────────────────────────

function _doSingle(req) {
  if (!_validUrl(req.u)) return _json({ e: "bad url" });

  // Micro-cache for identical GETs within the same execution
  const cacheKey = req.m === "GET" && !req.b ? req.u : null;
  if (cacheKey) {
    const hit = _cacheGet(cacheKey);
    if (hit) return _json(hit);
  }

  const resp = UrlFetchApp.fetch(req.u, _buildOpts(req));
  const result = _encodeResponse(resp);

  if (cacheKey) _cachePut(cacheKey, result);
  return _json(result);
}

// ── Batch request ─────────────────────────────────────────────────────────────

function _doBatch(items) {
  if (!Array.isArray(items) || items.length === 0) {
    return _json({ e: "empty batch" });
  }

  const fetchList = [];   // { idx, opts } — only valid URLs
  const results   = new Array(items.length);

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (!_validUrl(item.u)) {
      results[i] = { e: "bad url" };
      continue;
    }

    // Serve from micro-cache if possible
    const cacheKey = item.m === "GET" && !item.b ? item.u : null;
    if (cacheKey) {
      const hit = _cacheGet(cacheKey);
      if (hit) { results[i] = hit; continue; }
    }

    fetchList.push({ idx: i, opts: _buildOpts(item), cacheKey });
  }

  // UrlFetchApp.fetchAll() runs all requests in parallel inside Google —
  // this is the main throughput win of batch mode.
  if (fetchList.length > 0) {
    const responses = UrlFetchApp.fetchAll(fetchList.map(x => x.opts));
    for (let j = 0; j < fetchList.length; j++) {
      const { idx, cacheKey } = fetchList[j];
      const result = _encodeResponse(responses[j]);
      results[idx] = result;
      if (cacheKey) _cachePut(cacheKey, result);
    }
  }

  return _json({ q: results });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Build a UrlFetchApp options object from a relay request.
 */
function _buildOpts(req) {
  const opts = {
    method:                   (req.m || "GET").toLowerCase(),
    muteHttpExceptions:       true,
    followRedirects:          req.r !== false,
    validateHttpsCertificates: true,
  };

  if (req.h && typeof req.h === "object") {
    const headers = {};
    for (const k in req.h) {
      if (Object.prototype.hasOwnProperty.call(req.h, k) &&
          !SKIP_HEADERS.has(k.toLowerCase())) {
        headers[k] = req.h[k];
      }
    }
    if (Object.keys(headers).length > 0) opts.headers = headers;
  }

  if (req.b) {
    opts.payload     = Utilities.base64Decode(req.b);
    opts.contentType = req.ct || "application/octet-stream";
  }

  return opts;
}

/**
 * Convert a UrlFetchApp HTTPResponse into the compact wire format.
 */
function _encodeResponse(resp) {
  return {
    s: resp.getResponseCode(),
    h: resp.getHeaders(),
    b: Utilities.base64Encode(resp.getContent()),
  };
}

/**
 * Validate that a URL is an absolute http(s) URL.
 * Blocks private/loopback ranges to prevent SSRF.
 */
function _validUrl(u) {
  if (!u || typeof u !== "string" || !URL_RE.test(u)) return false;
  // Block SSRF targets: localhost, 169.254.x.x (metadata), RFC-1918
  if (/^https?:\/\/(localhost|127\.|0\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)/i.test(u)) {
    return false;
  }
  return true;
}

/**
 * Tiny in-process LRU-ish cache (no CacheService quota used).
 * Entries expire after CACHE_TTL_MS milliseconds.
 */
function _cacheGet(key) {
  const entry = _cache[key];
  if (!entry) return null;
  if (Date.now() - entry.t > CACHE_TTL_MS) {
    delete _cache[key];
    return null;
  }
  return entry.v;
}

function _cachePut(key, value) {
  _cache[key] = { v: value, t: Date.now() };
}

/**
 * Serialize an object as a JSON ContentService response.
 */
function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}