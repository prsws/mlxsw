"""mlxsw tui - optional interactive shell around mlxsw, built on Textual.

Started with `mlxsw.py tui`. Needs `pip install textual`; the rest of
mlxsw keeps working without it.

This first version is read-only. Commands: /help, /refresh, /quit.
"""
import time

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
    "/quit": "leave mlxsw (also /exit, Ctrl+Q)",
}
ALIASES = {"/exit": "/quit", "/q": "/quit", "/h": "/help", "/?": "/help", "/r": "/refresh"}


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

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="status-wrap"):
            yield Static("reading rapid-mlx …", id="status")
        yield RichLog(id="log", wrap=True, markup=True)
        yield CommandInput(
            placeholder="type / for commands  (Tab completes)",
            id="cmd",
            suggester=SuggestFromList(list(COMMANDS) + list(ALIASES), case_sensitive=False),
        )
        yield Footer()

    def on_mount(self) -> None:
        self.core.C.on = True       # render() emits ANSI colour; Rich converts it
        self.query_one("#status-wrap").border_title = "status"
        self.query_one("#log").border_title = "output"
        self.query_one("#cmd", Input).focus()
        self.say("[b]mlxsw[/b] shell (read-only for now). Type [b]/help[/b] for commands.")
        self.refresh_status(force_models=True)
        self.set_interval(self.interval, self.refresh_status)

    # ------------------------------------------------------------ status

    @work(thread=True, exclusive=True)
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

    # ------------------------------------------------------------ commands

    def say(self, markup: str) -> None:
        self.query_one("#log", RichLog).write(markup)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        event.input.value = ""
        if not raw:
            return
        self.say(f"[dim]›[/] {raw}")
        if not raw.startswith("/"):
            self.say("[yellow]Commands start with /. Try /help.[/]")
            return
        word = raw.split()[0].lower()
        cmd = ALIASES.get(word, word)
        if cmd == "/help":
            for name, text in COMMANDS.items():
                self.say(f"  [b]{name:<9}[/b] {text}")
        elif cmd == "/refresh":
            self.action_refresh_now()
        elif cmd == "/quit":
            self.exit()
        else:
            self.say(f"[red]Unknown command {word}.[/] Try /help.")


def run(core, version, interval=5.0):
    MlxswApp(core, version, interval).run()