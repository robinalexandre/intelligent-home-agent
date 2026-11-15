"""Tools to hand work to a coding subagent and talk to it (intelligent_home_agent.jobs)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

if TYPE_CHECKING:
    from intelligent_home_agent.jobs import JobRunner

_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_BRANCH = re.compile(r"^[\w./-]+$")
MAX_REQUEST_CHARS = 6000
# Arguments that keep their line breaks (a request can be a list).
MULTILINE = frozenset({"request", "message"})


def build_job_tools(runner: JobRunner | None, language: str = "en") -> list[Tool]:
    if runner is None:
        return []
    # Imported here: jobs.py itself imports this package (ToolError).
    from intelligent_home_agent.jobs import MODELS, NORMAL

    s = strings(language)

    def text_arg(args: dict[str, Any], key: str, limit: int) -> str:
        raw = str(args.get(key) or "")
        value = raw.strip() if key in MULTILINE else " ".join(raw.split())
        if not value:
            raise ToolError(f"{key} is required.")
        if len(value) > limit:
            raise ToolError(f"{key} is too long ({limit} characters at most).")
        return value

    def repo_arg(args: dict[str, Any]) -> str:
        repo = str(args.get("repo") or "").strip()
        if repo and not _REPO.match(repo):
            raise ToolError('repo must look like "owner/name".')
        return repo

    def branch_arg(args: dict[str, Any]) -> str:
        branch = str(args.get("branch") or "").strip()
        if branch and not _BRANCH.match(branch):
            raise ToolError("branch holds characters a git branch can't.")
        return branch

    async def start_job(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        title = text_arg(args, "title", 120)
        request = text_arg(args, "request", MAX_REQUEST_CHARS)
        complexity = str(args.get("complexity") or NORMAL).strip().lower()
        job = await runner.start(
            title,
            request,
            repo_arg(args),
            branch_arg(args),
            complexity,
            announce=ctx.user_waits,
        )
        result = {
            "job": job.id,
            "title": job.title,
            "model": job.model,
            "console": runner.console_url(job),
            "note": (
                "Started. It works on its own; the user gets news on the way and its report "
                "when it stops, with the pull request's address. Tell them the job number "
                "and how long it should take."
            ),
        }
        if minutes := runner.estimate_minutes(job):
            result["estimate_minutes"] = minutes
        return result

    async def ask_job(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        job = await runner.ask(
            text_arg(args, "job", 20), text_arg(args, "message", 4000), announce=ctx.user_waits
        )
        return {
            "job": job.id,
            "status": job.status,
            "note": "Sent; its answer reaches the user by itself.",
        }

    async def list_jobs(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return {"jobs": [job.summary() for job in runner.list()]}

    async def stop_job(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        job = await runner.stop(text_arg(args, "job", 20))
        return {"job": job.id, "status": job.status}

    def one_line(value: Any) -> str:
        return str(value or "").replace("\n", " ⏎ ")

    async def describe_start(args: dict[str, Any]) -> str:
        where = s.job_on_repo.format(repo=args["repo"]) if args.get("repo") else ""
        return s.job_start_summary.format(
            title=args.get("title", ""), where=where, request=one_line(args.get("request"))
        )

    async def describe_ask(args: dict[str, Any]) -> str:
        return s.job_ask_summary.format(
            job=args.get("job", ""), message=one_line(args.get("message"))
        )

    async def describe_stop(args: dict[str, Any]) -> str:
        return s.job_stop_summary.format(job=args.get("job", ""))

    return [
        Tool(
            name="start_job",
            description=(
                "Hand a piece of work to a coding subagent that works on its own, in the "
                "background, on one of the user's GitHub repositories: a fix, a feature, a "
                "refactor, a pull request. It clones the repository, codes, runs the tests "
                "and opens the pull request; the user gets its report by itself when it "
                "stops. Returns at once with the job's number."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "A few words, user's language."},
                    "request": {
                        "type": "string",
                        "description": (
                            "Everything the coder must know, as the user said it: what to "
                            "change, where, why, constraints, how to check it."
                        ),
                    },
                    "repo": {
                        "type": "string",
                        "description": '"owner/name" of the repository to work on.',
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch to start from (default: the default branch).",
                    },
                    "complexity": {
                        "type": "string",
                        "enum": sorted(MODELS),
                        "description": (
                            "Picks the coder's model; your call, from the request alone. "
                            '"simple": a change anyone could make in minutes (a line, a '
                            'label, a typo, a version bump). "normal": almost everything '
                            'else (default). "complex": only when it is really hard '
                            "(a design across modules, a subtle bug, a migration)."
                        ),
                    },
                },
                "required": ["title", "request", "repo"],
            },
            handler=start_job,
            proposable=True,
            needs_clean_turn=True,
            needs_user_request=True,
            describe=describe_start,
            multiline_args=MULTILINE,
        ),
        Tool(
            name="ask_job",
            description=(
                "Pass the user's answer or follow-up to a running or waiting job: a decision "
                "it asked for, a change of plan, a question about where it stands. Its reply "
                "reaches the user by itself."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job": {"type": "string", "description": "The job's number."},
                    "message": {"type": "string", "description": "What to tell it."},
                },
                "required": ["job", "message"],
            },
            handler=ask_job,
            proposable=True,
            needs_clean_turn=True,
            needs_user_request=True,
            describe=describe_ask,
            multiline_args=MULTILINE,
        ),
        Tool(
            name="list_jobs",
            description=(
                "The jobs handed to the coding subagent, newest first: status (running, idle "
                "and waiting for the user, done, failed, stopped), pull request, last report."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=list_jobs,
            taints=True,
        ),
        Tool(
            name="stop_job",
            description="Stop a job: interrupts it if it is working, and closes it.",
            input_schema={
                "type": "object",
                "properties": {"job": {"type": "string", "description": "The job's number."}},
                "required": ["job"],
            },
            handler=stop_job,
            proposable=True,
            needs_clean_turn=True,
            describe=describe_stop,
        ),
    ]
