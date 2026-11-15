"""Build the Siri Shortcuts (assistant, briefing, recap, quick questions) as signed files.

macOS only (signing uses the `shortcuts` command):

    python scripts/build_shortcuts.py --url https://agent.example.com --token "$TOKEN" \
        --name "$ASSISTANT_NAME" --language en \
        --quick "Surf check=Is there surf today or tomorrow?"

Then open the files on the Mac (they sync to the iPhone through iCloud) or AirDrop them.
The token is written into the files: don't share them.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

# Shortcuts marks a variable's place in a text with this character.
_SLOT = "￼"
REPEAT_ITEM = {"Type": "Variable", "VariableName": "Repeat Item"}
SHORTCUT_INPUT = {"Type": "ExtensionInput"}

# What the Shortcuts say and send, per language (the agent's LANGUAGE setting).
STRINGS = {
    "en": {
        "prompt": "Yes?",
        "briefing": ("Briefing", "Give me the morning briefing"),
        "recap": ("Recap", "Give me the evening recap"),
        "reminders": "Open reminders (title · due):",
        "departure": "Leaving home",
    },
    "fr": {
        "prompt": "Oui ?",
        "briefing": ("Briefing", "Fais-moi le briefing du matin"),
        "recap": ("Récap", "Fais-moi le récap du soir"),
        "reminders": "Rappels non terminés (titre · échéance) :",
        "departure": "Départ",
    },
}


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def output(action_id: str, name: str) -> dict[str, Any]:
    return {"OutputUUID": action_id, "Type": "ActionOutput", "OutputName": name}


def _with_property(ref: dict[str, Any], prop: str | None) -> dict[str, Any]:
    if prop is None:
        return dict(ref)
    aggrandizement = {"Type": "WFPropertyVariableAggrandizement", "PropertyName": prop}
    return {**ref, "Aggrandizements": [aggrandizement]}


def variable(ref: dict[str, Any], prop: str | None = None) -> dict[str, Any]:
    return {"Value": _with_property(ref, prop), "WFSerializationType": "WFTextTokenAttachment"}


def text(*parts: str | dict[str, Any] | tuple[dict[str, Any], str]) -> dict[str, Any]:
    string, slots = "", {}
    for part in parts:
        if isinstance(part, str):
            string += part
            continue
        ref, prop = part if isinstance(part, tuple) else (part, None)
        # Ranges count UTF-16 code units, like NSString.
        slots[f"{{{len(string.encode('utf-16-le')) // 2}, 1}}"] = _with_property(ref, prop)
        string += _SLOT
    value: dict[str, Any] = {"string": string}
    if slots:
        value["attachmentsByRange"] = slots
    return {"Value": value, "WFSerializationType": "WFTextTokenString"}


def fields(items: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {"WFItemType": 0, "WFKey": text(key), "WFValue": value} for key, value in items
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def action(identifier: str, **params: Any) -> dict[str, Any]:
    return {
        "WFWorkflowActionIdentifier": f"is.workflow.actions.{identifier}",
        "WFWorkflowActionParameters": params,
    }


def get_value(key: str, source: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action_id = new_id()
    return action_id, action(
        "getvalueforkey", UUID=action_id, WFDictionaryKey=key, WFInput=variable(source)
    )


def ask_agent(url: str, token: str, body: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict]:
    action_id = new_id()
    return action_id, action(
        "downloadurl",
        UUID=action_id,
        WFURL=f"{url.rstrip('/')}/v1/ask",
        WFHTTPMethod="POST",
        ShowHeaders=True,
        WFHTTPHeaders=fields([("Authorization", text(f"Bearer {token}"))]),
        WFHTTPBodyType="JSON",
        WFJSONValues=fields([*body, ("conversation_id", text("siri"))]),
    )


def phone_actions(response_id: str) -> list[dict[str, Any]]:
    """Create the reminders the agent asked for in its `actions`."""
    actions_id, get_actions = get_value("actions", output(response_id, "Contents of URL"))
    loop, condition = new_id(), new_id()
    title_id, get_title = get_value("title", REPEAT_ITEM)
    due_id, get_due = get_value("due", REPEAT_ITEM)
    dates_id = new_id()
    due = output(due_id, "Dictionary Value")
    title = text(output(title_id, "Dictionary Value"))
    return [
        get_actions,
        action(
            "repeat.each",
            GroupingIdentifier=loop,
            WFControlFlowMode=0,
            WFInput=variable(output(actions_id, "Dictionary Value")),
        ),
        get_title,
        get_due,
        action(
            "conditional",
            GroupingIdentifier=condition,
            WFControlFlowMode=0,
            WFCondition=100,
            WFInput={"Type": "Variable", "Variable": variable(due)},
        ),
        action("detect.date", UUID=dates_id, WFInput=variable(due)),
        action(
            "addnewreminder",
            WFCalendarItemTitle=title,
            WFAlertEnabled="Alert",
            WFAlertCustomTime=text(output(dates_id, "Dates")),
        ),
        action("conditional", GroupingIdentifier=condition, WFControlFlowMode=1),
        action("addnewreminder", WFCalendarItemTitle=title),
        action("conditional", GroupingIdentifier=condition, WFControlFlowMode=2),
        action("repeat.each", GroupingIdentifier=loop, WFControlFlowMode=2),
    ]


def if_has_value(group: str, ref: dict[str, Any]) -> dict[str, Any]:
    return action(
        "conditional",
        GroupingIdentifier=group,
        WFControlFlowMode=0,
        WFCondition=100,
        WFInput={"Type": "Variable", "Variable": variable(ref)},
    )


def reply_or_listen(response_id: str, reply_id: str, name: str, *, from_main: bool) -> list[dict]:
    """Show the reply; to keep the conversation going, the main Shortcut speaks it and
    listens again."""
    follow_id, get_follow = get_value("follow_up", output(response_id, "Contents of URL"))
    group = new_id()
    reply = output(reply_id, "Dictionary Value")
    return [
        get_follow,
        if_has_value(group, output(follow_id, "Dictionary Value")),
        action(
            "runworkflow",
            WFWorkflowName=name,
            WFWorkflow={"workflowName": name, "isSelf": from_main},
            WFInput=variable(reply),
            WFShowWorkflow=False,
        ),
        action("conditional", GroupingIdentifier=group, WFControlFlowMode=1),
        action("showresult", Text=text(reply)),
        action("conditional", GroupingIdentifier=group, WFControlFlowMode=2),
    ]


def main_shortcut(url: str, token: str, name: str, prompt: str) -> list[dict[str, Any]]:
    """Ask "Yes?", or the agent's question when the Shortcut runs itself for a follow-up."""
    group, heard_id, fresh_id, prompt_id = new_id(), new_id(), new_id(), new_id()
    response_id, request = ask_agent(url, token, [("text", text(output(prompt_id, "If Result")))])
    reply_id, get_reply = get_value("reply", output(response_id, "Contents of URL"))
    return [
        if_has_value(group, SHORTCUT_INPUT),
        action("ask", UUID=heard_id, WFAskActionPrompt=text(SHORTCUT_INPUT), WFInputType="Text"),
        action("conditional", GroupingIdentifier=group, WFControlFlowMode=1),
        action("ask", UUID=fresh_id, WFAskActionPrompt=prompt, WFInputType="Text"),
        action("conditional", UUID=prompt_id, GroupingIdentifier=group, WFControlFlowMode=2),
        request,
        get_reply,
        *phone_actions(response_id),
        *reply_or_listen(response_id, reply_id, name, from_main=True),
    ]


