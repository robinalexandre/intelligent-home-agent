"""Tool abstraction shared by every capability the agent exposes to the model.

A :class:`Tool` carries flags that say what its output is (others' words, a web page) and
what it does (acts, destroys); :class:`ToolRegistry.run` enforces them for every call,
with the request's state kept in :class:`ToolContext`. A write the rules would refuse is
parked instead, shown whole to the user by the code, and runs on their go
(tools/proposals.py).
"""

from __future__ import annotations

import inspect
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.leaks import Leaks

log = logging.getLogger(__name__)

MAX_FREE_SITES = 6
# How an engine that reaches the tools through an MCP bridge names them; a model on it may
# pass that name where a tool's own name is expected.
MCP_PREFIX = re.compile(r"^mcp__[^_]+__")
# A site's name as the user writes it, with or without a scheme and a path (react.dev,
# nextjs.org/blog); not the domain of an email address.
_HOST = re.compile(
    r"(?<![\w@.-])(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?![\w-])",
    re.IGNORECASE,
)
# The readable path the user wrote after a site's name (nextjs.org/blog).
_NAMED_PATH = re.compile(r"/[A-Za-z0-9._~%/-]*")


class ToolError(Exception):
    """An error message meant to be shown to the model (not a crash)."""


class ParkInstead(ToolError):
    """A call the rules refuse in this request (a site, a search after a read): instead of
    a refusal, the registry parks it, exactly as called, for the user's go. Run
    on their go, it starts from a clean request and the agent picks up from its result."""


@dataclass
class WithImage:
    """A result the model reads with a picture of it: the screen after a click, so the
    agent checks it did what it meant before the next step."""

    data: dict[str, Any]
    image: bytes
    mimetype: str = "image/jpeg"


