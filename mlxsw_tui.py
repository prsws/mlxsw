"""mlxsw tui - optional interactive shell around mlxsw, built on Textual.

Started with `mlxsw.py tui`. Needs `pip install textual`; the rest of
mlxsw keeps working without it.

Commands: /help, /refresh, /start, /restart, /switch, /stop, /quit.
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
    "/start SLOT MODEL": "start a model that's on disk in an empty slot (asks for an API key, default \"none\", then y/N)",
    "/restart SLOT": "same model, fresh process: stop, then start (asks first)",
    "/switch SLOT MODEL": "stop the slot's server, then start MODEL in it (asks first)",
    "/stop SLOT": "stop the server in a slot (asks first; slot name or port)",
    "/quit": "leave mlxsw (also /exit, Ctrl+Q); started servers keep running",
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
        # A question waiting for an answer:
        #   ("stop", target, server)        y/N
        #   ("key", plan, verb)             API key, hidden; Enter = "none"
        #   ("run", plan, api_key, verb)    y/N   (verb: start / restart / switch)
        self.pending = None
        self.busy = False           # a stop or start is in progress

    # ------------------------------------------------------------ layout

    def suggestions(self):
        words = [c.split()[0] for c in COMMANDS] + list(ALIASES)
        words += [f"/{verb} {s['name']}" for verb in ("stop", "restart") for s in self.core.SLOTS]
        names = [m["alias"] or m["repo"] for m in (self.models or []) if not m["aux"]]
        for verb in ("start", "switch"):
            words += [f"/{verb} {s['name']} {n}" for s in self.core.SLOTS for n in names]
            words += [f"/{verb} {s['name']}" for s in self.core.SLOTS]
        return words

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="status-wrap"):
            yield Static("reading rapid-mlx …", id="status")
        yield RichLog(id="log", wrap=True, markup=True)
        yield CommandInput(placeholder=PLACEHOLDER, id="cmd",
                           suggester=SuggestFromList(self.suggestions(), case_sensitive=False))
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
            first = self.models is None
            self.models = core.parse_ls(core.run([core.RAPID_MLX, "ls"], timeout=60))
            self.models_at = time.time()
            if first:   # model names are known now: offer them for /start
                self.call_from_thread(self._update_suggestions)
        state = core.collect([dict(m) for m in self.models])
        dot = "●" if self.tick % 2 == 0 else "○"
        self.tick += 1
        live = core.green(f"{dot} LIVE · every {self.interval:g}s")
        width = max(self.query_one("#status").size.width, 60)
        frame = core.render(state, self.version, live, cols=width)
        self.call_from_thread(self.query_one("#status", Static).update, Text.from_ansi(frame))

    def _update_suggestions(self) -> None:
        self.query_one("#cmd", Input).suggester = SuggestFromList(self.suggestions(), case_sensitive=False)

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

    def ask(self, question: str, placeholder: str, pending, secret: bool = False) -> None:
        self.pending = pending
        cmd = self.query_one("#cmd", Input)
        cmd.password = secret
        cmd.placeholder = placeholder
        self.say(f"[yellow]{escape(question)}[/]")

    def clear_question(self) -> None:
        self.pending = None
        cmd = self.query_one("#cmd", Input)
        cmd.password = False
        cmd.placeholder = PLACEHOLDER

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
        self.call_from_thread(self.ask, f"Stop {t['name']}? Type y to confirm; anything else cancels.",
                              f"y = stop {t['name']} :{t['port']}   anything else = cancel",
                              ("stop", t, srv))

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

    # ------------------------------------------------------------ start / restart / switch

    @work(thread=True, group="action")
    def prepare_plan(self, verb: str, target: str, model: str = "") -> None:
        """Check everything for start/restart/switch, show the plan, then ask for the key."""
        core = self.core
        if verb == "restart":
            plan, problem = core.plan_restart(target)
        else:
            plan, problem = core.plan_start(target, model, replace=(verb == "switch"))
        if problem:
            self.say_from_thread(problem)
            return
        for line in core.describe_plan(plan):
            self.say_from_thread(line)
        default = core.DEFAULT_API_KEY
        self.call_from_thread(self.ask, f'API key for the new server? Typing is hidden; Enter = "{default}".',
                              f'API key (hidden)   Enter = "{default}"', ("key", plan, verb), True)

    @work(thread=True, group="action")
    def do_execute(self, plan, api_key, verb) -> None:
        self.busy = True
        try:
            ok = self.core.execute(plan, api_key, say=self.say_from_thread)
        except Exception as e:                      # never leave busy stuck
            self.say_from_thread(f"{verb} failed: {e}")
            ok = False
        finally:
            self.busy = False
        if not ok:
            self.say_from_thread(f"{verb} of {plan['name']} did not finish cleanly; see above.")
        self.call_from_thread(self.refresh_status)

    # ------------------------------------------------------------ input

    def answer(self, raw: str) -> None:
        """Handle the reply to whichever question is pending."""
        kind, *rest = self.pending
        if kind == "key":
            plan, verb = rest
            api_key = raw or self.core.DEFAULT_API_KEY
            self.clear_question()
            self.say(f"[dim]›[/] API key: {self.core.key_label(api_key)}")   # never the key itself
            what = (f"{verb.capitalize()} {plan['name']}" if verb == "restart"
                    else f"{verb.capitalize()} {plan['name']} to {plan['model']}" if verb == "switch"
                    else f"Start {plan['model']} in {plan['name']}")
            self.ask(f"{what}? Type y to confirm; anything else cancels.",
                     f"y = {verb} {plan['name']} :{plan['port']}   anything else = cancel",
                     ("run", plan, api_key, verb))
            return
        self.clear_question()
        self.say(f"[dim]›[/] {escape(raw)}")
        yes = raw.lower() in ("y", "yes")
        if kind == "stop":
            t, srv = rest
            self.do_stop(t, srv) if yes else self.say("Cancelled; nothing was stopped.")
        elif kind == "run":
            plan, api_key, verb = rest
            self.do_execute(plan, api_key, verb) if yes else self.say("Cancelled; nothing was changed.")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        event.input.value = ""
        if self.pending:
            self.answer(raw)
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
                self.say(f"  [b]{escape(name):<19}[/b] {text}")
        elif cmd == "/refresh":
            self.action_refresh_now()
        elif cmd in ("/stop", "/start", "/restart", "/switch") and self.busy:
            self.say("Another stop/start is in progress; wait for it to finish.")
        elif cmd == "/stop":
            if len(parts) != 2:
                self.say("Usage: /stop SLOT   (a slot name like SMALL, or a port like 8002)")
            else:
                self.prepare_stop(parts[1])
        elif cmd in ("/start", "/switch"):
            if len(parts) != 3:
                self.say(f"Usage: {cmd} SLOT MODEL   (Tab completes slot and model names)")
            else:
                self.prepare_plan(cmd[1:], parts[1], parts[2])
        elif cmd == "/restart":
            if len(parts) != 2:
                self.say("Usage: /restart SLOT   (same model, fresh process)")
            else:
                self.prepare_plan("restart", parts[1])
        elif cmd == "/quit":
            if self.busy:
                self.say("[yellow]A stop/start is in progress; it carries on without mlxsw.[/]")
            self.exit()
        else:
            self.say(f"[red]Unknown command {escape(word)}.[/] Try /help.")


def run(core, version, interval=5.0):
    MlxswApp(core, version, interval).run()