def quick_question(url: str, token: str, name: str, question: str) -> list[dict[str, Any]]:
    """A fixed question in one Siri phrase, e.g. "Hey Siri, surf check"."""
    response_id, request = ask_agent(url, token, [("text", text(question))])
    reply_id, get_reply = get_value("reply", output(response_id, "Contents of URL"))
    return [request, get_reply, *reply_or_listen(response_id, reply_id, name, from_main=False)]


def with_reminders(
    url: str, token: str, request_text: str, label: str, title: str
) -> list[dict[str, Any]]:
    """Send open reminders as context and post the written reply as a notification."""
    found_id, loop, loop_end_id, line_id, joined_id, context_id = (new_id() for _ in range(6))
    response_id, request = ask_agent(
        url,
        token,
        [
            ("text", text(request_text)),
            ("context", text(output(context_id, "Text"))),
            ("display", text("notification")),
        ],
    )
    reply_id, get_reply = get_value("reply", output(response_id, "Contents of URL"))
    not_completed = {
        "Operator": 4,
        "Property": "Is Completed",
        "Removable": True,
        "Values": {"Bool": False},
    }
    return [
        action(
            "filter.reminders",
            UUID=found_id,
            WFContentItemFilter={
                "Value": {
                    "WFActionParameterFilterPrefix": 1,
                    "WFContentPredicateBoundedDate": False,
                    "WFActionParameterFilterTemplates": [not_completed],
                },
                "WFSerializationType": "WFContentPredicateTableTemplate",
            },
            WFContentItemSortProperty="Due Date",
            WFContentItemSortOrder="Oldest First",
            WFContentItemLimitEnabled=True,
            WFContentItemLimitNumber=30,
        ),
        action(
            "repeat.each",
            GroupingIdentifier=loop,
            WFControlFlowMode=0,
            WFInput=variable(output(found_id, "Reminders")),
        ),
        action(
            "gettext",
            UUID=line_id,
            WFTextActionText=text(REPEAT_ITEM, " · ", (REPEAT_ITEM, "Due Date")),
        ),
        action("repeat.each", UUID=loop_end_id, GroupingIdentifier=loop, WFControlFlowMode=2),
        action(
            "text.combine",
            UUID=joined_id,
            text=variable(output(loop_end_id, "Repeat Results")),
            WFTextSeparator="New Lines",
        ),
        action(
            "gettext",
            UUID=context_id,
            WFTextActionText=text(f"{label}\n", output(joined_id, "Combined Text")),
        ),
        request,
        get_reply,
        *phone_actions(response_id),
        action(
            "notification",
            WFNotificationActionTitle=text(title),
            WFNotificationActionBody=text(output(reply_id, "Dictionary Value")),
            WFNotificationActionSound=True,
        ),
    ]