class ToolContext:
    def __init__(
        self,
        conversation: Conversation,
        *,
        tainted: bool = False,
        scheduled: bool = False,
        quoted: bool | str = False,
        approved_plan: str = "",
        repeating: bool = False,
        answerable: bool = True,
    ) -> None:
        self.conversation = conversation
        # The user can answer this request's reply. When they can't (a briefing a scheduler
        # asked for, in a conversation of its own), nothing is parked for a go that could
        # never come: what would wait is refused instead.
        self.answerable = answerable
        # This request is a plan the agent proposed and the user approved as shown: the
        # writes whose arguments it names run without another go, while it reads only
        # web pages.
        self.approved_plan = approved_plan
        # Things the client must do on the user's phone after replying (e.g. create a
        # reminder).
        self.actions: list[dict[str, Any]] = []
        # Set once this request has read private, third-party-written content (emails,
        # events, pull requests). Pages a browsing tool showed count too, but they are
        # kept apart in page_hosts: acting on that same site stays allowed.
        self.private_read = tainted
        # Hosts of the web pages the tools read in this request.
        self.page_hosts: set[str] = set()
        # A web search was answered in this request: others' words, which can steer, but
        # nothing private that a URL could carry out.
        self.web_read = False
        # Web searches this request ran: a few may follow public pages (tools/web.py).
        self.searches = 0
        # Addresses the content read in this request put forward (a page's links and the
        # sites it names, a search answer's sources). Written by others before anything of
        # this request reached them, so opening one can't carry its data out.
        self.offered_urls: set[str] = set()
        self._hosts_decided: frozenset[str] | None = None
        # A page read in this request looked hostile (hidden text or comments speaking to
        # an assistant): no free navigation and no acting on sites until the user writes.
        self.hostile_read = False
        # A browsing tool showed a page of a site the user is logged into: their own
        # data, not public web, so no free navigation to new sites afterwards.
        self.account_read = False
        # The logged-in sites those pages were on, and the site each file downloaded in
        # this request came from, by kept name.
        self.account_hosts: set[str] = set()
        self.downloads: dict[str, str] = {}
        # Sites the agent went to on its own, with a plain address nothing read had named:
        # a few per request, as each such address could spell out a little of its content.
        self.free_hosts: set[str] = set()
        # Sites the user named in their own words (the sources to watch): their choice, not
        # the agent's, so they don't count among those few (name_hosts).
        self.named_hosts: set[str] = set()
        # The exact addresses the user wrote (site and path): every word of them is theirs,
        # so opening one carries nothing out, even after a page that looked hostile.
        self.named_addresses: set[str] = set()
        # What the request holds (memory, earlier replies, what its tools read): text the
        # model writes to the outside is checked against it (keep_in).
        self.leaks = Leaks()
        # A task the user left earlier, started by a timer: nobody wrote this request just
        # now.
        self.scheduled = scheduled
        # A repeating task's run, which carries its last report and so starts tainted.
        self.repeating = repeating
        # Tools such a run was refused that its next runs get by running clean.
        self.wants_clean_runs: set[str] = set()
        # The user's message quotes an earlier one, which may repeat what an email said:
        # "own" when it quotes one of the agent's own replies, "other" for any other message.
        self.quoted = bool(quoted)
        self.quotes_own = quoted == "own"
        # Event places read from the calendar in this request: the only destinations travel
        # time accepts once the request is tainted.
        self.calendar_places: set[str] = set()
        # The call running now is one the user approved as shown (a poll, or their "yes").
        self.approving = False
        # The country a browsing tool's pages of this request are seen from, "" at home.
        self.abroad = ""
        # What the request's tools left open, closed when it ends (finish).
        self.cleanups: list[Callable[[], Awaitable[None]]] = []

    async def finish(self) -> None:
        """Close what the request's tools left open (a connection, a browser tab)."""
        cleanups, self.cleanups = self.cleanups, []
        for cleanup in cleanups:
            try:
                await cleanup()
            except Exception:
                log.exception("A request's cleanup failed")

    def keep_in(self, text: str, kind: str) -> None:
        """Refuse text the model wrote for the outside when it repeats what the request
        holds; an address a page offered is the page's own words and is not checked."""
        if kind == "address" and self.offered(text):
            return
        found = self.leaks.found_in_address if kind == "address" else self.leaks.found
        if what := found(text):
            instead = (
                "Search again without it, in the user's own words."
                if kind == "query"
                else "Use an address a page listed, or ask the user."
            )
            raise ToolError(
                f"This {kind} repeats {what} from what this request holds (memory, earlier "
                f"replies, what was read): it can't go out. {instead}"
            )

    def free_site(self, host: str) -> bool:
        """Whether the agent may go to host on its own: one it already went to, or one
        more while the request has fewer than MAX_FREE_SITES."""
        if host in self.free_hosts or _bare(host) in self.named_hosts:
            return True
        if len(self.free_hosts) >= MAX_FREE_SITES:
            return False
        self.free_hosts.add(host)
        return True

    @property
    def user_waits(self) -> bool:
        """The user's own request, or a plan they approved from one: they wait for what it
        starts in the background, unlike a task they left earlier."""
        return not self.scheduled or bool(self.approved_plan)

    @property
    def tainted(self) -> bool:
        """Whether this request read anything written by others, pages included."""
        return self.private_read or bool(self.page_hosts) or self.web_read

    @tainted.setter
    def tainted(self, value: bool) -> None:
        self.private_read = bool(value)

    def read_page(self, host: str) -> None:
        self.page_hosts.add(host.lower().rstrip("."))

    def begin_batch(self) -> None:
        """The agent runs the calls of one round together: the model decided them all
        before any ran, so a page one of them reads doesn't bear on its siblings."""
        self._hosts_decided = frozenset(self.page_hosts)

    @property
    def hosts_decided(self) -> frozenset[str]:
        """The page hosts read when the running calls were decided (live outside a batch)."""
        if self._hosts_decided is None:
            return frozenset(self.page_hosts)
        return self._hosts_decided

    def name_hosts(self, own_words: str) -> None:
        """The sites the user's own words name: only text they wrote, never what a quote,
        a last report or a page said."""
        for match in _HOST.finditer(own_words):
            host = _bare(match.group(1))
            self.named_hosts.add(host)
            path = _NAMED_PATH.match(own_words, match.end())
            self.named_addresses.add(_address(host, path.group(0).rstrip(".") if path else ""))

    def named(self, url: str) -> bool:
        """Whether url is exactly an address the user's own words wrote, nothing added."""
        parts = urlsplit(url.strip())
        if parts.query or parts.username or parts.password or parts.port:
            return False
        return _address(parts.hostname or "", parts.path) in self.named_addresses

    def offer_urls(self, urls: Iterable[str]) -> None:
        self.offered_urls.update(canonical_url(u) for u in urls if u[:4].lower() == "http")

    def offered(self, url: str) -> bool:
        return canonical_url(url) in self.offered_urls


