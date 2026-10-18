#!/usr/bin/env python3
"""mlxsw - rapid-mlx model switcher for the Mac Studio.

Status view, plus starting, stopping, restarting and switching slot servers.
  - `rapid-mlx ls`  -> models in the HF cache
  - `rapid-mlx ps`  -> running servers (pid, port, model, uptime)
  - GET :PORT/v1/models -> is the server actually answering?
  - ps / sysctl / vm_stat -> process and memory figures

Usage:
  mlxsw.py              one-shot view
  mlxsw.py -w [SECS]    live view, refresh every SECS (default 5), Ctrl-C to quit
  mlxsw.py --json       dump the collected state as JSON
  mlxsw.py tui [-w N]   interactive shell (optional; needs `pip install textual`)
  mlxsw.py stop SLOT    stop the server in a slot (asks first; -y skips)
  mlxsw.py start SLOT MODEL
                        start a model that's on disk in an empty slot (asks for
                        an API key (default "none"), then for confirmation; -y skips)
  mlxsw.py restart SLOT same model, fresh process (stop, then start)
  mlxsw.py switch SLOT MODEL
                        stop the slot's server, then start MODEL in it

Stdlib only (the optional tui excepted). Only stop/start/restart/switch
change anything; all of them ask first. `stop` never force-kills; `start` never downloads.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Resolved from PATH at startup by find_rapid_mlx(); never hard-coded.
RAPID_MLX = None

# Slot layout. Read from slots.json in the config folder by load_slots();
# these defaults apply only when that file doesn't exist.
# What a slot gets when it has no "args" of its own. Meant for an internal,
# protected network: reachable from the LAN, any Host header accepted (every
# server has an API key). A slot with its own "args" -- even an empty list,
# which means localhost only -- gets exactly those and nothing from here.
DEFAULT_ARGS = ["--host", "0.0.0.0", "--trusted-hosts", "*"]

DEFAULT_SLOTS = [
    {"name": "LARGE", "port": 8000, "hint": "~30B", "args": list(DEFAULT_ARGS)},
    {"name": "MIDDLE", "port": 8001, "hint": "~12B", "args": list(DEFAULT_ARGS)},
    {"name": "SMALL", "port": 8002, "hint": "~2-4B", "args": list(DEFAULT_ARGS)},
]

# Flags `start` always sets itself; a slot's "args" may not contain them.
RESERVED_ARGS = ("--port", "--log-file", "--disable-model-downloads", "--api-key", "--listen-fd")
SLOTS = DEFAULT_SLOTS
CONFIG_SOURCE = "built-in defaults"

# A slot whose server holds less than this share of its model in RAM gets a
# warning: the rest was most likely compressed (or swapped) while idle.
RESIDENT_WARN = 0.60

# Models that are not chat LLMs: embedders, speech, drafters, tiny assistants.
AUX_NAME = re.compile(r"embed|whisper|-assistant|-MTP-4bit$", re.I)
AUX_MAX_BYTES = 2 * 1024**3

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}


# ---------------------------------------------------------------- helpers

def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout + p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"__error__ {e}"


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def to_bytes(num, unit):
    return int(float(num) * UNITS.get(unit, 1))


def human(n):
    if n is None:
        return "-"
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.0f} {u}" if u in ("B", "KiB") else f"{n:.1f} {u}"
        n /= 1024


# ---------------------------------------------------------------- collectors

LS_ROW = re.compile(r"^(\S+)\s+(\S+)\s+([\d.]+)\s+(B|KiB|MiB|GiB|TiB)\s+(.+?)\s*$")


def parse_ls(text):
    """Rows of `rapid-mlx ls`. Split on regex, not columns: long aliases
    shove the repo column right, and long repos are truncated with '...'."""
    models = []
    for line in strip_ansi(text).splitlines():
        m = LS_ROW.match(line.strip())
        if not m or m.group(1) == "Alias":
            continue
        alias, repo, num, unit, modified = m.groups()
        size = to_bytes(num, unit)
        models.append({
            "alias": None if alias == "(unmapped)" else alias,
            "repo": repo,
            "repo_truncated": repo.endswith("..."),
            "size": size,
            "modified": modified,
            "aux": bool(AUX_NAME.search(repo)) or size < AUX_MAX_BYTES,
        })
    return models


PS_ROW = re.compile(r"^(\d+)\s+(\d+)\s+(\S+)\s+(\S+)")


def parse_ps(text):
    servers = []
    for line in strip_ansi(text).splitlines():
        m = PS_ROW.match(line.strip())
        if m:
            pid, port, model, uptime = m.groups()
            servers.append({"pid": int(pid), "port": int(port), "model": model, "uptime": uptime})
    return servers


def probe(port, timeout=2.0):
    """Ask the server what it is serving. None = not answering.

    A server started with an API key refuses us (401/403); that still means
    it's up and serving, we just can't see its model list."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout) as r:
            data = json.load(r).get("data", [])
        return {
            "ids": [d.get("id") for d in data],
            "moe": any(d.get("is_moe") for d in data),
            "auth": False,
        }
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ids": [], "moe": False, "auth": True}
        return None
    except Exception:
        return None


