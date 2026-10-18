# mlxsw

A small terminal switcher for Rapid-MLX model servers on an Apple Silicon Mac. Needed in practice because rapid-mlx doesn't currently hotswap models in a running instance.

It shows which models are cached, which are running on which port, whether each server is actually answering, and how memory is being used. All of it fits on one screen, refreshed live if you want.

Built for a Mac Studio (M1 Ultra, 64 GB) that serves up to three slots of different sizes.

> **Status:** a status view, plus **starting**, **stopping**, **restarting** and **switching** a slot's server.

## Requirements

- macOS on Apple Silicon
- Rapid-MLX installed (tested with 0.16.2), with `rapid-mlx` on your `PATH`
- Python 3.8+ (standard library only, nothing to install)
- Optional, for the interactive shell only: Python 3.9+ and [Textual](https://textual.textualize.io/) (`pip install -r requirements-tui.txt`)

## Install

```sh
git clone https://github.com/prsws/mlxsw.git ~/mlxsw
chmod +x ~/mlxsw/mlxsw.py
```

mlxsw finds `rapid-mlx` through your `PATH`. At startup it runs `rapid-mlx version` to make sure the binary works. If `rapid-mlx` isn't found, or the check fails, mlxsw stops with a message saying which. Check where your shell finds it with:

```sh
command -v rapid-mlx
```

**Running from an IDE:** PyCharm and other GUI apps often start with a shorter `PATH` than your terminal. If mlxsw says `rapid-mlx` isn't on PATH, add the folder that `command -v rapid-mlx` shows to `PATH` in the run configuration's environment variables.

## Usage

```sh
mlxsw.py              # one-shot view
mlxsw.py -w           # live view, refresh every 5 s (Ctrl-C to quit)
mlxsw.py -w 2         # live view, refresh every 2 s
mlxsw.py --json       # collected state as JSON, for scripts
```

Set `NO_COLOR=1` to turn off colors.

### Interactive shell (optional)

```sh
python3 -m pip install -r requirements-tui.txt   # once; installs Textual
mlxsw.py tui                                     # refresh every 5 s
mlxsw.py tui -w 2                                # refresh every 2 s
```

Install Textual for the same `python3` you run mlxsw with. If pip refuses with `externally-managed-environment` (common with Homebrew Python), install into your user folder instead:

```sh
python3 -m pip install --user --break-system-packages -r requirements-tui.txt
```

A full-screen shell with the live status on top, an output log, and a command box at the bottom. Type `/` and the matching command appears greyed out; Tab completes it.

| Command    | Short      | What it does |
|------------|------------|--------------|
| `/help`    | `/h`, `/?` | list the commands |
| `/refresh` | `/r`       | re-read the model list and status now (also Ctrl+R) |
| `/start SLOT MODEL` |    | start a model that's on disk in an empty slot; asks for an API key (default `none`), then y/N (see below) |
| `/restart SLOT` |       | same model, fresh process; asks first (see below) |
| `/switch SLOT MODEL` |  | stop the slot's server, start MODEL in it; asks first (see below) |
| `/stop SLOT` |          | stop the server in a slot; asks first (see below) |
| `/quit`    | `/q`, `/exit` | leave (also Ctrl+Q) |

### Starting a model

```sh
mlxsw.py start SMALL gemma-4-e2b-4bit     # alias or HF repo, as listed on disk
mlxsw.py start SMALL gemma-4-e2b-4bit -y  # no questions (see the API key below)
```

In the shell it's `/start SMALL gemma-4-e2b-4bit`; Tab completes slot and model names.

Before asking anything, mlxsw checks that:

- the model is **on disk** (mlxsw never downloads; use `rapid-mlx pull` first);
- the slot is **empty** and its port is free (stop the old server first);
- it will probably **fit**: if the model is bigger than free + reclaimable memory, you get a warning, and you can still go ahead.

Then it shows the slot, the model and its size (next to the slot's hint), who will be able to reach the server, the exact command, and where the log goes. It asks for an **API key**, then for confirmation.

Every start uses the slot's `args` from the config, plus these, which can't be changed:

| Option | Why |
|---|---|
| `--port <slot port>` | always explicit, so the server can't drift to another port |
| `--disable-model-downloads` | the server never downloads a model, even if a client asks for one |
| `--log-file ~/Library/Logs/mlxsw/SLOT.log` | a fresh log on every start, visible in Console.app |

The server runs in the background, detached from your terminal, and keeps running after mlxsw exits. mlxsw waits until it answers (up to 5 minutes; Ctrl-C stops the waiting, not the server). If it exits while starting, mlxsw shows the end of its log.

#### API key

Every server mlxsw starts has an API key.

- You're asked for it on every start, with typing hidden. **Enter uses the default key, the literal four-character string `none`**, which is what many OpenAI-style clients send for local servers.
- Clients must send the key as `Authorization: Bearer <key>`. **A client that sends no key at all gets `401 Unauthorized`**, even with the default `none`. Set the key to `none` in each client's settings.
- Your own key goes to the server through the `RAPID_MLX_API_KEY` environment variable, **never** on the command line, so it doesn't appear in `ps`, in the log or on screen. mlxsw never stores it. (The default `none` is public, so mlxsw shows it.)
- The server always gets exactly the key chosen at start; a `RAPID_MLX_API_KEY` set in your shell is ignored, except with `-y`.
- With `-y` there's no question: the key comes from `RAPID_MLX_API_KEY` if your shell has it, otherwise `none`.
- A server with a key shows as `● serving … [key]`, since mlxsw's own status check is refused without it.

### Restarting and switching

```sh
mlxsw.py restart SMALL                    # same model, fresh process
mlxsw.py switch LARGE qwen3-coder-30b-4bit
```

In the shell: `/restart SMALL`, `/switch LARGE qwen3-coder-30b-4bit`.

Both are a stop followed by a start in the same slot:

- **Every question comes first.** You see what will be stopped and what will start, then give the API key and confirm, **before** anything is stopped. After that it runs straight through.
- **Stop first, then start.** The new server needs the slot's port, so the slot is briefly empty. That's also the order that never needs two big models in memory at once.
- **The memory check counts the old server's memory as freed**, so swapping one 30B model for another doesn't warn for nothing.
- **The new server uses the slot's `args` from the config**, not the old server's command line. Restarting a server you started by hand is a way to bring it under mlxsw: logs, no downloads, API key.
- If the old server **doesn't stop**, nothing is started and the slot keeps what it had.
- If the new model **fails to start** after a clean stop, the slot is left empty, and mlxsw prints the `start` command that brings back what was there.
- `restart` of an empty slot is refused (there's nothing to restart). `switch` into an empty slot works like `start`.

### Stopping a slot

```sh
mlxsw.py stop SMALL        # asks before stopping
mlxsw.py stop small -y     # no question; slot names are case-insensitive
mlxsw.py stop 8002         # a port number works too, even outside your slots
```

In the shell it's `/stop SMALL`, then `y` to confirm (anything else cancels).

Before asking, mlxsw shows what it's about to stop, including the server's **full launch command**, so you can copy it to start the server again later.

When you confirm, it:

1. checks the PID really is a `rapid-mlx serve` process, and refuses otherwise;
2. sends it a polite stop signal (SIGTERM);
3. waits up to 30 s until the process has exited **and** its port is free.

It never force-kills. If the server doesn't stop in time (for example, while it's busy with a long request), mlxsw says so and gives you the `kill -9` command to use yourself if it's really stuck.

If Textual isn't installed, `mlxsw.py tui` says so and stops. The other modes never need it.

## What it shows

```
 mlxsw · macstudio · rapid-mlx 0.16.2   ● LIVE · every 2s                    2026-10-17 06:56:35
 RAM █████████████████████████▒▒▒▒▒▒▒▒▒▒▒▒▒░░  used 40.3 GiB of 64.0 GiB
     ■ mlx 33.4 GiB  ■ other 6.9 GiB (compressed 523.5 MiB, wired 2.2 GiB)  ▒ cache 21.5 GiB  ░ free 2.2 GiB

 SLOTS
  LARGE   :8000  ● serving   muse-glimmer-30b-4bit   up 39m36s  pid 5547  rss 16.6 GiB  +emb embeddinggemma-2-bf16
  MIDDLE  :8001  ● serving   gemma-4-12b-qat-4bit    up 1m58s   pid 6942  rss 11.9 GiB
  SMALL   :8002  ● serving   gemma-4-e2b-4bit        up 1m28s   pid 6944  rss 4.9 GiB

 MODELS ON DISK  10 LLMs · 5 aux · 148.5 GiB total
  ▶8000  muse-glimmer-30b-4bit   mlx-community/Muse-Glimmer-30B-4bit   18.1 GiB  59d ago
  ...
```

### Slots

| Slot   | Port | Intended size |
|--------|------|---------------|
| LARGE  | 8000 | ~30B          |
| MIDDLE | 8001 | ~12B          |
| SMALL  | 8002 | ~2–4B         |

All three rarely fit in 64 GB at the same time. They're defined so the capability is there; which ones run is up to you. A server on any other port still shows up, as `OTHER`.

These are the built-in defaults: three slots, each started with `--host 0.0.0.0 --trusted-hosts '*'` (see [Network access](#network-access)). To use your own, create `~/.config/mlxsw/slots.json` (or `$XDG_CONFIG_HOME/mlxsw/slots.json`):

```json
{
  "slots": [
    { "name": "LARGE",  "port": 8000, "hint": "~30B",
      "args": ["--host", "0.0.0.0", "--trusted-hosts", "macstudio", "macstudio.local", "192.168.1.20",
               "--log-level", "WARNING"] },
    { "name": "MIDDLE", "port": 8001, "hint": "~12B" },
    { "name": "SMALL",  "port": 8002, "hint": "~2-4B", "args": [] }
  ]
}
```

- `name` and `port` are required; `hint` is optional and shows next to an empty slot.
- `args` is optional: extra `rapid-mlx serve` options used when mlxsw **starts** a model in that slot.
  - **No `args`** (MIDDLE above): the built-in network default, `--host 0.0.0.0 --trusted-hosts '*'`.
  - **Your own `args`** (LARGE above): used exactly as written. Nothing from the default is added, so include `--host` and `--trusted-hosts` yourself if you want network access.
  - **`"args": []`** (SMALL above): nothing extra, which is rapid-mlx's own default: this Mac only.
  - `--port`, `--log-file`, `--disable-model-downloads` and `--api-key` aren't allowed here, because mlxsw sets the first three itself and asks for the key at start.
- Names are shown in capitals, and each name and port can only be used once.
- If the file has a mistake, mlxsw stops and says what's wrong instead of guessing.
- `--json` output and the TUI's first message both say where the slots came from.

Each slot has one of four statuses:

| Status      | Meaning |
|-------------|---------|
| ● serving   | listed by `rapid-mlx ps` **and** answering `GET /v1/models` |
| ◐ loading   | process is up but the port isn't answering yet (or it's stuck) |
| ? foreign   | something answers on the port that `rapid-mlx ps` doesn't list |
| ○ empty     | nothing there |

### Network access

**The built-in default is meant for an internal, protected network** (a home or lab LAN behind a firewall) where the Mac is an inference server for other machines. Without any configuration, a server started by mlxsw:

- listens on all interfaces (`--host 0.0.0.0`), so other machines can reach it, and
- accepts any `Host:` header (`--trusted-hosts '*'`), so clients can use the Mac's IP, its `.local` name or any DNS name.

Why `*`: rapid-mlx checks the hostname in each request against `--trusted-hosts` (DNS-rebinding protection) and answers `400 Invalid host header` for any name not on the list. Its own default list is localhost only. A list of IPs fails as soon as a client uses a name, and the error doesn't say which name was wrong. `*` switches that check off, so the **API key is the only protection** (every server mlxsw starts has one; see [API key](#api-key)). With the default key `none`, anyone who can reach the port can use the server. On a network you don't fully trust, type a real key at the start prompt, or change the slot's `args`.

**What you put in `slots.json` rules.** A slot with its own `args` gets exactly those, and the default isn't merged in. The start plan always shows who can reach the server ("reachable from: …") and the exact command before you confirm.

Narrower setups, as `args` in `slots.json`:

```jsonc
"args": ["--host", "0.0.0.0", "--trusted-hosts", "macstudio", "macstudio.local", "192.168.1.20"]   // the network, but only these names
"args": ["--host", "127.0.0.1"]                                                                       // this Mac only
"args": []                                                                                             // this Mac only (rapid-mlx's default)
```

(Earlier versions started a slot without `args` for this Mac only. To get that back, give the slot `"args": []`.)

If you start rapid-mlx by hand, quote the star so the shell doesn't expand it: `--trusted-hosts '*'`.

### RAM bar

Memory is counted the way Activity Monitor counts it, from `vm_stat`. The four parts add up to total RAM:

- **mlx**: combined resident memory of the rapid-mlx server processes
- **other**: everything else in use (app memory + wired + compressed)
- **cache**: file cache, which macOS reclaims when needed
- **free**

The compressed figure is worth watching. A model that has been idle for a while can get pushed into the compressor, and the next request is slower while it gets decompressed. When a slot's server holds less than 60% of its model in RAM, its row gets a warning such as `⚠ 39% in RAM`, with a note underneath explaining it.

### Models on disk

Everything `rapid-mlx ls` reports. Chat LLMs come first, then aux models (embedders, Whisper, speculative drafters, anything under 2 GB). Loaded models are marked with their port. A model with no alias shows as `(unmapped)` and can't be started by name with `rapid-mlx serve`.

## How it works

- `rapid-mlx ls` and `rapid-mlx ps` have no JSON output, so their tables are parsed with regular expressions. Long aliases shift the columns and long repo names get truncated with `...`, which the parser handles.
- Each slot's port is probed with `GET /v1/models` (2-second timeout).
- `ps -o rss,command` gives each server's memory and its launch flags (such as `--embedding-model`).
- `sysctl` and `vm_stat` give the memory breakdown.
- In live mode the model list is re-read once a minute. Everything else is read on every refresh.

## Known quirks

- `rapid-mlx ps` sometimes prints an ASCII logo before its table. The parser skips it.
- Rapid-MLX is moving fast. If a release changes the `ls` or `ps` table layout, the parser is what will need updating.

## Roadmap

- [x] Status view, with live mode
- [x] Activity Monitor–style RAM breakdown
- [x] Three slots (LARGE / MIDDLE / SMALL)
- [x] Optional interactive shell (`mlxsw.py tui`)
- [x] Slot layout in a config file
- [x] Warning when a loaded model is mostly out of RAM
- [x] Stop a slot's server (`stop` / `/stop`)
- [x] Start a model in a slot (`start` / `/start`), always with an API key (default `none`)
- [x] Restart a slot, or switch it to another model, in one step

## License

MIT. See [LICENSE](LICENSE).