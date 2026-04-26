#!/usr/bin/env python3
"""
DomainFront Tunnel — Bypass DPI censorship via Domain Fronting.

Run a local HTTP proxy that tunnels all traffic through a CDN using
domain fronting: the TLS SNI shows an allowed domain while the encrypted
HTTP Host header routes to your Cloudflare Worker relay.
"""
import colorama
colorama.init()
import argparse
import asyncio
import datetime
import json
import logging
import os
import sys
import time

from cert_installer import install_ca, is_ca_trusted
from mitm import CA_CERT_FILE
from proxy_server import ProxyServer, get_stats, TrafficStats

__version__ = "1.0.0"

# ── Banner ─────────────────────────────────────────────────────────────────────

SPLASH_LINES = [
    "  ╔══════════════════════════════════════════════════════════════════╗",
    "  ║                                                                  ║",
    "  ║  ██████╗  ██████╗ ███╗   ███╗ █████╗ ██╗███╗   ██╗             ║",
    "  ║  ██╔══██╗██╔═══██╗████╗ ████║██╔══██╗██║████╗  ██║             ║",
    "  ║  ██║  ██║██║   ██║██╔████╔██║███████║██║██╔██╗ ██║             ║",
    "  ║  ██║  ██║██║   ██║██║╚██╔╝██║██╔══██║██║██║╚██╗██║             ║",
    "  ║  ██████╔╝╚██████╔╝██║ ╚═╝ ██║██║  ██║██║██║ ╚████║             ║",
    "  ║  ╚═════╝  ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝             ║",
    "  ║                                                                  ║",
    "  ║     ███████╗██████╗  ██████╗ ███╗   ██╗████████╗               ║",
    "  ║     ██╔════╝██╔══██╗██╔═══██╗████╗  ██║╚══██╔══╝               ║",
    "  ║     █████╗  ██████╔╝██║   ██║██╔██╗ ██║   ██║                  ║",
    "  ║     ██╔══╝  ██╔══██╗██║   ██║██║╚██╗██║   ██║                  ║",
    "  ║     ██║     ██║  ██║╚██████╔╝██║ ╚████║   ██║                  ║",
    "  ║     ╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝   ╚═╝                  ║",
    "  ║                                                                  ║",
    "  ║        ████████╗██╗   ██╗███╗   ██╗███╗   ██╗███████╗██╗       ║",
    "  ║        ╚══██╔══╝██║   ██║████╗  ██║████╗  ██║██╔════╝██║       ║",
    "  ║           ██║   ██║   ██║██╔██╗ ██║██╔██╗ ██║█████╗  ██║       ║",
    "  ║           ██║   ██║   ██║██║╚██╗██║██║╚██╗██║██╔══╝  ██║       ║",
    "  ║           ██║   ╚██████╔╝██║ ╚████║██║ ╚████║███████╗███████╗  ║",
    "  ║           ╚═╝    ╚═════╝ ╚═╝  ╚═══╝╚═╝  ╚═══╝╚══════╝╚══════╝  ║",
    "  ║                                                                  ║",
    "  ╠══════════════════════════════════════════════════════════════════╣",
    "  ║  Stealth HTTP Proxy  ·  DPI Bypass via Domain Fronting          ║",
    f" ║  v{__version__:<8}  ·  University of Toronto                        ║",
    "  ╚══════════════════════════════════════════════════════════════════╝",
]

TAGLINE = "  ╔══ Stealth HTTP proxy · DPI bypass via domain fronting      ══╗"
VERSION_LINE = f"  ║  v{__version__}  ·  University of Toronto                 ║"
DIVIDER  = "  ╚══════════════════════════════════════════════════════════════╝"

COLORS = {
    "reset":  "\033[0m",
    "bold":   "\033[1m",
    "dim":    "\033[2m",
    "cyan":   "\033[36m",
    "green":  "\033[32m",
    "yellow": "\033[33m",
    "red":    "\033[31m",
    "blue":   "\033[34m",
    "magenta":"\033[35m",
    "white":  "\033[97m",
}

def c(color: str, text: str) -> str:
    """Wrap text in ANSI color codes (no-op if stdout is not a tty)."""
    if not sys.stdout.isatty():
        return text
    return COLORS.get(color, "") + text + COLORS["reset"]