def proc_info(pid):
    out = run(["/bin/ps", "-ww", "-o", "rss=,command=", "-p", str(pid)], timeout=5).strip()
    if not out or out.startswith("__error__"):
        return None
    rss_kb, _, command = out.partition(" ")
    emb = re.search(r"--embedding-model\s+(\S+)", command)
    return {
        "rss": int(rss_kb) * 1024 if rss_kb.isdigit() else None,
        "embedding": emb.group(1) if emb else None,
        "command": command,
    }


def memory():
    """Activity Monitor's breakdown from vm_stat, in bytes."""
    total = run(["sysctl", "-n", "hw.memsize"], 5).strip()
    if not total.isdigit():
        return {"total": None}
    vm = run(["vm_stat"], 5)
    ps = re.search(r"page size of (\d+)", vm)
    pg = int(ps.group(1)) if ps else 16384
    v = {k.strip('"'): int(n) * pg
         for k, n in re.findall(r"^([^:\n]+):\s+(\d+)\.", vm, re.M)}
    g = lambda k: v.get(k, 0)
    app = g("Anonymous pages") - g("Pages purgeable")
    wired, comp = g("Pages wired down"), g("Pages occupied by compressor")
    cache = g("File-backed pages") + g("Pages purgeable")
    used, total = app + wired + comp, int(total)
    sw = re.search(r"used = ([\d.]+)M", run(["sysctl", "-n", "vm.swapusage"], 5))
    return {"total": total, "used": used, "wired": wired, "compressed": comp,
            "cache": cache, "free": max(total - used - cache, 0),
            "swap": float(sw.group(1)) * 1024**2 if sw else None}

def model_matches(m, name):
    """Does an ls row refer to the same model as `name` (alias or HF id)?"""
    if not name:
        return False
    name_l = name.lower()
    if m["alias"] and m["alias"].lower() == name_l:
        return True
    repo_l = m["repo"].lower()
    if m["repo_truncated"]:
        return name_l.startswith(repo_l[:-3])
    return repo_l == name_l


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "mlxsw", "slots.json")


def load_slots(path=None):
    """Return (slots, source). Missing file -> defaults; a broken one -> exit with why."""
    path = path or config_path()
    if not os.path.exists(path):
        return DEFAULT_SLOTS, "built-in defaults"
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"mlxsw: can't read slot config {path}:\n  {e}")

    def bad(msg):
        sys.exit(f"mlxsw: slot config {path}: {msg}")

    raw = data.get("slots") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        bad('expected {"slots": [ ... ]} with at least one slot')
    slots, names, ports = [], set(), set()
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            bad(f"slot #{i} is not an object")
        name, port, hint = s.get("name"), s.get("port"), s.get("hint", "")
        if not isinstance(name, str) or not name.strip():
            bad(f"slot #{i} needs a \"name\"")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            bad(f"slot {name!r} needs a \"port\" between 1 and 65535")
        if not isinstance(hint, str):
            bad(f"slot {name!r}: \"hint\" must be text")
        args = s.get("args", DEFAULT_ARGS)       # explicit args (even []) rule
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            bad(f"slot {name!r}: \"args\" must be a list of strings")
        for a in args:
            flag = a.split("=", 1)[0]
            if flag in RESERVED_ARGS:
                why = ("the API key is asked for at start, never stored"
                       if flag == "--api-key" else "mlxsw sets it itself")
                bad(f"slot {name!r}: {flag} can't go in \"args\" ({why})")
        name = name.strip().upper()
        if name in names:
            bad(f"slot name {name!r} is used twice")
        if port in ports:
            bad(f"port {port} is used by two slots")
        names.add(name)
        ports.add(port)
        slots.append({"name": name, "port": port, "hint": hint, "args": list(args)})
    return slots, path


def collect(models=None):
    if models is None:
        models = parse_ls(run([RAPID_MLX, "ls"], timeout=60))
    servers = parse_ps(run([RAPID_MLX, "ps"], timeout=30))
    by_port = {s["port"]: s for s in servers}

    slot_ports = {s["port"] for s in SLOTS}
    slots = []
    for slot in SLOTS + [{"name": "OTHER", "port": p, "hint": ""}
                         for p in sorted(by_port) if p not in slot_ports]:
        srv = by_port.get(slot["port"])
        api = probe(slot["port"])
        proc = proc_info(srv["pid"]) if srv else None
        if srv and api:
            status = "serving"
        elif srv:
            status = "loading"       # process up, API not answering yet (or wedged)
        elif api:
            status = "foreign"       # something answers on the port, not in rapid-mlx ps
        else:
            status = "empty"
        slots.append({**slot, "status": status, "server": srv, "api": api, "proc": proc})

    for m in models:
        m["loaded_on"] = [
            s["port"] for s in slots
            if (s["server"] and model_matches(m, s["server"]["model"]))
            or any(model_matches(m, i) for i in (s["api"] or {}).get("ids", []))
        ]

    # How much of each slot's main model is actually in RAM. The embedder (if
    # any) only adds to rss, so this errs on the side of not warning.
    for s in slots:
        s["model_size"] = s["resident"] = None
        main = next((m for m in models if not m["aux"] and s["port"] in m["loaded_on"]), None)
        rss = (s["proc"] or {}).get("rss")
        if main and rss and main["size"]:
            s["model_size"] = main["size"]
            s["resident"] = rss / main["size"]

    return {"host": socket.gethostname().split(".")[0], "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "config": CONFIG_SOURCE,
            "memory": memory(), "slots": slots, "models": models}


# ---------------------------------------------------------------- rendering

