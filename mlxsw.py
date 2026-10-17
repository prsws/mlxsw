#!/usr/bin/env python3
"""mlxsw - rapid-mlx model switcher for the Mac Studio.

Step 1: read-only status view.
  - `rapid-mlx ls`  -> models in the HF cache
  - `rapid-mlx ps`  -> running servers (pid, port, model, uptime)
  - GET :PORT/v1/models -> is the server actually answering?
  - ps / sysctl / vm_stat -> process and memory figures

Usage:
  mlxsw.py              one-shot view
  mlxsw.py -w [SECS]    live view, refresh every SECS (default 5), Ctrl-C to quit
  mlxsw.py --json       dump the collected state as JSON
  mlxsw.py tui [-w N]   interactive shell (optional; needs `pip install textual`)

Stdlib only (the optional tui excepted). Nothing here starts, stops or
changes anything.
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

# Resolved from PATH at startup by find_rapid_mlx(); never hard-coded.
RAPID_MLX = None

# Slot layout. Read from slots.json in the config folder by load_slots();
# these defaults apply only when that file doesn't exist.
DEFAULT_SLOTS = [
    {"name": "LARGE", "port": 8000, "hint": "~30B"},
    {"name": "MIDDLE", "port": 8001, "hint": "~12B"},
    {"name": "SMALL", "port": 8002, "hint": "~2-4B"},
]
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
    """Ask the server what it is serving. None = not answering."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout) as r:
            data = json.load(r).get("data", [])
        return {
            "ids": [d.get("id") for d in data],
            "moe": any(d.get("is_moe") for d in data),
        }
    except Exception:
        return None


def proc_info(pid):
    out = run(["/bin/ps", "-o", "rss=,command=", "-p", str(pid)], timeout=5).strip()
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
        name = name.strip().upper()
        if name in names:
            bad(f"slot name {name!r} is used twice")
        if port in ports:
            bad(f"port {port} is used by two slots")
        names.add(name)
        ports.add(port)
        slots.append({"name": name, "port": port, "hint": hint})
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
    ap = argparse.ArgumentParser(description="rapid-mlx slot status (read-only)")
    ap.add_argument("command", nargs="?", choices=["tui"],
                    help="tui: interactive shell (optional, needs `pip install textual`)")
    ap.add_argument("-w", "--watch", nargs="?", const=5.0, type=float, metavar="SECS",
                    help="refresh every SECS seconds (default 5)")
    ap.add_argument("--json", action="store_true", help="print collected state as JSON")
    args = ap.parse_args()

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