def bare_name(name: str) -> str:
    return MCP_PREFIX.sub("", name)


def _bare(host: str) -> str:
    return host.lower().rstrip(".").removeprefix("www.")


def _address(host: str, path: str) -> str:
    return _bare(host) + path.rstrip("/")


def canonical_url(url: str) -> str:
    """One spelling per address, so a link matches however the model copies it."""
    parts = urlsplit(url.strip())
    return parts._replace(
        scheme=parts.scheme.lower(),
        netloc=parts.netloc.lower().rstrip("."),
        path=parts.path or "/",
        fragment="",
    ).geturl()


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[Any]]
# A write's exact effect in one line, built by the code from its arguments, for the user.
Describer = Callable[[dict[str, Any]], Awaitable[str]]
MAX_SHOWN_ARG_CHARS = 1500
# A text the tool writes on several lines (a handoff's next steps) may run longer.
MAX_SHOWN_MULTILINE_CHARS = 4000


def _leaves(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (str(k), *_leaves(v))]
    if isinstance(value, list):
        return [s for item in value for s in _leaves(item)]
    return [str(value)]


def _values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [v for item in value.values() for v in _values(item)]
    if isinstance(value, list):
        return [v for item in value for v in _values(item)]
    return [value]


def named_in(plan: str, args: dict[str, Any]) -> bool:
    """Whether the user saw every argument of a write in the plan they approved: paths,
    names and ids must be in its text, so a page read on the way can't change them."""
    # One accent encoding for both: a path copied from a drive is often decomposed (NFD)
    # while the plan was written composed (NFC).
    text = unicodedata.normalize("NFC", " ".join(plan.split())).casefold()
    for value in _values(args):
        if isinstance(value, bool) or value is None:
            continue
        leaf = unicodedata.normalize("NFC", " ".join(str(value).split())).casefold()
        if len(leaf) > 3 and leaf not in text:
            return False
    return True