class C:
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    @staticmethod
    def w(code, s):
        return f"\x1b[{code}m{s}\x1b[0m" if C.on else s


def bold(s): return C.w("1", s)
def dim(s): return C.w("2", s)
def green(s): return C.w("32", s)
def yellow(s): return C.w("33", s)
def red(s): return C.w("31", s)
def cyan(s): return C.w("36", s)
def magenta(s): return C.w("35", s)
def blue(s): return C.w("34", s)


def pad(s, width):
    """ljust that ignores ANSI codes; truncates with an ellipsis."""
    raw = strip_ansi(s)
    if len(raw) > width:
        return raw[: max(width - 1, 0)] + "…"
    return s + " " * (width - len(raw))


def bar(frac, width=24):
    frac = max(0.0, min(1.0, frac))
    full = int(round(frac * width))
    colour = green if frac < 0.6 else yellow if frac < 0.8 else red
    return colour("█" * full) + dim("░" * (width - full))


STATUS = {
    "serving": lambda: green("● serving"),
    "loading": lambda: yellow("◐ loading"),
    "foreign": lambda: magenta("? foreign"),
    "empty": lambda: dim("○ empty"),
}
SLOT_COLOURS = (cyan, magenta, blue)


def port_colour(port):
    """Colour for a configured slot's port, by its position; other ports plain."""
    for i, s in enumerate(SLOTS):
        if s["port"] == port:
            return SLOT_COLOURS[i % len(SLOT_COLOURS)]
    return str


def render(st, version="", live="", cols=None):
    cols = cols or shutil.get_terminal_size((120, 40)).columns
    out = []
    mem = st["memory"]

    title = f" {bold('mlxsw')} · {st['host']}" + (f" · {version}" if version else "")
    if live:
        title += "   " + live
    out.append(title + " " * max(1, cols - len(strip_ansi(title)) - len(st["time"]) - 1) + dim(st["time"]))

    if mem["total"]:
        T = mem["total"]
        mlx = sum((s["proc"] or {}).get("rss") or 0 for s in st["slots"])
        other = max(mem["used"] - mlx, 0)
        W, n_done, b = 40, 0, ""
        for val, col, ch in ((mlx, cyan, "█"), (other, yellow, "█"), (mem["cache"], dim, "▒")):
            n = min(int(round(val / T * W)), W - n_done)
            b, n_done = b + col(ch * n), n_done + n
        b += dim("░" * (W - n_done))
        out.append(f" RAM {b}  used {bold(human(mem['used']))} of {human(T)}")
        d = (f"     {cyan('■')} mlx {human(mlx)}  {yellow('■')} other {human(other)}"
             f" (compressed {human(mem['compressed'])}, wired {human(mem['wired'])})"
             f"  {dim('▒')} cache {human(mem['cache'])}  {dim('░')} free {human(mem['free'])}")
        if mem["swap"] is not None:
            d += f"  swap {human(mem['swap'])}"
        out.append(d)
    out.append("")

    # --- slots
    out.append(bold(" SLOTS"))
    squeezed = False
    for s in st["slots"]:
        srv, proc, api = s["server"], s["proc"], s["api"]
        port = port_colour(s["port"])(f":{s['port']}")
        model = srv["model"] if srv else ((api or {}).get("ids") or ["—"])[0]
        row = f"  {pad(bold(s['name']), 7)} {pad(port, 6)} {pad(STATUS[s['status']](), 10)}  {pad(model, 32)}"
        if srv:
            row += f"  up {pad(srv['uptime'], 8)} pid {pad(str(srv['pid']), 6)}"
        if proc and proc["rss"]:
            row += f" rss {pad(human(proc['rss']), 9)}"
        if s.get("resident") is not None and s["resident"] < RESIDENT_WARN:
            row += yellow(f" ⚠ {s['resident']:.0%} in RAM")
            squeezed = True
        if api and api.get("auth"):
            row += dim(" [key]")   # plain text: emoji are 2 columns wide and break alignment
        if api and api["moe"]:
            row += dim(" MoE")
        if proc and proc["embedding"]:
            row += dim(f"  +emb {proc['embedding']}")
        if s["status"] == "empty" and s["hint"]:
            row += dim(f"  ({s['hint']} slot)")
        out.append(row)
    if squeezed:
        out.append(yellow("  ⚠ ") + dim("most of that model is out of RAM (compressed or swapped while idle); "
                                         "its next request will be slow"))
    out.append("")

    # --- models on disk
    llms = [m for m in st["models"] if not m["aux"]]
    aux = [m for m in st["models"] if m["aux"]]
    out.append(bold(f" MODELS ON DISK") + dim(f"  {len(llms)} LLMs · {len(aux)} aux · "
                                              f"{human(sum(m['size'] for m in st['models']))} total"))
    aw = max([len(m["alias"] or "(unmapped)") for m in st["models"]] + [10])
    rw = max(20, min(52, cols - aw - 34))
    out.append(dim(f"  {'':6} {pad('ALIAS', aw)}  {pad('HF REPO', rw)}  {'SIZE':>9}  MODIFIED"))

    def model_row(m, faint=False):
        mark = "".join(port_colour(p)(f"▶{p}") for p in m["loaded_on"])
        alias = m["alias"] or "(unmapped)"
        text = f"{pad(alias, aw)}  {pad(m['repo'], rw)}  {human(m['size']):>9}  {m['modified']}"
        if m["loaded_on"]:
            text = bold(text)
        elif faint or not m["alias"]:
            text = dim(text)
        return f"  {pad(mark, 6)} {text}"

    for m in llms:
        out.append(model_row(m))
    if aux:
        out.append(dim("  ── aux (embedders, speech, drafters)"))
        for m in aux:
            out.append(model_row(m, faint=True))

    unmapped = [m for m in llms if not m["alias"]]
    if unmapped:
        out.append("")
        out.append(dim(f"  {len(unmapped)} unmapped LLM(s): no alias, so `rapid-mlx serve <alias>` can't name them."))
    return "\n".join(out)