def _clear_terminal():
    """Clear terminal cross-platform."""
    if sys.platform == "win32":
        import os
        os.system("cls")
    else:
        sys.stdout.write("\033[H\033[J")
        sys.stdout.flush()


def show_splash(splash_secs: int = 8):
    """Display the signature splash screen with a live countdown, then clear."""
    _clear_terminal()
    is_tty = sys.stdout.isatty()

    for line in SPLASH_LINES:
        print(c("cyan", line))

    print()
    bar_width = 40
    for remaining in range(splash_secs, 0, -1):
        filled  = int((splash_secs - remaining) / splash_secs * bar_width)
        bar     = c("cyan",  "█" * filled) + c("dim", "░" * (bar_width - filled))
        status  = c("yellow", f"  Starting in {remaining}s  ") + bar + "  "
        if is_tty:
            sys.stdout.write("\r" + status)
        else:
            sys.stdout.write(status + "\n")
        sys.stdout.flush()
        time.sleep(1)

    if is_tty:
        sys.stdout.write("\r" + c("green", "  Launching…" + " " * (bar_width + 20)) + "\n")
        sys.stdout.flush()

    time.sleep(0.4)
    _clear_terminal()


def print_banner(mode: str, host: str, port: int):
    print(c("cyan", "\n".join(SPLASH_LINES)))
    print()
    print(c("green",   "  ► Mode     : ") + c("white", mode))
    print(c("green",   "  ► Proxy    : ") + c("yellow", f"http://{host}:{port}"))
    print(c("magenta", "  ► Status   : ") + c("green", "RUNNING"))
    print()


# ── Live stats dashboard ───────────────────────────────────────────────────────

SPARKLINE_CHARS = " ▁▂▃▄▅▆▇█"

def _sparkline(values, width: int = 20) -> str:
    """Render a tiny ASCII sparkline from a sequence of floats."""
    if not values:
        return " " * width
    pts = list(values)[-width:]
    if len(pts) < width:
        pts = [0.0] * (width - len(pts)) + pts
    max_val = max(pts) or 1.0
    return "".join(
        SPARKLINE_CHARS[int((v / max_val) * (len(SPARKLINE_CHARS) - 1))]
        for v in pts
    )


def _bar(ratio: float, width: int = 20, fill: str = "█", empty: str = "░") -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(ratio * width)
    return fill * filled + empty * (width - filled)


def _fmt_uptime(seconds: float) -> str:
    td = datetime.timedelta(seconds=int(seconds))
    h, rem = divmod(td.seconds, 3600)
    m, s = divmod(rem, 60)
    days = td.days
    if days:
        return f"{days}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_dashboard(stats: TrafficStats, mode: str, proxy_addr: str) -> str:
    down_bps, up_bps = stats.current_speeds()
    uptime = stats.uptime_seconds
    total = stats.cache_hits + stats.cache_misses or 1
    cache_ratio = stats.cache_hits / total

    lines = []
    W = 62

    def rule(char="─"):
        return c("dim", "  " + char * W)

    def row(label: str, value: str, color: str = "white"):
        pad = W - len(label) - len(value) - 2
        return (
            "  " + c("dim", label)
            + " " * max(pad, 1)
            + c(color, value)
        )

    lines.append(rule("═"))
    lines.append(
        "  " + c("bold", c("cyan", "  DOMAIN FRONT TUNNEL"))
        + c("dim", f"  {proxy_addr}  ·  {mode}  ·  up {_fmt_uptime(uptime)}")
    )
    lines.append(rule("═"))

    # ── Speed ──────────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("  " + c("bold", "SPEED"))
    lines.append(rule())

    down_spark = _sparkline(stats.speed_history_down)
    up_spark   = _sparkline(stats.speed_history_up)

    lines.append(
        "  " + c("green",  "↓ DN  ")
        + c("cyan",   f"{TrafficStats.fmt_speed(down_bps):>12}")
        + "  " + c("dim", down_spark)
    )
    lines.append(
        "  " + c("yellow", "↑ UP  ")
        + c("cyan",   f"{TrafficStats.fmt_speed(up_bps):>12}")
        + "  " + c("dim", up_spark)
    )

    # ── Traffic totals ─────────────────────────────────────────────────────────
    lines.append("")
    lines.append("  " + c("bold", "TRAFFIC TOTALS"))
    lines.append(rule())
    lines.append(row("  Total downloaded",
                     TrafficStats.fmt_bytes(stats.bytes_down), "green"))
    lines.append(row("  Total uploaded",
                     TrafficStats.fmt_bytes(stats.bytes_up), "yellow"))
    lines.append(row("  Requests completed",
                     str(stats.requests_total), "cyan"))
    lines.append(row("  Active connections",
                     str(stats.active_connections),
                     "green" if stats.active_connections else "dim"))
    lines.append(row("  Errors",
                     str(stats.errors),
                     "red" if stats.errors else "dim"))

    # ── Cache ──────────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("  " + c("bold", "RESPONSE CACHE"))
    lines.append(rule())
    bar_str = _bar(cache_ratio, width=20)
    lines.append(
        "  " + c("dim", "  Hit ratio  ")
        + c("green" if cache_ratio > 0.5 else "yellow", f"{cache_ratio*100:5.1f}%  ")
        + c("dim", bar_str)
    )
    lines.append(row("  Cache hits / misses",
                     f"{stats.cache_hits} / {stats.cache_misses}", "dim"))

    # ── Recent hosts ───────────────────────────────────────────────────────────
    recent = stats.recent_hosts
    if recent:
        lines.append("")
        lines.append("  " + c("bold", "RECENT DESTINATIONS"))
        lines.append(rule())
        for h in recent[:6]:
            # Truncate long hostnames
            display = h if len(h) <= W - 6 else h[:W - 9] + "…"
            lines.append("  " + c("dim", "  · ") + c("white", display))

    lines.append("")
    lines.append(rule("═"))
    lines.append(c("dim", "  Press Ctrl+C to stop"))
    lines.append("")

    return "\n".join(lines)