def leave_home(url: str, token: str, title: str) -> list[dict[str, Any]]:
    """Tell the agent the user left (the phone's "When I leave" automation runs this), and
    show what it found, if anything, as a notification. A yes to the main Shortcut then
    runs the fixes it lists: both use the same conversation."""
    response_id, group = new_id(), new_id()
    reply_id, get_reply = get_value("reply", output(response_id, "Contents of URL"))
    reply = output(reply_id, "Dictionary Value")
    return [
        action(
            "downloadurl",
            UUID=response_id,
            WFURL=f"{url.rstrip('/')}/v1/departure",
            WFHTTPMethod="POST",
            ShowHeaders=True,
            WFHTTPHeaders=fields([("Authorization", text(f"Bearer {token}"))]),
            WFHTTPBodyType="JSON",
            WFJSONValues=fields([("conversation_id", text("siri"))]),
        ),
        get_reply,
        if_has_value(group, reply),
        action(
            "notification",
            WFNotificationActionTitle=text(title),
            WFNotificationActionBody=text(reply),
            WFNotificationActionSound=True,
        ),
        action("conditional", GroupingIdentifier=group, WFControlFlowMode=2),
    ]


def workflow(
    actions: list[dict[str, Any]], glyph: int, color: int, takes_text: bool = False
) -> dict[str, Any]:
    return {
        "WFWorkflowHasShortcutInputVariables": takes_text,
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "2605.0.5",
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": glyph,
            "WFWorkflowIconStartColor": color,
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"] if takes_text else [],
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="Agent base URL")
    parser.add_argument("--token", required=True, help="AGENT_API_TOKEN")
    parser.add_argument(
        "--name",
        default=os.environ.get("ASSISTANT_NAME", "Assistant"),
        help="Name of the main Shortcut, which is also the Siri phrase",
    )
    parser.add_argument("--language", choices=sorted(STRINGS), default="en")
    parser.add_argument(
        "--quick",
        action="append",
        default=[],
        metavar="NAME=QUESTION",
        help='Extra one-phrase Shortcut, e.g. "Surf check=Is there surf today?" (repeatable)',
    )
    parser.add_argument(
        "--departure",
        action="store_true",
        help="Also build the Shortcut for the leaving-home automation (docs/home-devices.md)",
    )
    parser.add_argument("--out", type=Path, default=Path("shortcuts"))
    args = parser.parse_args()

    s = STRINGS[args.language]
    url, token, name = args.url, args.token, args.name
    shortcuts = {
        name: workflow(main_shortcut(url, token, name, s["prompt"]), 59446, 4282601983, True),
    }
    for quick in args.quick:
        title, sep, question = quick.partition("=")
        if not sep or not title.strip() or not question.strip():
            parser.error(f"--quick needs NAME=QUESTION, got {quick!r}")
        shortcuts[title.strip()] = workflow(
            quick_question(url, token, name, question.strip()), 59511, 4282601983
        )
    for key, glyph, color in (("briefing", 59782, 4292093695), ("recap", 59654, 4271458815)):
        title, request = s[key]
        shortcuts[title] = workflow(
            with_reminders(url, token, request, s["reminders"], title), glyph, color
        )
    if args.departure:
        title = s["departure"]
        shortcuts[title] = workflow(leave_home(url, token, title), 59809, 4282601983)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in shortcuts.items():
            unsigned = Path(tmp) / f"{name}.shortcut"
            unsigned.write_bytes(plistlib.dumps(content, fmt=plistlib.FMT_BINARY))
            signed = args.out / f"{name}.shortcut"
            subprocess.run(  # noqa: S603
                ["/usr/bin/shortcuts", "sign", "--input", str(unsigned), "--output", str(signed)],
                check=True,
            )
            print(signed)


if __name__ == "__main__":
    main()