# ---------------------------------------------------------------- main

# ---------------------------------------------------------------- actions

STOP_TIMEOUT = 30.0


def resolve_target(target):
    """Slot name (any case) or port number -> {"name", "port"}, or None."""
    t = str(target).strip()
    for s in SLOTS:
        if s["name"] == t.upper():
            return {"name": s["name"], "port": s["port"]}
    digits = t.lstrip(":")
    if digits.isdigit() and 1 <= int(digits) <= 65535:
        port = int(digits)
        name = next((s["name"] for s in SLOTS if s["port"] == port), "OTHER")
        return {"name": name, "port": port}
    return None


def server_on(port):
    """The rapid-mlx server listed on this port (with its command line), or None."""
    srv = next((s for s in parse_ps(run([RAPID_MLX, "ps"], timeout=30)) if s["port"] == port), None)
    if srv:
        srv["proc"] = proc_info(srv["pid"])
    return srv


def _pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    state = run(["/bin/ps", "-o", "stat=", "-p", str(pid)], 5).strip()
    return state == "" or state.startswith("Z")      # zombie = exited, not yet reaped


def _port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def stop_server(srv, timeout=STOP_TIMEOUT, say=print):
    """Politely stop a rapid-mlx server: SIGTERM, then wait until the process
    has exited and its port is free. Never force-kills. Returns True on success."""
    import signal
    pid, port = srv["pid"], srv["port"]
    command = (srv.get("proc") or {}).get("command", "")
    if "rapid-mlx" not in command or " serve" not in command:
        say(f"refusing: pid {pid} doesn't look like a rapid-mlx server "
            f"({command[:80] or 'no command line found'})")
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        say(f"pid {pid} had already exited")
    except PermissionError:
        say(f"not allowed to stop pid {pid} (it belongs to another user)")
        return False
    else:
        say(f"asked pid {pid} to stop; waiting up to {timeout:g}s …")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _pid_gone(pid) and not _port_in_use(port):
            say(f"stopped: pid {pid} exited and port {port} is free ({time.time() - t0:.1f}s)")
            return True
        time.sleep(0.5)
    if not _pid_gone(pid):
        say(f"pid {pid} is still running after {timeout:g}s (busy with a request?). "
            f"Nothing was forced. If it's stuck: kill -9 {pid}")
    else:
        say(f"pid {pid} exited, but something else is still using port {port}")
    return False


def describe_server(t, srv):
    """Lines shown before asking to stop a server."""
    proc = srv.get("proc") or {}
    lines = [f"{t['name']} :{t['port']}  {srv['model']}  pid {srv['pid']}  up {srv['uptime']}"]
    if proc.get("command"):
        lines.append("to start it again later: "
                     + " ".join(shlex.quote(a) for a in proc["command"].split()))   # '*' stays quoted
    return lines


# ---------------------------------------------------------------- info

def _get_text(port, path, timeout=3.0):
    """GET a path on the server. Returns (status, text); status None = no answer."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


def parse_metrics(text):
    """Prometheus text -> {name: value}. A name with labels keeps its first
    sample, plus "name{label=...}" entries for each labelled sample."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)", line)
        if not m:
            continue
        try:
            val = float(m.group(3))
        except ValueError:
            continue
        name, labels = m.group(1), m.group(2)
        out.setdefault(name, val)
        if labels:
            out[name + labels] = val
    return out


def server_details(port):
    """What the server says about itself on /health and /metrics. Both answer
    without the API key in rapid-mlx 0.16; missing or refused -> None."""
    h_status, h_text = _get_text(port, "/health")
    m_status, m_text = _get_text(port, "/metrics")
    health = None
    if h_status == 200:
        try:
            health = json.loads(h_text)
        except ValueError:
            pass
    metrics = parse_metrics(m_text) if m_status == 200 else None
    return {"health": health, "health_status": h_status,
            "metrics": metrics, "metrics_status": m_status}


def _count(n):
    if n is None:
        return "-"
    n = float(n)
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"


def _n(value, word, plural=None):
    """'1 request', '3 requests', '- requests' (value as from metrics, may be None)."""
    one = value is not None and float(value) == 1
    return f"{_count(value)} {word if one else (plural or word + 's')}"


def _from_rapid_mlx(args):
    """A server's command line starting at `rapid-mlx`, without the Python
    interpreter (and the full path) that ps shows in front of it."""
    for i, a in enumerate(args):
        if os.path.basename(a) == "rapid-mlx":
            return ["rapid-mlx"] + args[i + 1:]
    return args