def _clear_screen():
    """Clear the terminal in a cross-platform way."""
    if sys.platform == "win32":
        import os
        os.system("cls")
    else:
        sys.stdout.write("\033[H\033[J")
        sys.stdout.flush()


async def live_stats_loop(stats: TrafficStats, mode: str, proxy_addr: str,
                          refresh: float = 1.0):
    """Erase and redraw the dashboard every `refresh` seconds."""
    first = True
    while True:
        await asyncio.sleep(refresh)
        dashboard = render_dashboard(stats, mode, proxy_addr)

        if sys.stdout.isatty():
            _clear_screen()
        elif not first:
            sys.stdout.write("\n")

        sys.stdout.write(dashboard + "\n")
        sys.stdout.flush()
        first = False


# ── Logging ────────────────────────────────────────────────────────────────────

class _SilentFilter(logging.Filter):
    """Suppress INFO and below from the root logger when dashboard is running.

    ERROR / WARNING still make it through so real problems are visible,
    though they'll interleave with the dashboard on non-TTY outputs.
    """
    def filter(self, record):
        return record.levelno >= logging.WARNING


def setup_logging(level_name: str, dashboard_mode: bool = False):
    level = getattr(logging, level_name.upper(), logging.INFO)
    fmt = "%(asctime)s [%(name)-12s] %(levelname)-7s %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    if dashboard_mode and sys.stdout.isatty():
        # Quiet the root logger down — dashboard owns the terminal
        for handler in logging.root.handlers:
            handler.addFilter(_SilentFilter())