def describe_whole(label: str) -> Describer:
    """A write shown argument by argument, line breaks as ⏎: what the user approves is
    every word that will be written."""

    async def describe(args: dict[str, Any]) -> str:
        shown = " · ".join(
            f"{key.replace('_', ' ')}: {str(value).strip().replace(chr(10), ' ⏎ ')}"
            for key, value in args.items()
            if value not in (None, "")
        )
        return f"{label} — {shown}"

    return describe


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    # Output is private content written by others (emails, invites): it can carry
    # injected instructions, and must not leak out.
    taints: bool = False
    # Output is a web page a browsing tool shows: others' content too, but the handler
    # records it per site (ToolContext.read_page) so the request may still act on that site.
    reads_pages: bool = False
    # Output is written from public web pages (a search answer): others' words, so writes
    # are refused as after any read, but nothing private, so public pages still open.
    reads_web: bool = False
    # Acts on the home or sends data out: refused once the request is tainted.
    needs_clean_turn: bool = False
    # Destroys something: also refused in a scheduled task, as only a message the user
    # just wrote can ask for it.
    needs_user_request: bool = False
    # Once the request is tainted, parked for the user's go instead of refused: the code
    # shows them exactly what it will do, and their next message can run it.
    proposable: bool = False
    # Parked even in a clean request: the user always sees it before it runs.
    always_propose: bool = False
    # Only runs what the user was already shown (run_proposals): a quote can't change
    # what that does, so a "yes" in reply to the list works.
    runs_shown_writes: bool = False
    describe: Describer | None = None
    # Refuses at once, even before the write is parked, what could never run: the user
    # is not asked about it.
    vet: Callable[[dict[str, Any], ToolContext], None | Awaitable[None]] | None = None
    # Arguments that may hold line breaks: describe shows them as ⏎.
    multiline_args: frozenset[str] = frozenset()
    # Refused rather than parked once the request read others' content: run_proposals
    # itself. Every other write that needs a clean request waits for the user's go
    # instead, shown whole under the reply, so a read never forces them to write again.
    never_park: bool = False
    # Refused in a repeating task's run as it carries its last report: the task then runs
    # clean from its next run on, so it gets it then.
    clean_runs_in_tasks: bool = False

    @property
    def parks(self) -> bool:
        """Parked for the user's go where a clean request would be needed."""
        return self.proposable or (self.needs_clean_turn and not self.never_park)

    async def summary(self, args: dict[str, Any]) -> str:
        """Every argument whole: what the user approves is exactly what runs."""

        def shown(text: str, multiline: bool) -> bool:
            # Line breaks only where the tool shows them, as ⏎ on one line (a compose
            # file); anything else invisible is refused.
            limit = MAX_SHOWN_ARG_CHARS
            if multiline:
                text = text.replace("\n", " ").replace("\t", " ")
                limit = MAX_SHOWN_MULTILINE_CHARS
            return len(text) <= limit and text.isprintable()

        if not all(
            shown(text, key in self.multiline_args and self.describe is not None)
            for key, value in args.items()
            for text in _leaves(value)
        ):
            raise ToolError("An argument is too long or holds hidden characters to be shown.")
        if self.describe:
            # It may add others' text (a payment's label) to describe the action: one
            # visible line, whatever that text holds.
            line = " ".join((await self.describe(args)).split())
            return "".join(c if c.isprintable() else "�" for c in line)
        shown = ", ".join(f"{key.replace('_', ' ')}: {value}" for key, value in args.items())
        return f"{self.name.replace('_', ' ').capitalize()} — {shown}"

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _refused(name: str, ctx: ToolContext) -> str:
    if name in ctx.wants_clean_runs:
        return (
            f"{name} never runs in this run: a repeating task carries its last report, so it "
            "starts as if it had read others' content. From its next run on, this task runs "
            f"clean, with the time of its last run instead of its last report, and {name} "
            "runs then. Do the rest of the task now, without mentioning it."
        )
    if ctx.repeating and ctx.private_read:
        return (
            f"{name} never runs in a repeating task: it carries its last report, so it starts "
            "as if it had read others' content. Do the rest of the task. Unless your last "
            f"report already said it, end with one line: {name} was skipped, and adding "
            "`fresh: yes` to this task's file lets it run (a fresh task starts clean, gets "
            "the time of its last run instead of its last report, and gets its own message "
            "instead of being folded into the briefing)."
        )
    if ctx.tainted:
        return (
            f"{name} is disabled in a request that read emails, the calendar, GitHub, a "
            "connected account or the web. Tell the user to ask for it again in a separate "
            "request."
        )
    return (
        f"{name} is disabled in a reply quoting a message that may repeat what an email or a "
        "page said. Tell the user to ask for it again in a new message, not as a reply."
    )