def describe_info(slot):
    """Detail lines for one slot of collect(): the long form the status view has no room for."""
    srv, proc, api = slot["server"], slot["proc"] or {}, slot["api"]
    head = f"{slot['name']} :{slot['port']}  {slot['status']}"
    if srv:
        head += f"  {srv['model']}  pid {srv['pid']}  up {srv['uptime']}"
    if api and api.get("auth"):
        head += "  [key]"
    lines = [head]
    if slot["status"] == "empty":
        lines.append("  nothing is running here")
        return lines
    d = slot.get("details") or {}
    h, mt = d.get("health"), d.get("metrics")
    label = lambda k: f"  {k:<9} "

    if h:
        bits = [h.get("state") or ("ready" if h.get("ready") else "not ready"),
                h.get("model_type"), h.get("engine_type")]
        lines.append(label("state") + " · ".join(str(b) for b in bits if b))
    else:
        lines.append(label("state") + f"no /health answer ({d.get('health_status') or 'no reply'})")

    args = proc.get("command", "").split()
    if args:
        host = _host_of(args)
        trusted = _trusted_of(args)
        reach = "this Mac only" if host in LOOPBACK else f"the network (--host {host})"
        if trusted is not None:
            reach += ", trusted hosts: " + (" ".join(trusted) or "-")
        lines.append(label("reach") + reach)

    if mt is None:
        lines.append(label("metrics") + f"no /metrics answer ({d.get('metrics_status') or 'no reply'}); "
                     "an older rapid-mlx, or not up yet")
    else:
        g = mt.get
        mem = [f"metal {human(g('rapid_mlx_metal_active_memory_bytes'))} now",
               f"{human(g('rapid_mlx_metal_peak_memory_bytes'))} peak"]
        if g("rapid_mlx_metal_cache_memory_bytes"):
            mem.append(f"{human(g('rapid_mlx_metal_cache_memory_bytes'))} metal cache")
        if proc.get("rss"):
            mem.append(f"rss {human(proc['rss'])}")
        lines.append(label("memory") + " · ".join(mem))

        cap, cur = g("rapid_mlx_prefix_cache_cap_bytes"), g("rapid_mlx_prefix_cache_current_bytes")
        if cap or cur:
            pfx = f"{human(cur or 0)} of {human(cap)} cap" if cap else f"{human(cur)}"
            if g("rapid_mlx_prefix_cache_radix_entries") is not None:
                pfx += f" · {_n(g('rapid_mlx_prefix_cache_radix_entries'), 'entry', 'entries')}"
            pfx += (f" · {_n(g('rapid_mlx_prefix_cache_hits_total'), 'hit')}"
                    f" / {_n(g('rapid_mlx_prefix_cache_misses_total'), 'miss', 'misses')}"
                    f" · {_count(g('rapid_mlx_prefix_cache_tokens_saved_total'))} tokens saved")
        else:
            pfx = "not in use (no cap, nothing cached)"
        lines.append(label("prefix") + pfx)

        traffic = (f"{_n(g('rapid_mlx_requests_processed_total'), 'request')}"
                   f" · {_count(g('rapid_mlx_requests_running'))} running"
                   f" · {_count(g('rapid_mlx_requests_waiting'))} waiting"
                   f" · {_count(g('rapid_mlx_prompt_tokens_total'))} prompt"
                   f" / {_n(g('rapid_mlx_completion_tokens_total'), 'completion token')}")
        lines.append(label("traffic") + traffic)

        speed = []
        if g("rapid_mlx_model_decode_tokens_per_second_last") is not None:
            speed.append(f"last decode {g('rapid_mlx_model_decode_tokens_per_second_last'):.1f} tok/s")
        n, total = g("rapid_mlx_model_ttft_seconds_count"), g("rapid_mlx_model_ttft_seconds_sum")
        if n:
            speed.append(f"avg time to first token {total / n:.2f}s")
        if speed:
            lines.append(label("speed") + " · ".join(speed))

    if proc.get("command"):
        lines.append(label("command") + " ".join(shlex.quote(a) for a in _from_rapid_mlx(args)))
    log = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--log-file"), None)
    if log:                                   # only what this server actually writes to
        lines.append(label("log") + log.replace(os.path.expanduser("~"), "~", 1))
    return lines


def info_state(target=None):
    """collect() plus /health and /metrics for the slot asked for (or every
    slot with something running). Returns (slots, problem)."""
    st = collect([])        # no model list needed: skips the slow `rapid-mlx ls`
    if target is not None:
        t = resolve_target(target)
        if not t:
            names = ", ".join(s["name"] for s in SLOTS)
            return None, f"no slot called {target!r}. Slots: {names} (or give a port number)."
        slots = [s for s in st["slots"] if s["port"] == t["port"]]
        if not slots:                      # a port that isn't a slot and has nothing on it
            slots = [{"name": t["name"], "port": t["port"], "status": "empty", "hint": "",
                      "server": None, "api": None, "proc": None}]
    else:
        slots = [s for s in st["slots"] if s["status"] != "empty"]
        if not slots:
            return None, "nothing from rapid-mlx is running in any slot."
    for s in slots:
        if s["status"] != "empty":
            s["details"] = server_details(s["port"])
    return slots, None


def cmd_info(target=None, as_json=False):
    slots, problem = info_state(target)
    if problem:
        print(f"mlxsw: {problem}")
        return 1
    if as_json:
        print(json.dumps(slots, indent=2, default=str))
        return 0
    print("\n\n".join("\n".join(describe_info(s)) for s in slots))
    return 0


