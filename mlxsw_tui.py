"""mlxsw tui - optional interactive shell around mlxsw, built on Textual.

Started with `mlxsw.py tui`. Needs `pip install textual`; the rest of
mlxsw keeps working without it.

Commands: /help, /refresh, /stop SLOT, /quit.
"""
import time

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Input, RichLog, Static

COMMANDS = {
    "/help": "show this list",
    "/refresh": "re-read the model list and status now",
    "/stop SLOT": "stop the server in a slot (asks first; slot name or port)",
    "/quit": "leave mlxsw (also /exit, Ctrl+Q)",
}
ALIASES = {"/exit": "/quit", "/q": "/quit", "/h": "/help", "/?": "/help", "/r": "/refresh"}
PLACEHOLDER = "type / for commands  (Tab completes)"


class CommandInput(Input):
    """Input where Tab accepts the greyed-out suggestion (Textual uses → by default)."""

    BINDINGS = [Binding("tab", "complete", "Complete", show=False, priority=True)]

    def action_complete(self) -> None:
        self.cursor_position = len(self.value)
        self.action_cursor_right()   # at end of text, this accepts the suggestion


class MlxswApp(App):
    TITLE = "mlxsw"

    CSS = """
    /* Always reserve the scrollbar's columns, so the width the status is
       drawn for doesn't change when the scrollbar comes and goes. */
    #status-wrap { height: 1fr; border: round $primary; padding: 0 1; scrollbar-gutter: stable; }
    #log { height: 7; border: round $secondary; padding: 0 1; }
    #cmd { margin: 0 0; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+r", "refresh_now", "Refresh"),
    ]

    def __init__(self, core, version, interval=5.0):
        super().__init__()
        self.core = core            # the already-initialised mlxsw module
        self.version = version
        self.interval = interval
        self.models = None
        self.models_at = 0.0
        self.tick = 0
        self.pending = None         # (slot, server) waiting for a y/N answer
        self.busy = False           # a stop is in progress

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        words = [c.split()[0] for c in COMMANDS] + list(ALIASES)
        words += [f"/stop {s['name']}" for s in self.core.SLOTS]
        with VerticalScroll(id="status-wrap"):
            yield Static("reading rapid-mlx …", id="status")
        yield RichLog(id="log", wrap=True, markup=True)
        yield CommandInput(
            placeholder=PLACEHOLDER,
            id="cmd",
            suggester=SuggestFromList(words, case_sensitive=False),
        )
        yield Footer()

    def on_mount(self) -> None:
        self.core.C.on = True       # render() emits ANSI colour; Rich converts it
        self.query_one("#status-wrap").border_title = "status"
        self.query_one("#log").border_title = "output"
        self.query_one("#cmd", Input).focus()
        self.say("[b]mlxsw[/b] shell. Type [b]/help[/b] for commands.")
        self.say(f"[dim]slots from {escape(self.core.CONFIG_SOURCE)}[/]")
        self.refresh_status(force_models=True)
        self.set_interval(self.interval, self.refresh_status)

    # ------------------------------------------------------------ status

    @work(thread=True, exclusive=True, group="status")
    def refresh_status(self, force_models: bool = False) -> None:
        core = self.core
        if force_models or self.models is None or time.time() - self.models_at > 60:
            self.models = core.parse_ls(core.run([core.RAPID_MLX, "ls"], timeout=60))
            self.models_at = time.time()
        state = core.collect([dict(m) for m in self.models])
        dot = "●" if self.tick % 2 == 0 else "○"
        self.tick += 1
        live = core.green(f"{dot} LIVE · every {self.interval:g}s")
        width = max(self.query_one("#status").size.width, 60)
        frame = core.render(state, self.version, live, cols=width)
        self.call_from_thread(self.query_one("#status", Static).update, Text.from_ansi(frame))

    def action_refresh_now(self) -> None:
        self.say("refreshing …")
        self.refresh_status(force_models=True)

    # ------------------------------------------------------------ output

    def say(self, markup: str) -> None:
        self.query_one("#log", RichLog).write(markup)

    def say_plain(self, text: str) -> None:
        """For text from outside (command lines, errors): never read as markup."""
        self.say(escape(text))

    def say_from_thread(self, text: str) -> None:
        self.call_from_thread(self.say_plain, text)

    def set_placeholder(self, text: str) -> None:
        self.query_one("#cmd", Input).placeholder = text

    # ------------------------------------------------------------ stop

    @work(thread=True, group="action")
    def prepare_stop(self, target: str) -> None:
        core = self.core
        t = core.resolve_target(target)
        if not t:
            names = ", ".join(s["name"] for s in core.SLOTS)
            self.say_from_thread(f"No slot called {target!r}. Slots: {names} (or give a port number).")
            return
        srv = core.server_on(t["port"])
        if not srv:
            self.say_from_thread(f"Nothing from rapid-mlx is running on {t['name']} :{t['port']}.")
            return
        for line in core.describe_server(t, srv):
            self.say_from_thread(line)
        self.call_from_thread(self._ask_stop, t, srv)

    def _ask_stop(self, t, srv) -> None:
        self.pending = (t, srv)
        self.say(f"[yellow]Stop {escape(t['name'])}? Type y to confirm; anything else cancels.[/]")
        self.set_placeholder(f"y = stop {t['name']} :{t['port']}   anything else = cancel")

    @work(thread=True, group="action")
    def do_stop(self, t, srv) -> None:
        self.busy = True
        try:
            ok = self.core.stop_server(srv, say=self.say_from_thread)
        finally:
            self.busy = False
        if not ok:
            self.say_from_thread(f"{t['name']} was not stopped cleanly; see above.")
        self.call_from_thread(self.refresh_status)

    # ------------------------------------------------------------ input

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        event.input.value = ""

        if self.pending:                          # answering "Stop X?"
            t, srv = self.pending
            self.pending = None
            self.set_placeholder(PLACEHOLDER)
            self.say(f"[dim]›[/] {escape(raw)}")
            if raw.lower() in ("y", "yes"):
                self.do_stop(t, srv)
            else:
                self.say("Cancelled; nothing was stopped.")
            return

        if not raw:
            return
        self.say(f"[dim]›[/] {escape(raw)}")
        if not raw.startswith("/"):
            self.say("[yellow]Commands start with /. Try /help.[/]")
            return
        parts = raw.split()
        word = parts[0].lower()
        cmd = ALIASES.get(word, word)
        if cmd == "/help":
            for name, text in COMMANDS.items():
                self.say(f"  [b]{escape(name):<11}[/b] {text}")
        elif cmd == "/refresh":
            self.action_refresh_now()
        elif cmd == "/stop":
            if len(parts) != 2:
                self.say("Usage: /stop SLOT   (a slot name like SMALL, or a port like 8002)")
            elif self.busy:
                self.say("A stop is already in progress; wait for it to finish.")
            else:
                self.prepare_stop(parts[1])
        elif cmd == "/quit":
            if self.busy:
                self.say("[yellow]A stop is in progress; quitting now won't undo it.[/]")
            self.exit()
        else:
            self.say(f"[red]Unknown command {escape(word)}.[/] Try /help.")


def run(core, version, interval=5.0):
    MlxswApp(core, version, interval).run()