async def vet(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> None:
    """What the tool refuses at once, before a call runs or is parked."""
    if tool.vet is not None and inspect.isawaitable(checked := tool.vet(args, ctx)):
        await checked


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def extend(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.to_api() for tool in self._tools.values()]

    def names(self) -> set[str]:
        return set(self._tools)

    def taints(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.taints

    def get(self, name: str) -> Tool | None:
        return self._tools.get(bare_name(name))

    def reads_web(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.reads_web

    async def run(
        self, name: str, args: dict[str, Any], ctx: ToolContext, *, approved: bool = False
    ) -> Any:
        """``approved``: run_proposals running a write the user agreed to."""
        name = bare_name(name)
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        unclean = ctx.tainted or (ctx.quoted and not tool.runs_shown_writes)
        # A write the approved plan spelled out: the pages read on the way don't park it.
        if (
            tool.parks
            and ctx.approved_plan
            and not (ctx.private_read or ctx.quoted)
            and named_in(ctx.approved_plan, args)
        ):
            unclean = False
        await vet(tool, args, ctx)
        # In a scheduled task, what only the user may ask for waits for them instead.
        unasked = tool.needs_user_request and ctx.scheduled
        if not approved and (
            tool.always_propose or (tool.parks and (unasked or (tool.needs_clean_turn and unclean)))
        ):
            if ctx.repeating and ctx.private_read and not tool.proposable:
                # A repeating task would ask the same thing at every run.
                raise ToolError(_refused(name, ctx))
            return await self._park(tool, args, ctx)
        if tool.needs_clean_turn and unclean and not approved:
            raise ToolError(_refused(name, ctx))
        if tool.needs_user_request and ctx.scheduled:
            raise ToolError(
                f"{name} is disabled in a scheduled task: only the user's own message can ask "
                "for it. Tell the user exactly what you would do and ask; act on their reply."
            )
        ctx.approving = approved
        try:
            result = await tool.handler(args, ctx)
        except ParkInstead:
            if not approved and ctx.repeating and ctx.private_read and tool.clean_runs_in_tasks:
                ctx.wants_clean_runs.add(name)
                raise ToolError(_refused(name, ctx)) from None
            if approved or ctx.repeating:
                raise
            return await self._park(tool, args, ctx)
        finally:
            ctx.approving = False
        if approved:
            # Run on the user's go inside another tool (run_proposals): what it read still
            # counts for the rest of the request, as if the agent had called it.
            if tool.taints:
                ctx.tainted = True
            if tool.reads_web:
                ctx.web_read = True
        return result

    async def propose(self, name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Park a write for the user's go even in a clean request, to ask before acting.

        A write that is refused rather than parked once the request read others' content
        can still wait in a clean request: it could run right away, so asking first is
        only stricter.
        """
        name = bare_name(name)
        tool = self._tools.get(name)
        if tool is not None and not tool.parks and tool.needs_clean_turn:
            if ctx.tainted or (ctx.quoted and not tool.runs_shown_writes):
                raise ToolError(f"{name} can't wait for the user's go. {_refused(name, ctx)}")
        if tool is None or not (tool.parks or tool.needs_clean_turn):
            raise ToolError(f"{name} can't wait for the user's go: call it, or ask the user.")
        await vet(tool, args, ctx)
        return await self._park(tool, args, ctx)

    async def _park(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if not ctx.answerable:
            raise ToolError(
                f"{tool.name} would wait for the user's go, and nobody can answer this "
                "reply: leave it out, and do the rest."
            )
        return await park(tool, args, ctx)


async def park(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Park a call for the user's go: shown whole, numbered, under the reply. A tool may
    park another one itself (a change it could not check, waiting for the user instead)."""
    summary = await tool.summary(args)
    proposal, new = ctx.conversation.propose(tool.name, args, summary)
    if new:
        # Listed under the reply by the code, so nothing parked goes unseen.
        ctx.actions.append({"type": "proposal", "number": proposal.number, "summary": summary})
    return {
        "status": "waiting_for_the_user" if new else "already_waiting",
        "number": proposal.number,
        "note": "Not done yet: the user sees it, numbered, under your reply. If their "
        "next message agrees, run_proposals does it.",
    }