def cmd_stop(target, assume_yes=False):
    t = resolve_target(target)
    if not t:
        names = ", ".join(s["name"] for s in SLOTS)
        sys.exit(f"mlxsw: no slot called {target!r}. Slots: {names} (or give a port number).")
    srv = server_on(t["port"])
    if not srv:
        print(f"Nothing from rapid-mlx is running on {t['name']} :{t['port']}.")
        return 0
    for line in describe_server(t, srv):
        print(line)
    if not assume_yes:
        if not sys.stdin.isatty():
            sys.exit("mlxsw: can't ask for confirmation here (not a terminal); add -y to confirm.")
        if input("Stop it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled; nothing was stopped.")
            return 0
    return 0 if stop_server(srv) else 1


START_TIMEOUT = 300.0

# Every server mlxsw starts has an API key. Enter at the prompt means this one:
# the literal four-character string many OpenAI-style clients send by default.
DEFAULT_API_KEY = "none"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def log_dir():
    return os.path.join(os.path.expanduser("~"), "Library", "Logs", "mlxsw")


def find_model(name, models):
    """A model on disk by alias, HF repo, or the repo's short name (any case).
    Returns (model, problem)."""
    n = name.strip().lower()
    for m in models:
        if (m["alias"] or "").lower() == n or m["repo"].lower() == n:
            return m, None
    short = [m for m in models if m["repo"].lower().split("/")[-1] == n]
    if len(short) == 1:
        return short[0], None
    if len(short) > 1:
        return None, f"{name!r} matches more than one model: " + ", ".join(m["repo"] for m in short)
    return None, (f"{name!r} is not on disk. mlxsw never downloads; "
                  f"get it first with:  rapid-mlx pull <hf-repo>")


def _host_of(args):
    for i, a in enumerate(args):
        if a == "--host" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--host="):
            return a.split("=", 1)[1]
    return "127.0.0.1"                     # rapid-mlx's own default


def _trusted_of(args):
    """The --trusted-hosts values in a slot's args, or None if the flag is absent."""
    for i, a in enumerate(args):
        if a == "--trusted-hosts":
            vals = []
            for v in args[i + 1:]:
                if v.startswith("--"):
                    break
                vals += [x for x in v.split(",") if x]
            return vals
        if a.startswith("--trusted-hosts="):
            return [x for x in a.split("=", 1)[1].split(",") if x]
    return None


def plan_start(target, model_name, replace=False):
    """Check everything before starting. Returns (plan, problem); one is None.

    With replace=True (restart/switch) a server already in the slot is not a
    problem: it goes into plan["old"], to be stopped right before the start,
    and the memory it holds counts as available."""
    t = resolve_target(target)
    if not t:
        names = ", ".join(s["name"] for s in SLOTS)
        return None, f"no slot called {target!r}. Slots: {names} (or give a port number)."
    slot = next((s for s in SLOTS if s["port"] == t["port"]), None)
    models = parse_ls(run([RAPID_MLX, "ls"], timeout=60))
    m, problem = find_model(model_name, models)
    if not m:
        return None, problem
    if not m["alias"] and m["repo_truncated"]:
        return None, (f"{m['repo']} has no alias and its name is cut short in `rapid-mlx ls`, "
                      f"so it can't be started by name. Give it an alias with `rapid-mlx alias`.")
    srv = server_on(t["port"])
    if srv and not replace:
        return None, (f"{t['name']} :{t['port']} is already serving {srv['model']} "
                      f"(pid {srv['pid']}). Stop it first, or use switch / restart.")
    if not srv and _port_in_use(t["port"]):
        return None, f"port {t['port']} is in use by something that isn't a rapid-mlx server."

    serve_name = m["alias"] or m["repo"]
    args = list(slot["args"]) if slot else []
    log = os.path.join(log_dir(), (t["name"] if slot else f"port-{t['port']}") + ".log")
    cmd = [RAPID_MLX, "serve", serve_name, *args, "--port", str(t["port"]),
           "--disable-model-downloads", "--log-file", log]

    warnings = []
    mem = memory()
    if mem.get("total"):
        freed = ((srv or {}).get("proc") or {}).get("rss") or 0
        avail = mem["free"] + mem["cache"] + freed
        if m["size"] > avail:
            extra = f", counting the {human(freed)} the old server frees" if freed else ""
            warnings.append(f"it may not fit: the model is {human(m['size'])} on disk, and only about "
                            f"{human(avail)} is free or reclaimable right now{extra}")
    if m["aux"]:
        warnings.append("this looks like an auxiliary model (embedder, speech, drafter), not a chat model")
    return {"name": t["name"], "port": t["port"], "model": serve_name, "size": m["size"],
            "hint": slot["hint"] if slot else "", "host": _host_of(args),
            "trusted": _trusted_of(args), "log": log,
            "cmd": cmd, "warnings": warnings, "old": srv}, None


def plan_restart(target):
    """Restart = switch the slot to the model it's serving now."""
    t = resolve_target(target)
    if not t:
        return plan_start(target, "", replace=True)          # same "no slot" message
    srv = server_on(t["port"])
    if not srv:
        return None, (f"nothing from rapid-mlx is running on {t['name']} :{t['port']}, "
                      f"so there's nothing to restart. Use start.")
    return plan_start(target, srv["model"], replace=True)