# ── Arg parsing ────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        prog="domainfront-tunnel",
        description="Local HTTP proxy that tunnels traffic through domain fronting.",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("DFT_CONFIG", "config.json"),
        help="Path to config file (default: config.json, env: DFT_CONFIG)",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=None,
        help="Override listen port (env: DFT_PORT)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override listen host (env: DFT_HOST)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log level (env: DFT_LOG_LEVEL)",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--install-cert",
        action="store_true",
        help="Install the MITM CA certificate as a trusted root and exit.",
    )
    parser.add_argument(
        "--no-cert-check",
        action="store_true",
        help="Skip the certificate installation check on startup.",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable live stats dashboard; fall back to plain log output.",
    )
    parser.add_argument(
        "--stats-interval",
        type=float,
        default=1.0,
        metavar="SECS",
        help="Dashboard refresh interval in seconds (default: 1.0).",
    )
    return parser.parse_args()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    config_path = args.config

    try:
        with open(config_path) as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Config not found: {config_path}")
        print("Copy config.example.json to config.json and fill in your values.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in config: {e}")
        sys.exit(1)

    # Environment variable overrides
    if os.environ.get("DFT_AUTH_KEY"):
        config["auth_key"] = os.environ["DFT_AUTH_KEY"]
    if os.environ.get("DFT_SCRIPT_ID"):
        config["script_id"] = os.environ["DFT_SCRIPT_ID"]

    # CLI argument overrides
    if args.port is not None:
        config["listen_port"] = args.port
    elif os.environ.get("DFT_PORT"):
        config["listen_port"] = int(os.environ["DFT_PORT"])

    if args.host is not None:
        config["listen_host"] = args.host
    elif os.environ.get("DFT_HOST"):
        config["listen_host"] = os.environ["DFT_HOST"]

    if args.log_level is not None:
        config["log_level"] = args.log_level
    elif os.environ.get("DFT_LOG_LEVEL"):
        config["log_level"] = os.environ["DFT_LOG_LEVEL"]

    for key in ("auth_key",):
        if key not in config:
            print(f"Missing required config key: {key}")
            sys.exit(1)

    mode = config.get("mode", "domain_fronting")
    if mode == "custom_domain" and "custom_domain" not in config:
        print("Mode 'custom_domain' requires 'custom_domain' in config")
        sys.exit(1)
    if mode == "domain_fronting":
        for key in ("front_domain", "worker_host"):
            if key not in config:
                print(f"Mode 'domain_fronting' requires '{key}' in config")
                sys.exit(1)
    if mode == "google_fronting":
        if "worker_host" not in config:
            print("Mode 'google_fronting' requires 'worker_host' in config (your Cloud Run URL)")
            sys.exit(1)
    if mode == "apps_script":
        sid = config.get("script_ids") or config.get("script_id")
        if not sid or (isinstance(sid, str) and sid == "YOUR_APPS_SCRIPT_DEPLOYMENT_ID"):
            print("Mode 'apps_script' requires 'script_id' in config.")
            print("Deploy the Apps Script from appsscript/Code.gs and paste the Deployment ID.")
            sys.exit(1)

    # ── Certificate installation ───────────────────────────────────────────────
    if args.install_cert:
        setup_logging("INFO")
        _log = logging.getLogger("Main")
        _log.info("Installing CA certificate…")
        ok = install_ca(CA_CERT_FILE)
        sys.exit(0 if ok else 1)

    listen_host = config.get("listen_host", "127.0.0.1")
    listen_port = config.get("listen_port", 8080)
    use_dashboard = not args.no_dashboard

    setup_logging(config.get("log_level", "INFO"), dashboard_mode=use_dashboard)
    log = logging.getLogger("Main")

    if mode == "apps_script":
        if not os.path.exists(CA_CERT_FILE):
            from mitm import MITMCertManager
            MITMCertManager()

        if not args.no_cert_check:
            if not is_ca_trusted(CA_CERT_FILE):
                log.warning("MITM CA is not trusted — attempting automatic installation…")
                ok = install_ca(CA_CERT_FILE)
                if ok:
                    log.info("CA certificate installed. You may need to restart your browser.")
                else:
                    log.error(
                        "Auto-install failed. Run with --install-cert (may need admin/sudo) "
                        "or manually install ca/ca.crt as a trusted root CA."
                    )
            else:
                log.info("MITM CA is already trusted.")

    # ── Splash screen (8 s) then banner ──────────────────────────────────────
    show_splash(splash_secs=8)

    proxy_addr = f"{listen_host}:{listen_port}"
    print_banner(mode, listen_host, listen_port)

    if not use_dashboard:
        log.info("Live dashboard disabled — logging to stdout.")
        log.info("Mode: %s  |  Proxy: %s", mode, proxy_addr)

    # ── Run ────────────────────────────────────────────────────────────────────
    async def run():
        proxy = ProxyServer(config)
        stats = get_stats()

        if use_dashboard:
            asyncio.ensure_future(
                live_stats_loop(stats, mode, proxy_addr,
                                refresh=args.stats_interval)
            )
        await proxy.start()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print()
        print(c("dim", "  ⏹  Stopped. Bye."))
        print()


if __name__ == "__main__":
    main()