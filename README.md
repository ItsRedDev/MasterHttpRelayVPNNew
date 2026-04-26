# MasterHttpRelayVPN — Improved Fork

[![GitHub](https://img.shields.io/badge/GitHub-MasterHttpRelayVPN-blue?logo=github)](https://github.com/masterking32/MasterHttpRelayVPN)
[![Original Author](https://img.shields.io/badge/Original%20Author-masterking32-orange?logo=github)](https://github.com/masterking32)
[![Persian README](https://img.shields.io/badge/🇮🇷-راهنمای%20فارسی-green)](README_FA.md)

> **This is a performance- and security-improved fork of [MasterHttpRelayVPN](https://github.com/masterking32/MasterHttpRelayVPN) by [@masterking32](https://github.com/masterking32), built at the University of Toronto.**
> All credit for the original concept, architecture, and implementation goes to the original author. This fork focuses on speed, correctness, and security hardening — not new features.

---

## What Is This?

A free tool that lets you access the internet freely by hiding your traffic behind trusted websites like Google. No VPS or server needed — just a free Google account.

> **How it works in simple terms:** Your browser talks to this tool on your computer. This tool disguises your traffic to look like normal Google traffic. The firewall sees "google.com" and lets it pass. Behind the scenes, a free Google Apps Script relay fetches the real website for you.

```
Browser → Local Proxy → Google/CDN front → Your relay → Target website
              |
              └─ shows google.com to the network filter
```

---

## What Changed in This Fork

### `proxy_server.py`
- **`current_speeds()`** — replaced O(n) linear scan over 300 samples with `bisect` binary search (O log n)
- **`parse_ttl()` and `_is_likely_download()`** — replaced Python `for` loops over extension lists with `str.endswith(tuple)`, which runs at C speed
- **`recent_hosts` property** — removed unnecessary double list allocation
- **`_do_http()`** — `header_block.split()` was called twice separately; now split once and reused
- **`_tunnel_http()`** — `import re` was inside the function body, re-evaluated on every request; moved out
- **`asyncio.get_event_loop().create_task()`** — replaced deprecated calls with `asyncio.ensure_future()`

### `main.py`
- **`_sparkline()`** — `list.insert(0, ...)` in a loop is O(n²); replaced with single O(n) list concatenation
- **Dashboard clearing** — `\033[H\033[J` ANSI escape does not work reliably in Windows `cmd.exe`/`py.exe`; replaced with `os.system("cls")` on Windows, ANSI on Linux/macOS
- **Splash screen** — added an 8-second animated signature splash screen on startup with a live countdown progress bar before the proxy begins
- **Fixed typo** — "University of Tornoto" → "University of Toronto"

### `domain_fronter.py` (was `fronter.py`)
- **`_close_writer()`** — extracted shared helper; eliminated 8 identical `try: writer.close() except: pass` blocks
- **`_REDIRECT_STATUSES`** — extracted as a `frozenset` class constant; both `_relay_single` and `_relay_batch` now use `status not in self._REDIRECT_STATUSES`
- **`_SKIP` header set in `_parse_relay_json()`** — was rebuilt on every response call; now a class-level `frozenset`
- **`_STATUS_TEXT`** — same issue, moved to class-level dict
- **`_pool_maintenance()`** — was closing writers while holding `_pool_lock`, risking deadlock; now collects dead writers first, releases lock, then closes
- **`_rewrite_206_to_200()`** — fragile chained `.replace()` calls replaced with `re.sub`
- **`_read_http_response()` / `_split_raw_response()`** — `split(":", 1)` replaced with `partition(":")` (faster, safer)
- **`has_range` detection in `relay()`** — loop replaced with `any()` expression
- **`_prewarm_script()`** — two duplicate try/except request blocks collapsed into a single loop
- **`_h2_connect` + `_h2_connect_and_warm`** — unnecessary two-method split merged into one
- **`_relay_fallback` batch gather** — was missing `asyncio.gather`, causing fallbacks to run sequentially

### `h2_transport.py`
- **`_close_internal()`** — `_read_task.cancel()` was never awaited (task kept running on dead connection); now properly awaited with `CancelledError` suppression. `writer.close()` now followed by `await writer.wait_closed()`
- **`_dispatch()`** — now returns a `bool` (`need_flush`); the reader loop does **one** `_flush()` per event batch instead of one per event, reducing lock contention dramatically
- **Timeout path** — on `TimeoutError`, the stream is now cleaned up with `RST_STREAM` so the server releases resources
- **`WindowUpdated` event** — was a no-op `pass`; now returns `need_flush = True` so pending `send_data` frames are actually sent after the peer opens the window
- **`RemoteSettingsChanged`** — now handled; when the server lowers `MAX_CONCURRENT_STREAMS`, the semaphore updates to match, preventing guaranteed stream resets
- **`ConnectionTerminated` (GOAWAY)** — was ignored; now sets `_connected = False` so next request reconnects cleanly
- **`_reader_loop()`** — added 120-second idle timeout with automatic PING on silence to detect dead connections
- **`_send_body()`** — flow-control exhaustion now logs a debug warning with bytes unsent instead of silently dropping data
- **Added `max_concurrent` semaphore** — caps in-flight streams to prevent overwhelming server's `MAX_CONCURRENT_STREAMS` before it's received
- **Constants** (`_INITIAL_WINDOW`, `_CONNECTION_WINDOW`, `_CONNECT_TIMEOUT`, `_READ_CHUNK`) extracted to class level

### `Code.gs` (Google Apps Script)
- **SSRF protection** — added URL validation blocking `localhost`, `127.x`, `169.254.x.x` (AWS metadata), and RFC-1918 ranges
- **Prototype pollution fix** — `SKIP_HEADERS` was a plain object used as `obj[k]`; replaced with `new Set()` + `.has()`. `for...in` on `req.h` now uses `Object.prototype.hasOwnProperty.call()`
- **Missing `contentType` fallback** — `req.b` decoded with no type caused Apps Script to guess and sometimes corrupt binary payloads; now defaults to `application/octet-stream`
- **Micro-cache** — in-process 5-second cache for identical GETs within the same script execution; zero quota consumed for repeated requests (favicons, fonts, trackers)
- **`URL_RE` compiled once** — regex was re-compiled on every `_doSingle` / `_doBatch` call
- **`_encodeResponse()` helper** — eliminated duplicated `getResponseCode/getHeaders/getContent/base64Encode` block
- **Empty batch guard** — returns early instead of calling `fetchAll([])` unnecessarily
- **`const`/`let`** throughout — replaced `var` which has function-scoped hoisting that can cause subtle loop bugs

---

## Step-by-Step Setup Guide

### Step 1: Download This Project

```bash
git clone https://github.com/ItsRedDev/MasterHttpRelayVPNNew.git
cd MasterHttpRelayVPNNew
pip install -r requirements.txt
```

> **Can't reach PyPI directly?** Use this mirror:
> ```bash
> pip install -r requirements.txt -i https://mirror-pypi.runflare.com/simple/ --trusted-host mirror-pypi.runflare.com
> ```

### Step 2: Set Up the Google Relay (Code.gs)

1. Open [Google Apps Script](https://script.google.com/) and sign in.
2. Click **New project**.
3. Delete all default code.
4. Copy everything from `Code.gs` in this project and paste it in.
5. **Change the auth key** to something only you know:
   ```javascript
   const AUTH_KEY = "your-secret-password-here";
   ```
6. Click **Deploy → New deployment**.
7. Set type to **Web app**, execute as **Me**, access to **Anyone**.
8. Click **Deploy** and **copy the Deployment ID**.

> ⚠️ If you edit `Code.gs` later, you must create a **new deployment** — edits don't update the live version automatically.

### Step 3: Configure

```bash
cp config.example.json config.json
```

Edit `config.json`:
```json
{
  "mode": "apps_script",
  "google_ip": "216.239.38.120",
  "front_domain": "www.google.com",
  "script_id": "PASTE_YOUR_DEPLOYMENT_ID_HERE",
  "auth_key": "your-secret-password-here",
  "listen_host": "127.0.0.1",
  "listen_port": 8085,
  "log_level": "INFO",
  "verify_ssl": true
}
```

### Step 4: Run

```bash
python main.py
```

On first launch you'll see the signature splash screen for 8 seconds, then the proxy starts on `127.0.0.1:8085`.

### Step 5: Set Up Your Browser

| Browser | How to set proxy |
|---------|-----------------|
| Firefox | Settings → General → Network Settings → Manual proxy → `127.0.0.1` port `8085` → check "Also use this proxy for HTTPS" |
| Chrome/Edge | Windows Settings → Network → Proxy → Manual → `127.0.0.1:8085` |
| Any browser | Use [FoxyProxy](https://addons.mozilla.org/en-US/firefox/addon/foxyproxy-standard/) or [SwitchyOmega](https://chrome.google.com/webstore/detail/proxy-switchyomega/) |

### Step 6: Install the CA Certificate (HTTPS only)

The tool generates a CA certificate on first run at `ca/ca.crt`. You must install it or your browser will show security warnings.

**Windows**
1. Double-click `ca/ca.crt` → Install Certificate → Current User
2. Place in **Trusted Root Certification Authorities**
3. Restart browser

**macOS**
1. Double-click `ca/ca.crt` → opens Keychain Access
2. Find the cert → double-click → Trust → **Always Trust**
3. Restart browser

**Linux (Ubuntu/Debian)**
```bash
sudo cp ca/ca.crt /usr/local/share/ca-certificates/masterhttp-relay.crt
sudo update-ca-certificates
```

**Firefox (all platforms — must do this separately)**
1. Settings → Privacy & Security → Certificates → View Certificates
2. Authorities tab → Import → select `ca/ca.crt`
3. Check **Trust this CA to identify websites** → OK

> The proxy will attempt to auto-install the certificate on startup. Run `python main.py --install-cert` if auto-install fails.

---

## Modes

| Mode | Requires | Description |
|------|----------|-------------|
| `apps_script` | Free Google account | **Recommended.** Free, no server needed. |
| `google_fronting` | Google Cloud Run service | Your own Cloud Run behind Google's CDN. |
| `domain_fronting` | Cloudflare Worker | A Cloudflare Worker as relay. |
| `custom_domain` | Custom domain on Cloudflare | Direct connection to your Cloudflare domain. |

### Load Balancing (Faster Speeds)

Deploy `Code.gs` to multiple Apps Script projects and list all IDs:

```json
{
  "script_ids": [
    "DEPLOYMENT_ID_1",
    "DEPLOYMENT_ID_2",
    "DEPLOYMENT_ID_3"
  ]
}
```

---

## Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `mode` | — | Relay type (`apps_script`, `google_fronting`, `domain_fronting`, `custom_domain`) |
| `auth_key` | — | Password shared between proxy and relay |
| `script_id` | — | Apps Script Deployment ID |
| `listen_host` | `127.0.0.1` | Proxy listen address |
| `listen_port` | `8085` | Proxy listen port |
| `log_level` | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `google_ip` | `216.239.38.120` | Google IP to connect through |
| `front_domain` | `www.google.com` | Domain shown to the firewall |
| `verify_ssl` | `true` | Verify TLS certificates |
| `script_ids` | — | Array of Script IDs for load balancing |

---

## Command Line Options

```bash
python main.py                          # Normal start
python main.py -p 9090                  # Use a different port
python main.py --log-level DEBUG        # Verbose logging
python main.py -c /path/to/config.json  # Use a different config file
python main.py --install-cert           # Install MITM CA certificate and exit
python main.py --no-cert-check          # Skip CA install check on startup
```

---

## Project Files

| File | Description |
|------|-------------|
| `main.py` | Entry point, splash screen, argument parsing |
| `proxy_server.py` | Handles browser connections, HTTP/CONNECT proxy logic |
| `domain_fronter.py` | Domain fronting, relay, batching, coalescing |
| `h2_transport.py` | HTTP/2 multiplexed transport (optional, faster) |
| `mitm.py` | HTTPS certificate generation and interception |
| `cert_installer.py` | Cross-platform CA installer (Windows/macOS/Linux/Firefox) |
| `ws.py` | WebSocket framing (encode/decode) |
| `Code.gs` | Google Apps Script relay — deploy this to your Google account |
| `config.example.json` | Example configuration — copy to `config.json` |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Config not found" | Copy `config.example.json` to `config.json` |
| Browser shows certificate errors | Install `ca/ca.crt` (see Step 6) |
| Telegram works but browser doesn't | CA cert not installed — follow Step 6, then **fully close** and reopen the browser |
| Cert installed but errors persist | Chrome/Edge cache certs — check Task Manager, kill all browser processes, reopen |
| "unauthorized" error | `auth_key` in `config.json` must exactly match `AUTH_KEY` in `Code.gs` |
| Connection timeout | Try a different `google_ip` |
| Slow browsing | Deploy multiple `Code.gs` copies and use `script_ids` array |
| `502 Bad JSON` | Wrong `script_id`, or you edited `Code.gs` without creating a new deployment |

---

## Architecture

```
┌──────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐
│  Browser │───►│ Local Proxy  │───►│ Google / CDN │───►│  Relay   │──► Internet
│          │◄───│ (this tool)  │◄───│  (fronted)   │◄───│ Endpoint │◄──
└──────────┘    └──────────────┘    └──────────────┘    └──────────┘
                  HTTP/CONNECT         TLS (SNI: ok)       fetchTarget
                  MITM optional        Host: relay          return resp
```

---

## Security Tips

- **Change `AUTH_KEY`** in `Code.gs` before deploying — the default is public.
- **Never share `config.json`** — it contains your password.
- **Never share the `ca/` folder** — it contains your private certificate key.
- Keep `listen_host` as `127.0.0.1` so only your computer can use the proxy.

---

## Credits

- **Original project:** [MasterHttpRelayVPN](https://github.com/masterking32/MasterHttpRelayVPN) by [@masterking32](https://github.com/masterking32)
- **Special thanks:** [@abolix](https://github.com/abolix) for making the original project possible
- **This fork:** Performance, correctness, and security improvements — University of Toronto

---

## Disclaimer

MasterHttpRelayVPN is provided for educational, testing, and research purposes only. The developers and contributors are not responsible for any damages resulting from use. You are solely responsible for legal compliance in your jurisdiction and compliance with Google's Terms of Service.

## License

MIT