def describe_plan(p):
    """Lines shown before asking to start (or restart / switch)."""
    hint = f"; slot hint {p['hint']}" if p["hint"] else ""
    where = ("this Mac only" if p["host"] in LOOPBACK
             else f"the network (--host {p['host']})")
    if p["host"] not in LOOPBACK and p.get("trusted") and "*" in p["trusted"]:
        where += ", any Host header accepted"
    shown = [shlex.quote(a) for a in ["rapid-mlx"] + p["cmd"][1:]]   # pasteable: '*' stays quoted
    lines = []
    old = p.get("old")
    if old:
        lines.append(f"stops:  {old['model']}  (pid {old['pid']}, up {old['uptime']}); "
                     f"the slot is empty until the new server answers")
    lines += [f"{'starts: ' if old else ''}{p['name']} :{p['port']}  ←  {p['model']}  "
              f"({human(p['size'])} on disk{hint})",
              f"reachable from: {where}",
              f"command: {' '.join(shown)}",
              f"log: {p['log'].replace(os.path.expanduser('~'), '~', 1)}"]
    lines += [f"⚠ {w}" for w in p["warnings"]]
    return lines


def key_label(api_key):
    if api_key == DEFAULT_API_KEY:
        return f'"{DEFAULT_API_KEY}" (the default; clients send it as their API key)'
    return "your own (not shown, not saved)"


def launch(p, api_key=None):
    """Start the server in the background, detached from this terminal.
    The API key goes through the environment, never the command line."""
    os.makedirs(os.path.dirname(p["log"]), exist_ok=True)
    env = dict(os.environ)
    # Always exactly the key chosen at start, never one inherited from the shell.
    env["RAPID_MLX_API_KEY"] = api_key or DEFAULT_API_KEY
    with open(p["log"], "w") as f:            # a fresh log for every start
        f.write(f"# started by mlxsw {time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(shlex.quote(a) for a in p['cmd'])}\n")
    out = open(p["log"], "a")                 # catches anything before --log-file takes over
    try:
        return subprocess.Popen(p["cmd"], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, env=env, start_new_session=True)
    finally:
        out.close()


def _tail(path, n=12):
    try:
        with open(path, errors="replace") as f:
            return [ln.rstrip() for ln in f.readlines()[-n:]]
    except OSError:
        return []


def wait_ready(p, proc, timeout=START_TIMEOUT, say=print):
    """Wait until the new server answers, or exits, or time runs out."""
    t0, last = time.time(), 0.0
    while True:
        elapsed = time.time() - t0
        if proc.poll() is not None:
            say(f"the server exited while starting (code {proc.returncode}). End of {p['log']}:")
            for ln in _tail(p["log"]):
                say("  " + ln)
            return False
        if probe(p["port"], timeout=1.0):
            say(f"serving: {p['model']} on {p['name']} :{p['port']} after {elapsed:.0f}s (pid {proc.pid})")
            return True
        if elapsed >= timeout:
            say(f"not answering after {timeout:g}s. It's still running and may still be loading; "
                f"check {p['log']}")
            return False
        if elapsed - last >= 10:
            say(f"loading … {elapsed:.0f}s")
            last = elapsed
        time.sleep(1)


def execute(p, api_key, say=print):
    """Run a confirmed plan: stop the old server if there is one, then start.
    Returns True when the new server answers."""
    old = p.get("old")
    if old:
        if not stop_server(old, say=say):
            say(f"the old server was not stopped, so nothing was started; "
                f"{p['name']} still has {old['model']}.")
            return False
    proc = launch(p, api_key)
    say(f"started pid {proc.pid}; waiting for it to answer (up to {START_TIMEOUT:g}s) …")
    ok = wait_ready(p, proc, say=say)
    if not ok and old and proc.poll() is not None:
        say(f"{p['name']} is now empty. To bring back what it had:  "
            f"mlxsw.py start {p['name']} {old['model']}")
    return ok


def _confirm_and_run(p, assume_yes, verb):
    for line in describe_plan(p):
        print(line)
    if assume_yes:
        from_env = os.environ.get("RAPID_MLX_API_KEY")
        api_key = from_env or DEFAULT_API_KEY
        print(f"API key: {key_label(api_key)}" + (" (from RAPID_MLX_API_KEY)" if from_env else ""))
    else:
        if not sys.stdin.isatty():
            sys.exit("mlxsw: can't ask for confirmation here (not a terminal); add -y to confirm.")
        import getpass
        api_key = getpass.getpass(f'API key (hidden; Enter = "{DEFAULT_API_KEY}"): ').strip() or DEFAULT_API_KEY
        print(f"API key: {key_label(api_key)}")
        if input(f"{verb.capitalize()} it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled; nothing was changed.")
            return 0
    try:
        return 0 if execute(p, api_key) else 1
    except KeyboardInterrupt:
        print("\nStopped waiting. The server keeps loading in the background.")
        return 0


def _plan_or_exit(result):
    p, problem = result
    if problem:
        sys.exit(f"mlxsw: {problem}")
    return p


def cmd_start(target, model_name, assume_yes=False):
    return _confirm_and_run(_plan_or_exit(plan_start(target, model_name)), assume_yes, "start")


def cmd_switch(target, model_name, assume_yes=False):
    return _confirm_and_run(_plan_or_exit(plan_start(target, model_name, replace=True)), assume_yes, "switch")


def cmd_restart(target, assume_yes=False):
    return _confirm_and_run(_plan_or_exit(plan_restart(target)), assume_yes, "restart")


