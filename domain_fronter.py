import asyncio
import base64
import gzip
import json
import logging
import os
import re
import ssl
import time
from urllib.parse import urlparse

from ws import ws_encode, ws_decode

log = logging.getLogger("Fronter")


class DomainFronter:
    def __init__(self, config: dict):
        mode = config.get("mode", "domain_fronting")

        if mode == "custom_domain":
            domain = config["custom_domain"]
            self.connect_host = domain
            self.sni_host = domain
            self.http_host = domain
        elif mode == "google_fronting":
            self.connect_host = config.get("google_ip", "216.239.38.120")
            self.sni_host = config.get("front_domain", "www.google.com")
            self.http_host = config["worker_host"]
        elif mode == "apps_script":
            self.connect_host = config.get("google_ip", "216.239.38.120")
            self.sni_host = config.get("front_domain", "www.google.com")
            self.http_host = "script.google.com"
            script = config.get("script_ids") or config.get("script_id")
            self._script_ids = script if isinstance(script, list) else [script]
            self._script_idx = 0
            self.script_id = self._script_ids[0]
            self._dev_available = False
        else:
            self.connect_host = config["front_domain"]
            self.sni_host = config["front_domain"]
            self.http_host = config["worker_host"]

        self.mode = mode
        self.worker_path = config.get("worker_path", "")
        self.auth_key = config.get("auth_key", "")
        self.verify_ssl = config.get("verify_ssl", True)

        self._pool: list[tuple[asyncio.StreamReader, asyncio.StreamWriter, float]] = []
        self._pool_lock = asyncio.Lock()
        self._pool_max = 50
        self._conn_ttl = 45.0
        self._semaphore = asyncio.Semaphore(50)
        self._warmed = False
        self._refilling = False
        self._pool_min_idle = 15
        self._maintenance_task: asyncio.Task | None = None

        self._batch_lock = asyncio.Lock()
        self._batch_pending: list[tuple[dict, asyncio.Future]] = []
        self._batch_task: asyncio.Task | None = None
        self._batch_window_micro = 0.005
        self._batch_window_macro = 0.050
        self._batch_max = 50
        self._batch_enabled = True

        self._coalesce: dict[str, list[asyncio.Future]] = {}

        self._h2 = None
        if mode == "apps_script":
            try:
                from h2_transport import H2Transport, H2_AVAILABLE
                if H2_AVAILABLE:
                    self._h2 = H2Transport(
                        self.connect_host, self.sni_host, self.verify_ssl
                    )
                    log.info("HTTP/2 multiplexing available — "
                             "all requests will share one connection")
            except ImportError:
                pass

    # ── SSL ───────────────────────────────────────────────────────

    def _ssl_ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # ── Connection pool ───────────────────────────────────────────

    async def _open(self):
        return await asyncio.open_connection(
            self.connect_host, 443,
            ssl=self._ssl_ctx(),
            server_hostname=self.sni_host,
        )

    async def _acquire(self):
        """Pop a live connection from the pool or open a new one."""
        now = asyncio.get_event_loop().time()
        async with self._pool_lock:
            while self._pool:
                reader, writer, created = self._pool.pop()
                if (now - created) < self._conn_ttl and not reader.at_eof():
                    asyncio.create_task(self._add_conn_to_pool())
                    return reader, writer, created
                self._close_writer(writer)
        reader, writer = await asyncio.wait_for(self._open(), timeout=10)
        if not self._refilling:
            self._refilling = True
            asyncio.create_task(self._refill_pool())
        return reader, writer, asyncio.get_event_loop().time()

    async def _release(self, reader, writer, created):
        """Return a healthy connection to the pool."""
        now = asyncio.get_event_loop().time()
        if (now - created) >= self._conn_ttl or reader.at_eof():
            self._close_writer(writer)
            return
        async with self._pool_lock:
            if len(self._pool) < self._pool_max:
                self._pool.append((reader, writer, created))
            else:
                self._close_writer(writer)

    @staticmethod
    def _close_writer(writer):
        """Silently close a StreamWriter."""
        try:
            writer.close()
        except Exception:
            pass

    async def _flush_pool(self):
        """Close all pooled connections (e.g. after an error)."""
        async with self._pool_lock:
            for _, writer, _ in self._pool:
                self._close_writer(writer)
            self._pool.clear()

    async def _add_conn_to_pool(self):
        try:
            r, w = await asyncio.wait_for(self._open(), timeout=5)
            t = asyncio.get_event_loop().time()
            async with self._pool_lock:
                if len(self._pool) < self._pool_max:
                    self._pool.append((r, w, t))
                else:
                    self._close_writer(w)
        except Exception:
            pass

    async def _refill_pool(self):
        try:
            await asyncio.gather(
                *[self._add_conn_to_pool() for _ in range(8)],
                return_exceptions=True,
            )
        finally:
            self._refilling = False

    async def _pool_maintenance(self):
        """Continuously prune stale connections and maintain minimum idle count."""
        while True:
            try:
                await asyncio.sleep(3)
                now = asyncio.get_event_loop().time()
                async with self._pool_lock:
                    alive, dead = [], []
                    for entry in self._pool:
                        r, w, t = entry
                        if (now - t) < self._conn_ttl and not r.at_eof():
                            alive.append(entry)
                        else:
                            dead.append(w)
                    self._pool = alive
                    idle = len(alive)
                for w in dead:
                    self._close_writer(w)

                needed = max(0, self._pool_min_idle - idle)
                if needed:
                    await asyncio.gather(
                        *[self._add_conn_to_pool()
                          for _ in range(min(needed, 5))],
                        return_exceptions=True,
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ── Pool warm-up ──────────────────────────────────────────────

    async def _warm_pool(self):
        """Pre-open TLS connections in the background. Non-blocking."""
        if self._warmed:
            return
        self._warmed = True
        asyncio.create_task(self._do_warm())
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(
                self._pool_maintenance()
            )
        if self._h2:
            asyncio.create_task(self._h2_connect_and_warm())

    async def _do_warm(self):
        count = 30
        results = await asyncio.gather(
            *[self._add_conn_to_pool() for _ in range(count)],
            return_exceptions=True,
        )
        opened = sum(1 for r in results if not isinstance(r, Exception))
        log.info("Pre-warmed %d/%d TLS connections", opened, count)

    # ── HTTP/2 management ─────────────────────────────────────────

    async def _h2_connect_and_warm(self):
        try:
            await self._h2.ensure_connected()
            log.info("H2 multiplexing active — one conn handles all requests")
        except Exception as e:
            log.warning("H2 connect failed (%s), using H1 pool", e)
            return
        if self._h2.is_connected:
            asyncio.create_task(self._prewarm_script())
            asyncio.create_task(self._keepalive_loop())

    async def _prewarm_script(self):
        """Probe /dev fast path; fall back to /exec warm-up."""
        sid = self._script_ids[0]
        payload = json.dumps(
            {"m": "HEAD", "u": "http://example.com/", "k": self.auth_key}
        ).encode()
        hdrs = {"content-type": "application/json"}

        for endpoint, label in [("dev", "/dev fast path"), ("exec", "/exec warm-up")]:
            path = f"/macros/s/{sid}/{endpoint}"
            try:
                t0 = time.perf_counter()
                _, _, body = await asyncio.wait_for(
                    self._h2.request(
                        method="POST", path=path, host=self.http_host,
                        headers=hdrs, body=payload,
                    ),
                    timeout=15,
                )
                dt = (time.perf_counter() - t0) * 1000
                data = json.loads(body.decode(errors="replace"))
                if endpoint == "dev" and "s" in data:
                    self._dev_available = True
                    log.info("/dev fast path active (%.0fms, no redirect)", dt)
                    return
                elif endpoint == "exec":
                    log.info("Apps Script pre-warmed via /exec in %.0fms", dt)
                    return
            except Exception as e:
                log.debug("%s probe failed: %s", label, e)

    async def _keepalive_loop(self):
        """Ping every 4 minutes to keep Apps Script warm and H2 alive."""
        payload = {"m": "HEAD", "u": "http://example.com/", "k": self.auth_key}
        hdrs = {"content-type": "application/json"}
        while True:
            try:
                # 4 min — Google container timeout is ~5 min idle.
                # Saves ~90 quota hits/day vs 3-minute interval.
                await asyncio.sleep(240)
                if not (self._h2 and self._h2.is_connected):
                    try:
                        await self._h2.reconnect()
                    except Exception:
                        continue
                await self._h2.ping()
                path = self._exec_path()
                t0 = time.perf_counter()
                await asyncio.wait_for(
                    self._h2.request(
                        method="POST", path=path, host=self.http_host,
                        headers=hdrs, body=json.dumps(payload).encode(),
                    ),
                    timeout=20,
                )
                log.debug("Keepalive ping: %.0fms",
                          (time.perf_counter() - t0) * 1000)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Keepalive error: %s", e)

    # ── Script ID helpers ─────────────────────────────────────────

    def _next_script_id(self) -> str:
        """Round-robin across script IDs."""
        sid = self._script_ids[self._script_idx % len(self._script_ids)]
        self._script_idx += 1
        return sid

    def _exec_path(self) -> str:
        sid = self._next_script_id()
        suffix = "dev" if self._dev_available else "exec"
        return f"/macros/s/{sid}/{suffix}"

    def _auth_header(self) -> str:
        return f"X-Auth-Key: {self.auth_key}\r\n" if self.auth_key else ""

    # ── WebSocket tunnel (CONNECT / HTTPS) ────────────────────────

    async def tunnel(self, target_host: str, target_port: int,
                     client_r: asyncio.StreamReader,
                     client_w: asyncio.StreamWriter):
        """Tunnel raw TCP through a domain-fronted WebSocket."""
        try:
            remote_r, remote_w = await self._open()
        except Exception as e:
            log.error("TLS connect to %s failed: %s", self.connect_host, e)
            return

        try:
            ws_key = base64.b64encode(os.urandom(16)).decode()
            path = (f"{self.worker_path}/tunnel"
                    f"?host={target_host}&port={target_port}")
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {self.http_host}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"{self._auth_header()}"
                f"\r\n"
            )
            remote_w.write(handshake.encode())
            await remote_w.drain()

            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = await asyncio.wait_for(remote_r.read(4096), timeout=15)
                if not chunk:
                    raise ConnectionError("No WebSocket handshake response")
                resp += chunk

            status_line = resp.split(b"\r\n")[0]
            if b"101" not in status_line:
                raise ConnectionError(
                    f"WebSocket upgrade rejected: "
                    f"{status_line.decode(errors='replace')}"
                )

            log.info("Tunnel ready → %s:%d", target_host, target_port)
            await asyncio.gather(
                self._client_to_ws(client_r, remote_w),
                self._ws_to_client(remote_r, client_w),
            )
        except Exception as e:
            log.error("Tunnel error (%s:%d): %s", target_host, target_port, e)
        finally:
            self._close_writer(remote_w)

    async def _client_to_ws(self, src: asyncio.StreamReader,
                            dst: asyncio.StreamWriter):
        """Wrap client bytes in WS frames and forward to CDN."""
        try:
            while True:
                data = await src.read(16384)
                if not data:
                    dst.write(ws_encode(b"", opcode=0x08))  # WS close
                    await dst.drain()
                    break
                dst.write(ws_encode(data))
                await dst.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def _ws_to_client(self, src: asyncio.StreamReader,
                            dst: asyncio.StreamWriter):
        """Unwrap WS frames from CDN and write plaintext to client."""
        buf = b""
        try:
            while True:
                chunk = await src.read(16384)
                if not chunk:
                    break
                buf += chunk
                while buf:
                    result = ws_decode(buf)
                    if result is None:
                        break
                    opcode, payload, consumed = result
                    buf = buf[consumed:]
                    if opcode == 0x08:
                        return
                    if payload:
                        dst.write(payload)
                        await dst.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    # ── HTTP forwarding ───────────────────────────────────────────

    async def forward(self, raw_request: bytes) -> bytes:
        """Forward a plain HTTP request through the domain-fronted channel."""
        try:
            reader, writer, created = await self._acquire()
            request = (
                f"POST {self.worker_path}/forward HTTP/1.1\r\n"
                f"Host: {self.http_host}\r\n"
                f"Content-Type: application/octet-stream\r\n"
                f"Content-Length: {len(raw_request)}\r\n"
                f"Connection: keep-alive\r\n"
                f"{self._auth_header()}"
                f"\r\n"
            )
            writer.write(request.encode() + raw_request)
            await writer.drain()
            _, _, resp_body = await self._read_http_response(reader)
            await self._release(reader, writer, created)
            return resp_body
        except Exception as e:
            log.error("Forward failed: %s", e)
            return b"HTTP/1.1 502 Bad Gateway\r\n\r\nDomain fronting request failed\r\n"

    # ── Apps Script relay ─────────────────────────────────────────

    async def relay(self, method: str, url: str,
                    headers: dict, body: bytes = b"") -> bytes:
        """Relay an HTTP request through Apps Script.

        - Coalesces concurrent identical GETs (unless Range header present)
        - Batches concurrent calls via fetchAll() (5ms micro / 50ms macro window)
        - Retries once on connection failure
        - Concurrency-limited via semaphore
        """
        if not self._warmed:
            await self._warm_pool()

        payload = self._build_payload(method, url, headers, body)

        # Range requests must bypass coalescing — each chunk is unique
        has_range = headers and any(
            k.lower() == "range" for k in headers
        )
        if method == "GET" and not body and not has_range:
            return await self._coalesced_submit(url, payload)

        return await self._batch_submit(payload)

    async def _coalesced_submit(self, url: str, payload: dict) -> bytes:
        """Deduplicate concurrent fetches for the same URL."""
        if url in self._coalesce:
            future = asyncio.get_event_loop().create_future()
            self._coalesce[url].append(future)
            log.debug("Coalesced: %s", url[:60])
            return await future

        self._coalesce[url] = []
        try:
            result = await self._batch_submit(payload)
            for f in self._coalesce.get(url, []):
                if not f.done():
                    f.set_result(result)
            return result
        except Exception as e:
            for f in self._coalesce.get(url, []):
                if not f.done():
                    f.set_exception(e)
            raise
        finally:
            self._coalesce.pop(url, None)

    async def relay_parallel(self, method: str, url: str,
                             headers: dict, body: bytes = b"",
                             chunk_size: int = 256 * 1024,
                             max_parallel: int = 16) -> bytes:
        """Parallel range download for large files.

        1. Probe with Range: bytes=0-<chunk_size-1>
        2. If 206 → fetch remaining chunks concurrently via H2
        3. If 200 or small file → return as-is
        """
        if method != "GET" or body:
            return await self.relay(method, url, headers, body)

        range_headers = {**(headers or {}), "Range": f"bytes=0-{chunk_size - 1}"}
        first_resp = await self.relay("GET", url, range_headers, b"")
        status, resp_hdrs, resp_body = self._split_raw_response(first_resp)

        if status != 206:
            return first_resp

        m = re.search(r"/(\d+)", resp_hdrs.get("content-range", ""))
        if not m:
            return self._rewrite_206_to_200(first_resp)

        total_size = int(m.group(1))
        if total_size <= chunk_size or len(resp_body) >= total_size:
            return self._rewrite_206_to_200(first_resp)

        ranges = []
        start = len(resp_body)
        while start < total_size:
            end = min(start + chunk_size - 1, total_size - 1)
            ranges.append((start, end))
            start = end + 1

        log.info("Parallel download: %d bytes, %d chunks of %d KB",
                 total_size, len(ranges) + 1, chunk_size // 1024)

        sem = asyncio.Semaphore(max_parallel)
        base_headers = headers or {}

        async def fetch_range(s: int, e: int, max_tries: int = 3) -> bytes:
            async with sem:
                rh = {**base_headers, "Range": f"bytes={s}-{e}"}
                expected = e - s + 1
                last_err = None
                for attempt in range(max_tries):
                    try:
                        raw = await self.relay("GET", url, rh, b"")
                        _, _, chunk_body = self._split_raw_response(raw)
                        if len(chunk_body) == expected:
                            return chunk_body
                        last_err = f"short chunk {len(chunk_body)}/{expected} B"
                    except Exception as exc:
                        last_err = repr(exc)
                    log.warning("Range %d-%d retry %d/%d: %s",
                                s, e, attempt + 1, max_tries, last_err)
                    await asyncio.sleep(0.3 * (attempt + 1))
                raise RuntimeError(
                    f"chunk {s}-{e} failed after {max_tries} tries: {last_err}"
                )

        t0 = asyncio.get_event_loop().time()
        results = await asyncio.gather(
            *[fetch_range(s, e) for s, e in ranges],
            return_exceptions=True,
        )
        elapsed = asyncio.get_event_loop().time() - t0

        parts = [resp_body]
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log.error("Range chunk %d failed: %s", i, r)
                return self._error_response(502, f"Parallel download failed: {r}")
            parts.append(r)

        full_body = b"".join(parts)
        kbs = (len(full_body) / 1024) / elapsed if elapsed > 0 else 0
        log.info("Parallel download complete: %d B in %.2fs = %.1f KB/s",
                 len(full_body), elapsed, kbs)

        skip = {"transfer-encoding", "connection", "keep-alive",
                "content-length", "content-encoding", "content-range"}
        header_lines = "HTTP/1.1 200 OK\r\n"
        for k, v in resp_hdrs.items():
            if k.lower() not in skip:
                header_lines += f"{k}: {v}\r\n"
        header_lines += f"Content-Length: {len(full_body)}\r\n\r\n"
        return header_lines.encode() + full_body

    @staticmethod
    def _rewrite_206_to_200(raw: bytes) -> bytes:
        """Rewrite 206 → 200 for clients that never sent a Range header.

        Handing a 206 back to the browser for a plain GET breaks XHR/fetch
        on sites like x.com and Cloudflare challenges.
        """
        sep = b"\r\n\r\n"
        if sep not in raw:
            return raw
        header_section, body = raw.split(sep, 1)
        lines = header_section.decode(errors="replace").split("\r\n")
        if not lines:
            return raw
        lines[0] = re.sub(r"206[^\r]*", "200 OK", lines[0])
        out = [
            ln for ln in lines[1:]
            if not ln.lower().startswith(("content-range:", "content-length:"))
        ]
        rebuilt = "\r\n".join([lines[0]] + out)
        rebuilt += f"\r\nContent-Length: {len(body)}"
        return (rebuilt + "\r\n\r\n").encode() + body

    def _build_payload(self, method: str, url: str,
                       headers: dict, body: bytes) -> dict:
        """Build the JSON relay payload dict."""
        payload: dict = {"m": method, "u": url, "r": True}
        if headers:
            # Strip Accept-Encoding: Apps Script auto-decompresses gzip but
            # NOT brotli/zstd — forwarding "br" causes garbled responses.
            filt = {k: v for k, v in headers.items()
                    if k.lower() != "accept-encoding"}
            payload["h"] = filt or headers
        if body:
            payload["b"] = base64.b64encode(body).decode()
            ct = headers.get("Content-Type") or headers.get("content-type") if headers else None
            if ct:
                payload["ct"] = ct
        return payload

    # ── Batch collector ───────────────────────────────────────────

    async def _batch_submit(self, payload: dict) -> bytes:
        """Queue a request into the batch collector."""
        if not self._batch_enabled:
            return await self._relay_with_retry(payload)

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._batch_lock:
            self._batch_pending.append((payload, future))

            if len(self._batch_pending) >= self._batch_max:
                batch = self._batch_pending[:]
                self._batch_pending.clear()
                if self._batch_task and not self._batch_task.done():
                    self._batch_task.cancel()
                self._batch_task = None
                asyncio.create_task(self._batch_send(batch))
            elif self._batch_task is None or self._batch_task.done():
                self._batch_task = asyncio.create_task(self._batch_timer())

        return await future

    async def _batch_timer(self):
        """Two-tier flush: 5ms micro (single req) → 50ms macro (burst)."""
        await asyncio.sleep(self._batch_window_micro)
        async with self._batch_lock:
            if len(self._batch_pending) <= 1:
                if self._batch_pending:
                    batch = self._batch_pending[:]
                    self._batch_pending.clear()
                    self._batch_task = None
                    asyncio.create_task(self._batch_send(batch))
                return

        await asyncio.sleep(self._batch_window_macro - self._batch_window_micro)
        async with self._batch_lock:
            if self._batch_pending:
                batch = self._batch_pending[:]
                self._batch_pending.clear()
                self._batch_task = None
                asyncio.create_task(self._batch_send(batch))

    async def _batch_send(self, batch: list):
        """Dispatch a batch: single relay for one item, fetchAll for many."""
        if len(batch) == 1:
            payload, future = batch[0]
            try:
                result = await self._relay_with_retry(payload)
            except Exception as e:
                result = self._error_response(502, str(e))
            if not future.done():
                future.set_result(result)
            return

        log.info("Batch relay: %d requests", len(batch))
        try:
            results = await self._relay_batch([p for p, _ in batch])
            for (_, future), result in zip(batch, results):
                if not future.done():
                    future.set_result(result)
        except Exception as e:
            log.warning(
                "Batch relay failed — disabling. Redeploy Code.gs for batch "
                "support. Error: %s", e
            )
            self._batch_enabled = False
            await asyncio.gather(
                *[self._relay_fallback(p, f) for p, f in batch]
            )

    async def _relay_fallback(self, payload: dict, future: asyncio.Future):
        try:
            result = await self._relay_with_retry(payload)
        except Exception as e:
            result = self._error_response(502, str(e))
        if not future.done():
            future.set_result(result)

    # ── Core relay ────────────────────────────────────────────────

    async def _relay_with_retry(self, payload: dict) -> bytes:
        """Single relay with one retry. Prefers H2, falls back to H1 pool."""
        if self._h2 and self._h2.is_connected:
            for attempt in range(2):
                try:
                    return await asyncio.wait_for(
                        self._relay_single_h2(payload), timeout=25
                    )
                except Exception as e:
                    if attempt == 0:
                        log.debug("H2 relay failed (%s), reconnecting", e)
                        try:
                            await self._h2.reconnect()
                        except Exception:
                            log.warning("H2 reconnect failed, falling back to H1")
                            break
                    else:
                        raise

        async with self._semaphore:
            for attempt in range(2):
                try:
                    return await asyncio.wait_for(
                        self._relay_single(payload), timeout=25
                    )
                except Exception as e:
                    if attempt == 0:
                        log.debug("Relay attempt 1 failed (%s: %s), retrying",
                                  type(e).__name__, e)
                        await self._flush_pool()
                    else:
                        raise

    async def _relay_single_h2(self, payload: dict) -> bytes:
        """Relay via shared HTTP/2 connection — no pool checkout needed."""
        full_payload = {**payload, "k": self.auth_key}
        _, _, body = await self._h2.request(
            method="POST", path=self._exec_path(), host=self.http_host,
            headers={"content-type": "application/json"},
            body=json.dumps(full_payload).encode(),
        )
        return self._parse_relay_response(body)

    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    async def _relay_single(self, payload: dict) -> bytes:
        """Relay via H1 pool: POST → follow redirects → parse response."""
        full_payload = {**payload, "k": self.auth_key}
        json_body = json.dumps(full_payload).encode()
        path = self._exec_path()
        reader, writer, created = await self._acquire()

        try:
            request = (
                f"POST {path} HTTP/1.1\r\n"
                f"Host: {self.http_host}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(json_body)}\r\n"
                f"Accept-Encoding: gzip\r\n"
                f"Connection: keep-alive\r\n"
                f"\r\n"
            )
            writer.write(request.encode() + json_body)
            await writer.drain()

            status, resp_headers, resp_body = await self._read_http_response(reader)

            for _ in range(5):
                if status not in self._REDIRECT_STATUSES:
                    break
                location = resp_headers.get("location")
                if not location:
                    break
                parsed = urlparse(location)
                rpath = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                writer.write((
                    f"GET {rpath} HTTP/1.1\r\n"
                    f"Host: {parsed.netloc}\r\n"
                    f"Accept-Encoding: gzip\r\n"
                    f"Connection: keep-alive\r\n"
                    f"\r\n"
                ).encode())
                await writer.drain()
                status, resp_headers, resp_body = await self._read_http_response(reader)

            await self._release(reader, writer, created)
            return self._parse_relay_response(resp_body)

        except Exception:
            self._close_writer(writer)
            raise

    async def _relay_batch(self, payloads: list[dict]) -> list[bytes]:
        """Send multiple requests in one POST using Apps Script fetchAll."""
        json_body = json.dumps({"k": self.auth_key, "q": payloads}).encode()
        path = self._exec_path()

        if self._h2 and self._h2.is_connected:
            try:
                _, _, body = await asyncio.wait_for(
                    self._h2.request(
                        method="POST", path=path, host=self.http_host,
                        headers={"content-type": "application/json"},
                        body=json_body,
                    ),
                    timeout=30,
                )
                return self._parse_batch_body(body, payloads)
            except Exception as e:
                log.debug("H2 batch failed (%s), falling back to H1", e)

        async with self._semaphore:
            reader, writer, created = await self._acquire()
            try:
                request = (
                    f"POST {path} HTTP/1.1\r\n"
                    f"Host: {self.http_host}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(json_body)}\r\n"
                    f"Accept-Encoding: gzip\r\n"
                    f"Connection: keep-alive\r\n"
                    f"\r\n"
                )
                writer.write(request.encode() + json_body)
                await writer.drain()

                status, resp_headers, resp_body = await self._read_http_response(reader)
                for _ in range(5):
                    if status not in self._REDIRECT_STATUSES:
                        break
                    location = resp_headers.get("location")
                    if not location:
                        break
                    parsed = urlparse(location)
                    rpath = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                    writer.write((
                        f"GET {rpath} HTTP/1.1\r\n"
                        f"Host: {parsed.netloc}\r\n"
                        f"Accept-Encoding: gzip\r\n"
                        f"Connection: keep-alive\r\n"
                        f"\r\n"
                    ).encode())
                    await writer.drain()
                    status, resp_headers, resp_body = await self._read_http_response(reader)

                await self._release(reader, writer, created)
            except Exception:
                self._close_writer(writer)
                raise

        return self._parse_batch_body(resp_body, payloads)

    def _parse_batch_body(self, resp_body: bytes,
                          payloads: list[dict]) -> list[bytes]:
        text = resp_body.decode(errors="replace").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            data = json.loads(m.group()) if m else None
        if not data:
            raise RuntimeError(f"Bad batch response: {text[:200]}")
        if "e" in data:
            raise RuntimeError(f"Batch error: {data['e']}")
        items = data.get("q", [])
        if len(items) != len(payloads):
            raise RuntimeError(
                f"Batch size mismatch: {len(items)} vs {len(payloads)}"
            )
        return [self._parse_relay_json(item) for item in items]

    # ── HTTP response reading ─────────────────────────────────────

    async def _read_http_response(self, reader: asyncio.StreamReader):
        """Read one HTTP/1.1 response. Keep-alive safe."""
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = await asyncio.wait_for(reader.read(8192), timeout=8)
            if not chunk:
                break
            raw += chunk

        if b"\r\n\r\n" not in raw:
            return 0, {}, b""

        header_section, body = raw.split(b"\r\n\r\n", 1)
        lines = header_section.split(b"\r\n")

        m = re.search(r"\d{3}", lines[0].decode(errors="replace"))
        status = int(m.group()) if m else 0

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if b":" in line:
                k, _, v = line.decode(errors="replace").partition(":")
                headers[k.strip().lower()] = v.strip()

        te = headers.get("transfer-encoding", "")
        cl = headers.get("content-length")

        if "chunked" in te:
            body = await self._read_chunked(reader, body)
        elif cl:
            remaining = int(cl) - len(body)
            while remaining > 0:
                chunk = await asyncio.wait_for(
                    reader.read(min(remaining, 65536)), timeout=20
                )
                if not chunk:
                    break
                body += chunk
                remaining -= len(chunk)
        else:
            # No framing — drain with short timeout (keep-alive safe)
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=2)
                    if not chunk:
                        break
                    body += chunk
                except asyncio.TimeoutError:
                    break

        if headers.get("content-encoding", "").lower() == "gzip":
            try:
                body = gzip.decompress(body)
            except Exception:
                pass

        return status, headers, body

    async def _read_chunked(self, reader: asyncio.StreamReader,
                            buf: bytes = b"") -> bytes:
        """Read chunked transfer-encoding incrementally."""
        result = b""
        while True:
            while b"\r\n" not in buf:
                data = await asyncio.wait_for(reader.read(8192), timeout=20)
                if not data:
                    return result
                buf += data

            end = buf.index(b"\r\n")
            size_str = buf[:end].decode(errors="replace").strip()
            buf = buf[end + 2:]

            if not size_str:
                continue
            try:
                size = int(size_str, 16)
            except ValueError:
                break
            if size == 0:
                break

            while len(buf) < size + 2:
                data = await asyncio.wait_for(reader.read(65536), timeout=20)
                if not data:
                    result += buf[:size]
                    return result
                buf += data

            result += buf[:size]
            buf = buf[size + 2:]

        return result

    # ── Response parsing ──────────────────────────────────────────

    _STATUS_TEXT: dict[int, str] = {
        200: "OK", 206: "Partial Content",
        301: "Moved Permanently", 302: "Found", 304: "Not Modified",
        400: "Bad Request", 403: "Forbidden", 404: "Not Found",
        500: "Internal Server Error",
    }

    def _parse_relay_response(self, body: bytes) -> bytes:
        text = body.decode(errors="replace").strip()
        if not text:
            return self._error_response(502, "Empty response from relay")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if not m:
                return self._error_response(502, f"No JSON: {text[:200]}")
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return self._error_response(502, f"Bad JSON: {text[:200]}")
        return self._parse_relay_json(data)

    def _parse_relay_json(self, data: dict) -> bytes:
        """Convert a relay JSON dict to raw HTTP response bytes."""
        if "e" in data:
            return self._error_response(502, f"Relay error: {data['e']}")

        status = data.get("s", 200)
        resp_headers = data.get("h", {})
        resp_body = base64.b64decode(data.get("b", ""))
        status_text = self._STATUS_TEXT.get(status, "OK")

        _SKIP = frozenset({"transfer-encoding", "connection", "keep-alive",
                           "content-length", "content-encoding"})
        lines = [f"HTTP/1.1 {status} {status_text}"]
        for k, v in resp_headers.items():
            if k.lower() in _SKIP:
                continue
            values = v if isinstance(v, list) else [v]
            if k.lower() == "set-cookie":
                expanded: list[str] = []
                for item in values:
                    expanded.extend(self._split_set_cookie(str(item)))
                values = expanded
            for val in values:
                lines.append(f"{k}: {val}")
        lines.append(f"Content-Length: {len(resp_body)}")
        lines.append("")  # blank line before body
        return "\r\n".join(lines).encode() + b"\r\n" + resp_body

    @staticmethod
    def _split_set_cookie(blob: str) -> list[str]:
        """Split joined Set-Cookie values, preserving date commas in Expires."""
        if not blob:
            return []
        parts = re.split(r",\s*(?=[A-Za-z0-9!#$%&'*+\-.^_`|~]+=)", blob)
        return [p.strip() for p in parts if p.strip()]

    def _split_raw_response(self, raw: bytes):
        """Split raw HTTP response into (status, headers_dict, body)."""
        sep = b"\r\n\r\n"
        if sep not in raw:
            return 0, {}, raw
        header_section, body = raw.split(sep, 1)
        lines = header_section.split(b"\r\n")
        m = re.search(r"\d{3}", lines[0].decode(errors="replace"))
        status = int(m.group()) if m else 0
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if b":" in line:
                k, _, v = line.decode(errors="replace").partition(":")
                headers[k.strip().lower()] = v.strip()
        return status, headers, body

    def _error_response(self, status: int, message: str) -> bytes:
        body = f"<html><body><h1>{status}</h1><p>{message}</p></body></html>"
        return (
            f"HTTP/1.1 {status} Error\r\n"
            f"Content-Type: text/html\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
            f"{body}"
        ).encode()