"""
Local HTTP proxy server.

Intercepts the user's browser traffic and forwards everything through
a domain-fronted connection to a CDN worker or Apps Script relay.

Supports:
  - CONNECT method  → WebSocket tunnel (modes 1-3) or MITM relay (apps_script)
  - GET / POST etc. → HTTP forwarding  (modes 1-3) or JSON relay (apps_script)
"""

import asyncio
import bisect
import collections
import logging
import re
import ssl
import time

from domain_fronter import DomainFronter

log = logging.getLogger("Proxy")


# ── Live traffic statistics ───────────────────────────────────────────────────

class TrafficStats:
    """Thread-safe counters for live dashboard display."""

    SPEED_WINDOW = 2.0          # seconds to smooth speed samples over
    HISTORY_POINTS = 60         # data points kept for sparkline display

    def __init__(self):
        self._lock = asyncio.Lock()

        # Totals
        self.requests_total: int = 0
        self.bytes_down: int = 0          # bytes sent to client (download)
        self.bytes_up: int = 0            # bytes sent to remote (upload)
        self.errors: int = 0
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self.start_time: float = time.time()

        # Active connections
        self._active: int = 0

        # Sliding window for speed calculation
        # Each entry: (timestamp, down_bytes, up_bytes)
        self._samples: collections.deque = collections.deque(maxlen=300)
        self._last_sample_time: float = time.time()
        self._last_bytes_down: int = 0
        self._last_bytes_up: int = 0

        # History for sparklines (one point per ~1 s)
        self.speed_history_down: collections.deque = collections.deque(
            maxlen=self.HISTORY_POINTS)
        self.speed_history_up: collections.deque = collections.deque(
            maxlen=self.HISTORY_POINTS)

        # Recent hosts (last 8 unique)
        self._recent_hosts: collections.OrderedDict = collections.OrderedDict()

    # ── Mutators (called from async code, no lock needed for simple ints) ─────

    def record_request(self, host: str = ""):
        self.requests_total += 1
        self._active += 1
        if host:
            self._recent_hosts[host] = time.time()
            if len(self._recent_hosts) > 8:
                self._recent_hosts.popitem(last=False)

    def finish_request(self):
        if self._active > 0:
            self._active -= 1

    def record_bytes_down(self, n: int):
        self.bytes_down += n

    def record_bytes_up(self, n: int):
        self.bytes_up += n

    def record_error(self):
        self.errors += 1

    def record_cache_hit(self):
        self.cache_hits += 1

    def record_cache_miss(self):
        self.cache_misses += 1

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def active_connections(self) -> int:
        return self._active

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    def current_speeds(self) -> tuple[float, float]:
        """Return (down_bps, up_bps) as bytes/sec over the last SPEED_WINDOW."""
        now = time.time()
        cutoff = now - self.SPEED_WINDOW
        # snapshot current totals
        self._samples.append((now, self.bytes_down, self.bytes_up))
        # Binary search for oldest sample still inside window (O(log n) vs O(n))
        samples_list = list(self._samples)
        timestamps = [s[0] for s in samples_list]
        idx = bisect.bisect_left(timestamps, cutoff)
        if idx >= len(samples_list) - 1:
            return 0.0, 0.0
        oldest = samples_list[idx]
        elapsed = now - oldest[0]
        if elapsed <= 0:
            return 0.0, 0.0
        down_bps = (self.bytes_down - oldest[1]) / elapsed
        up_bps   = (self.bytes_up   - oldest[2]) / elapsed
        return max(0.0, down_bps), max(0.0, up_bps)

    def tick_history(self):
        """Called once per second by the stats loop to record history points."""
        down, up = self.current_speeds()
        self.speed_history_down.append(down)
        self.speed_history_up.append(up)

    @property
    def recent_hosts(self) -> list[str]:
        return list(reversed(self._recent_hosts))

    @staticmethod
    def fmt_bytes(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    @staticmethod
    def fmt_speed(bps: float) -> str:
        return TrafficStats.fmt_bytes(bps) + "/s"


# Singleton — shared across all proxy handler instances
_stats = TrafficStats()


def get_stats() -> TrafficStats:
    return _stats


# ── Response cache ─────────────────────────────────────────────────────────────

class ResponseCache:
    """Simple LRU response cache — avoids repeated relay calls."""

    def __init__(self, max_mb: int = 50):
        self._store: dict[str, tuple[bytes, float]] = {}
        self._size = 0
        self._max = max_mb * 1024 * 1024

    def get(self, url: str) -> bytes | None:
        entry = self._store.get(url)
        if not entry:
            _stats.record_cache_miss()
            return None
        raw, expires = entry
        if time.time() > expires:
            self._size -= len(raw)
            del self._store[url]
            _stats.record_cache_miss()
            return None
        _stats.record_cache_hit()
        return raw

    def put(self, url: str, raw_response: bytes, ttl: int = 300):
        size = len(raw_response)
        if size > self._max // 4 or size == 0:
            return
        while self._size + size > self._max and self._store:
            oldest = next(iter(self._store))
            self._size -= len(self._store[oldest][0])
            del self._store[oldest]
        if url in self._store:
            self._size -= len(self._store[url][0])
        self._store[url] = (raw_response, time.time() + ttl)
        self._size += size

    @property
    def hit_ratio(self) -> float:
        total = _stats.cache_hits + _stats.cache_misses
        return (_stats.cache_hits / total * 100) if total else 0.0

    @staticmethod
    def parse_ttl(raw_response: bytes, url: str) -> int:
        """Determine cache TTL from response headers and URL."""
        hdr_end = raw_response.find(b"\r\n\r\n")
        if hdr_end < 0:
            return 0
        hdr = raw_response[:hdr_end].decode(errors="replace").lower()

        if b"HTTP/1.1 200" not in raw_response[:20]:
            return 0
        if "no-store" in hdr:
            return 0

        m = re.search(r"max-age=(\d+)", hdr)
        if m:
            return min(int(m.group(1)), 86400)

        path = url.split("?")[0].lower()
        _STATIC_EXTS = (
            ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
            ".mp3", ".mp4", ".wasm",
        )
        if path.endswith(_STATIC_EXTS):
            return 3600

        ct_m = re.search(r"content-type:\s*([^\r\n]+)", hdr)
        ct = ct_m.group(1) if ct_m else ""
        if "image/" in ct or "font/" in ct:
            return 3600
        if "text/css" in ct or "javascript" in ct:
            return 1800
        if "text/html" in ct or "application/json" in ct:
            return 0

        return 0


# ── Proxy server ──────────────────────────────────────────────────────────────

class ProxyServer:
    def __init__(self, config: dict):
        self.host = config.get("listen_host", "127.0.0.1")
        self.port = config.get("listen_port", 8080)
        self.mode = config.get("mode", "domain_fronting")
        self.fronter = DomainFronter(config)
        self.mitm = None
        self._cache = ResponseCache(max_mb=50)

        # Persistent HTTP tunnel cache for google_fronting mode
        self._http_tunnels: dict = {}
        self._tunnel_lock = asyncio.Lock()

        # Hosts override — DNS fake-map: domain/suffix → IP
        self._hosts: dict[str, str] = config.get("hosts", {})

        if self.mode == "apps_script":
            try:
                from mitm import MITMCertManager
                self.mitm = MITMCertManager()
            except ImportError:
                log.error("apps_script mode requires 'cryptography' package.")
                log.error("Run: pip install cryptography")
                raise SystemExit(1)

    async def start(self):
        srv = await asyncio.start_server(self._on_client, self.host, self.port)
        log.info(
            "Listening on %s:%d — configure your browser HTTP proxy to this address",
            self.host, self.port,
        )
        # Start the background stats-history ticker
        asyncio.ensure_future(self._stats_ticker())
        async with srv:
            await srv.serve_forever()

    async def _stats_ticker(self):
        """Record one speed history point per second."""
        while True:
            await asyncio.sleep(1)
            _stats.tick_history()

    # ── client handler ─────────────────────────────────────────────────────────

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not first_line:
                return

            header_block = first_line
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                header_block += line
                if line in (b"\r\n", b"\n", b""):
                    break

            request_line = first_line.decode(errors="replace").strip()
            parts = request_line.split(" ", 2)
            if len(parts) < 2:
                return

            method = parts[0].upper()

            # Extract host for stats
            host = ""
            if method == "CONNECT":
                host = parts[1].split(":")[0]
            else:
                for raw_line in header_block.split(b"\r\n")[1:]:
                    if raw_line.lower().startswith(b"host:"):
                        host = raw_line.split(b":", 1)[1].strip().decode(errors="replace")
                        host = host.split(":")[0]
                        break

            _stats.record_request(host)

            if method == "CONNECT":
                await self._do_connect(parts[1], reader, writer)
            else:
                await self._do_http(header_block, reader, writer)

        except asyncio.TimeoutError:
            log.debug("Timeout: %s", addr)
            _stats.record_error()
        except Exception as e:
            log.debug("Error (%s): %s", addr, e)
            _stats.record_error()
        finally:
            _stats.finish_request()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ── CONNECT (HTTPS tunnelling) ─────────────────────────────────────────────

    async def _do_connect(self, target: str, reader, writer):
        host, _, port = target.rpartition(":")
        port = int(port) if port else 443
        if not host:
            host, port = target, 443

        log.debug("CONNECT → %s:%d", host, port)

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        if self.mode == "apps_script":
            override_ip = self._sni_rewrite_ip(host)
            if override_ip:
                log.debug("SNI-rewrite tunnel → %s via %s (SNI: %s)",
                          host, override_ip, self.fronter.sni_host)
                await self._do_sni_rewrite_tunnel(host, port, reader, writer,
                                                  connect_ip=override_ip)
            elif self._is_google_domain(host):
                log.debug("Direct tunnel → %s (Google domain, skipping relay)", host)
                await self._do_direct_tunnel(host, port, reader, writer)
            else:
                await self._do_mitm_connect(host, port, reader, writer)
        else:
            await self.fronter.tunnel(host, port, reader, writer)

    # ── Hosts override (fake DNS) ──────────────────────────────────────────────

    _SNI_REWRITE_SUFFIXES = (
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "ytimg.com",
        "ggpht.com",
        "gvt1.com",
        "gvt2.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googleadservices.com",
        "google-analytics.com",
        "googletagmanager.com",
        "googletagservices.com",
        "fonts.googleapis.com",
    )

    def _sni_rewrite_ip(self, host: str) -> str | None:
        ip = self._hosts_ip(host)
        if ip:
            return ip
        h = host.lower().rstrip(".")
        for suffix in self._SNI_REWRITE_SUFFIXES:
            if h == suffix or h.endswith("." + suffix):
                return self.fronter.connect_host
        return None

    def _hosts_ip(self, host: str) -> str | None:
        h = host.lower().rstrip(".")
        if h in self._hosts:
            return self._hosts[h]
        parts = h.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[i:])
            if parent in self._hosts:
                return self._hosts[parent]
        return None

    # ── Google domain detection ────────────────────────────────────────────────

    _GOOGLE_SUFFIXES = (
        ".google.com", ".google.co",
        ".googleapis.com", ".gstatic.com",
        ".googleusercontent.com",
    )
    _GOOGLE_EXACT = {
        "google.com", "gstatic.com", "googleapis.com",
    }

    def _is_google_domain(self, host: str) -> bool:
        h = host.lower().rstrip(".")
        if h in self._GOOGLE_EXACT:
            return True
        for suffix in self._GOOGLE_SUFFIXES:
            if h.endswith(suffix):
                return True
        return False

    # ── Direct tunnel (no MITM) ────────────────────────────────────────────────

    async def _do_direct_tunnel(self, host: str, port: int,
                                reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter,
                                connect_ip: str | None = None):
        target_ip = connect_ip or self.fronter.connect_host
        try:
            r_remote, w_remote = await asyncio.wait_for(
                asyncio.open_connection(target_ip, port), timeout=10
            )
        except Exception as e:
            log.debug("Direct tunnel connect failed (%s via %s): %s",
                      host, target_ip, e)
            _stats.record_error()
            return

        async def pipe(src, dst, direction: str):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
                    if direction == "down":
                        _stats.record_bytes_down(len(data))
                    else:
                        _stats.record_bytes_up(len(data))
            except (ConnectionError, asyncio.CancelledError):
                pass
            except Exception as e:
                log.debug("Pipe %s ended: %s", direction, e)
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pipe(reader, w_remote, "up"),
            pipe(r_remote, writer,  "down"),
        )

    # ── SNI-rewrite tunnel ─────────────────────────────────────────────────────

    async def _do_sni_rewrite_tunnel(self, host: str, port: int, reader, writer,
                                     connect_ip: str | None = None):
        target_ip = connect_ip or self.fronter.connect_host
        sni_out   = self.fronter.sni_host

        ssl_ctx_server = self.mitm.get_server_context(host)
        loop = asyncio.get_event_loop()
        transport = writer.transport
        protocol  = transport.get_protocol()
        try:
            new_transport = await loop.start_tls(
                transport, protocol, ssl_ctx_server, server_side=True,
            )
        except Exception as e:
            log.debug("SNI-rewrite TLS accept failed (%s): %s", host, e)
            return
        writer._transport = new_transport

        ssl_ctx_client = ssl.create_default_context()
        if not self.fronter.verify_ssl:
            ssl_ctx_client.check_hostname = False
            ssl_ctx_client.verify_mode = ssl.CERT_NONE
        try:
            r_out, w_out = await asyncio.wait_for(
                asyncio.open_connection(
                    target_ip, port,
                    ssl=ssl_ctx_client,
                    server_hostname=sni_out,
                ),
                timeout=10,
            )
        except Exception as e:
            log.error("SNI-rewrite outbound connect failed (%s via %s): %s",
                      host, target_ip, e)
            _stats.record_error()
            return

        async def pipe(src, dst, direction: str):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
                    if direction == "down":
                        _stats.record_bytes_down(len(data))
                    else:
                        _stats.record_bytes_up(len(data))
            except (ConnectionError, asyncio.CancelledError):
                pass
            except Exception as exc:
                log.debug("Pipe %s ended: %s", direction, exc)
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pipe(reader, w_out,  "up"),
            pipe(r_out,  writer, "down"),
        )

    # ── MITM CONNECT (apps_script mode) ───────────────────────────────────────

    async def _do_mitm_connect(self, host: str, port: int, reader, writer):
        ssl_ctx = self.mitm.get_server_context(host)

        loop = asyncio.get_event_loop()
        transport = writer.transport
        protocol = transport.get_protocol()

        try:
            new_transport = await loop.start_tls(
                transport, protocol, ssl_ctx, server_side=True,
            )
        except Exception as e:
            if port != 443:
                log.debug("TLS handshake skipped for %s:%d (non-HTTPS): %s", host, port, e)
            else:
                log.debug("TLS handshake failed for %s: %s", host, e)
            return

        writer._transport = new_transport

        while True:
            try:
                first_line = await asyncio.wait_for(reader.readline(), timeout=120)
                if not first_line:
                    break

                header_block = first_line
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=10)
                    header_block += line
                    if line in (b"\r\n", b"\n", b""):
                        break

                body = b""
                for raw_line in header_block.split(b"\r\n"):
                    if raw_line.lower().startswith(b"content-length:"):
                        length = int(raw_line.split(b":", 1)[1].strip())
                        body = await reader.readexactly(length)
                        break

                request_line = first_line.decode(errors="replace").strip()
                parts = request_line.split(" ", 2)
                if len(parts) < 2:
                    break

                method = parts[0]
                path = parts[1]

                headers = {}
                for raw_line in header_block.split(b"\r\n")[1:]:
                    if b":" in raw_line:
                        k, v = raw_line.decode(errors="replace").split(":", 1)
                        headers[k.strip()] = v.strip()

                if port == 443:
                    url = f"https://{host}{path}"
                else:
                    url = f"https://{host}:{port}{path}"

                log.debug("MITM → %s %s", method, url)

                origin = next(
                    (v for k, v in headers.items() if k.lower() == "origin"), ""
                )
                acr_method = next(
                    (v for k, v in headers.items()
                     if k.lower() == "access-control-request-method"), ""
                )
                acr_headers = next(
                    (v for k, v in headers.items()
                     if k.lower() == "access-control-request-headers"), ""
                )

                if method.upper() == "OPTIONS" and acr_method:
                    log.debug("CORS preflight → %s (responding locally)", url[:60])
                    writer.write(self._cors_preflight_response(origin, acr_method, acr_headers))
                    await writer.drain()
                    continue

                response = None
                if method == "GET" and not body:
                    response = self._cache.get(url)

                if response is None:
                    try:
                        response = await self._relay_smart(method, url, headers, body)
                    except Exception as e:
                        log.error("Relay error (%s): %s", url[:60], e)
                        _stats.record_error()
                        err_body = f"Relay error: {e}".encode()
                        response = (
                            b"HTTP/1.1 502 Bad Gateway\r\n"
                            b"Content-Type: text/plain\r\n"
                            b"Content-Length: " + str(len(err_body)).encode() + b"\r\n"
                            b"\r\n" + err_body
                        )

                    if method == "GET" and not body and response:
                        ttl = ResponseCache.parse_ttl(response, url)
                        if ttl > 0:
                            self._cache.put(url, response, ttl)
                            log.debug("Cached (%ds): %s", ttl, url[:60])

                if origin and response:
                    response = self._inject_cors_headers(response, origin)

                if response:
                    _stats.record_bytes_down(len(response))
                    _stats.record_bytes_up(len(body))

                writer.write(response)
                await writer.drain()

            except asyncio.TimeoutError:
                break
            except asyncio.IncompleteReadError:
                break
            except ConnectionError:
                break
            except Exception as e:
                log.debug("MITM handler error (%s): %s", host, e)
                _stats.record_error()
                break

    # ── CORS helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _cors_preflight_response(origin: str, acr_method: str, acr_headers: str) -> bytes:
        allow_origin = origin or "*"
        allow_methods = (
            f"{acr_method}, GET, POST, PUT, DELETE, PATCH, OPTIONS"
            if acr_method else
            "GET, POST, PUT, DELETE, PATCH, OPTIONS"
        )
        allow_headers = acr_headers or "*"
        return (
            "HTTP/1.1 204 No Content\r\n"
            f"Access-Control-Allow-Origin: {allow_origin}\r\n"
            f"Access-Control-Allow-Methods: {allow_methods}\r\n"
            f"Access-Control-Allow-Headers: {allow_headers}\r\n"
            "Access-Control-Allow-Credentials: true\r\n"
            "Access-Control-Max-Age: 86400\r\n"
            "Vary: Origin\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        ).encode()

    @staticmethod
    def _inject_cors_headers(response: bytes, origin: str) -> bytes:
        """Inject CORS headers only if the upstream response lacks them."""
        sep = b"\r\n\r\n"
        if sep not in response:
            return response
        header_section, body = response.split(sep, 1)
        lines = header_section.decode(errors="replace").split("\r\n")

        existing = {ln.split(":", 1)[0].strip().lower()
                    for ln in lines if ":" in ln}

        if "access-control-allow-origin" in existing:
            return response

        allow_origin = origin or "*"
        additions = [f"Access-Control-Allow-Origin: {allow_origin}"]
        if allow_origin != "*":
            additions.append("Access-Control-Allow-Credentials: true")
            additions.append("Vary: Origin")
        return ("\r\n".join(lines + additions) + "\r\n\r\n").encode() + body

    async def _relay_smart(self, method, url, headers, body):
        """Choose optimal relay strategy based on request type."""
        if method == "GET" and not body:
            if headers:
                for k in headers:
                    if k.lower() == "range":
                        return await self.fronter.relay(method, url, headers, body)
            if self._is_likely_download(url, headers):
                return await self.fronter.relay_parallel(method, url, headers, body)
        return await self.fronter.relay(method, url, headers, body)

    _LARGE_EXTS = (
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk",
        ".iso", ".img",
        ".mp4", ".mkv", ".avi", ".mov", ".webm",
        ".mp3", ".flac", ".wav", ".aac",
        ".pdf", ".doc", ".docx", ".ppt", ".pptx",
        ".wasm",
    )

    def _is_likely_download(self, url: str, headers: dict) -> bool:
        path = url.split("?")[0].lower()
        return path.endswith(self._LARGE_EXTS)

    # ── Plain HTTP forwarding ──────────────────────────────────────────────────

    async def _do_http(self, header_block: bytes, reader, writer):
        body = b""
        lines = header_block.split(b"\r\n")
        first_line = lines[0].decode(errors="replace")
        for raw_line in lines[1:]:
            if raw_line.lower().startswith(b"content-length:"):
                length = int(raw_line.split(b":", 1)[1].strip())
                body = await reader.readexactly(length)
                break

        log.debug("HTTP → %s", first_line)

        if self.mode == "apps_script":
            parts = first_line.strip().split(" ", 2)
            method = parts[0] if parts else "GET"
            url = parts[1] if len(parts) > 1 else "/"

            headers = {}
            for raw_line in header_block.split(b"\r\n")[1:]:
                if b":" in raw_line:
                    k, v = raw_line.decode(errors="replace").split(":", 1)
                    headers[k.strip()] = v.strip()

            origin = next(
                (v for k, v in headers.items() if k.lower() == "origin"), ""
            )
            acr_method = next(
                (v for k, v in headers.items()
                 if k.lower() == "access-control-request-method"), ""
            )
            acr_headers_val = next(
                (v for k, v in headers.items()
                 if k.lower() == "access-control-request-headers"), ""
            )
            if method.upper() == "OPTIONS" and acr_method:
                log.debug("CORS preflight (HTTP) → %s (responding locally)", url[:60])
                writer.write(self._cors_preflight_response(origin, acr_method, acr_headers_val))
                await writer.drain()
                return

            response = None
            if method == "GET" and not body:
                response = self._cache.get(url)

            if response is None:
                response = await self._relay_smart(method, url, headers, body)
                if method == "GET" and not body and response:
                    ttl = ResponseCache.parse_ttl(response, url)
                    if ttl > 0:
                        self._cache.put(url, response, ttl)

            if origin and response:
                response = self._inject_cors_headers(response, origin)

        elif self.mode in ("google_fronting", "custom_domain", "domain_fronting"):
            response = await self._tunnel_http(header_block, body)
        else:
            response = await self.fronter.forward(header_block + body)

        if response:
            _stats.record_bytes_down(len(response))
            _stats.record_bytes_up(len(body))

        writer.write(response)
        await writer.drain()

    async def _tunnel_http(self, header_block: bytes, body: bytes) -> bytes:
        """Forward plain HTTP via a persistent WebSocket tunnel."""
        from urllib.parse import urlparse

        host = ""
        port = 80
        for line in header_block.split(b"\r\n")[1:]:
            if not line:
                break
            if line.lower().startswith(b"host:"):
                host_val = line.split(b":", 1)[1].strip().decode(errors="replace")
                if ":" in host_val:
                    h, p = host_val.rsplit(":", 1)
                    try:
                        host, port = h, int(p)
                    except ValueError:
                        host = host_val
                else:
                    host = host_val
                break

        if not host:
            return b"HTTP/1.1 400 Bad Request\r\n\r\nNo Host header\r\n"

        first_line = header_block.split(b"\r\n")[0]
        first_str = first_line.decode(errors="replace")
        parts = first_str.split(" ", 2)
        if len(parts) >= 2 and parts[1].startswith("http://"):
            from urllib.parse import urlparse
            parsed = urlparse(parts[1])
            rel_path = parsed.path or "/"
            if parsed.query:
                rel_path += "?" + parsed.query
            new_first = f"{parts[0]} {rel_path}"
            if len(parts) == 3:
                new_first += f" {parts[2]}"
            header_block = new_first.encode() + b"\r\n" + b"\r\n".join(header_block.split(b"\r\n")[1:])

        raw_request = header_block + body

        try:
            return await asyncio.wait_for(
                self.fronter.forward(raw_request), timeout=30
            )
        except Exception as e:
            log.debug("Tunnel HTTP failed (%s:%d): %s", host, port, e)
            _stats.record_error()
            return b"HTTP/1.1 502 Bad Gateway\r\n\r\nTunnel forward failed\r\n"