def find_rapid_mlx():
    """Find rapid-mlx on PATH and check that it runs.

    Returns (path, version string). Exits with a clear message otherwise.
    """
    path = shutil.which("rapid-mlx")
    if not path:
        sys.exit(
            "mlxsw: 'rapid-mlx' was not found on PATH.\n"
            f"  PATH = {os.environ.get('PATH', '(empty)')}\n"
            "  Install Rapid-MLX, or add the folder that contains it to PATH.\n"
            "  (IDE run configurations often get a shorter PATH than your login shell.)"
        )
    try:
        p = subprocess.run([path, "version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.exit(f"mlxsw: found rapid-mlx at {path}, but it could not be run: {e}")
    out = strip_ansi(p.stdout + p.stderr).strip()
    if p.returncode != 0:
        sys.exit(
            f"mlxsw: found rapid-mlx at {path}, but `rapid-mlx version` "
            f"exited with code {p.returncode}:\n  {out[-400:] or '(no output)'}"
        )
    found = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("rapid-mlx")]
    return path, (found[-1] if found else "")


def main():
    ap = argparse.ArgumentParser(description="rapid-mlx slot status and control")
    ap.add_argument("command", nargs="?", choices=["tui", "info", "stop", "start", "restart", "switch"],
                    help="tui: interactive shell (optional, needs `pip install textual`); "
                         "info [SLOT]: details a slot's server reports (memory, prefix cache, traffic); "
                         "stop SLOT; start SLOT MODEL; restart SLOT (same model, fresh process); "
                         "switch SLOT MODEL (stop the slot's server, start MODEL)")
    ap.add_argument("target", nargs="?", metavar="SLOT",
                    help="slot name or port number")
    ap.add_argument("model", nargs="?", metavar="MODEL",
                    help="model alias or HF repo, as in the list on disk (for start and switch)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help='don\'t ask; for start, the API key comes from RAPID_MLX_API_KEY if set, else "none"')
    ap.add_argument("-w", "--watch", nargs="?", const=5.0, type=float, metavar="SECS",
                    help="refresh every SECS seconds (default 5)")
    ap.add_argument("--json", action="store_true", help="print collected state as JSON")
    args = ap.parse_args()
    if args.command == "stop" and (not args.target or args.model):
        ap.error("usage: mlxsw.py stop SLOT   e.g.  mlxsw.py stop SMALL")
    if args.command == "start" and not (args.target and args.model):
        ap.error("usage: mlxsw.py start SLOT MODEL   e.g.  mlxsw.py start SMALL gemma-4-e2b-4bit")
    if args.command == "switch" and not (args.target and args.model):
        ap.error("usage: mlxsw.py switch SLOT MODEL   e.g.  mlxsw.py switch LARGE qwen3-coder-30b-4bit")
    if args.command == "restart" and (not args.target or args.model):
        ap.error("usage: mlxsw.py restart SLOT   e.g.  mlxsw.py restart SMALL")
    if args.command == "info" and args.model:
        ap.error("usage: mlxsw.py info [SLOT]   e.g.  mlxsw.py info LARGE")
    if args.target and args.command not in ("info", "stop", "start", "restart", "switch"):
        ap.error(f"unexpected argument {args.target!r}")

    # Check the optional dependency before touching rapid-mlx, so the
    # message is about the real problem.
    if args.command == "tui":
        import importlib.util
        if importlib.util.find_spec("textual") is None:
            sys.exit("mlxsw: the tui needs Textual, which isn't installed.\n"
                     "  pip install textual\n"
                     "  (everything else in mlxsw works without it)")

    global RAPID_MLX, SLOTS, CONFIG_SOURCE
    SLOTS, CONFIG_SOURCE = load_slots()
    RAPID_MLX, version = find_rapid_mlx()

    if args.command == "info":
        sys.exit(cmd_info(args.target, as_json=args.json))
    if args.command == "stop":
        sys.exit(cmd_stop(args.target, assume_yes=args.yes))
    if args.command == "start":
        sys.exit(cmd_start(args.target, args.model, assume_yes=args.yes))
    if args.command == "switch":
        sys.exit(cmd_switch(args.target, args.model, assume_yes=args.yes))
    if args.command == "restart":
        sys.exit(cmd_restart(args.target, assume_yes=args.yes))

    if args.command == "tui":
        import mlxsw_tui
        # Hand over this module itself: `import mlxsw` from the tui would
        # load a second, uninitialised copy when this file runs as __main__.
        mlxsw_tui.run(sys.modules[__name__], version, args.watch or 5.0)
        return

    if args.json:
        print(json.dumps(collect(), indent=2, default=str))
        return

    if not args.watch:
        print(render(collect(), version))
        return

    # Live view: the model list changes rarely, so re-read it once a minute;
    # `ps` and the port probes run every tick.
    models, models_at, tick = None, 0.0, 0
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    try:
        while True:
            if models is None or time.time() - models_at > 60:
                models, models_at = parse_ls(run([RAPID_MLX, "ls"], timeout=60)), time.time()
            live = green(("●" if tick % 2 == 0 else "○") + f" LIVE · every {args.watch:g}s")
            tick += 1
            frame = render(collect([dict(m) for m in models]), version, live)
            sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n\n" + dim(f" refresh {args.watch:g}s · Ctrl-C to quit") + "\n")
            sys.stdout.flush()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()