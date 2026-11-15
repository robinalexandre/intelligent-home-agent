import importlib.util
import plistlib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_shortcuts.py"
spec = importlib.util.spec_from_file_location("build_shortcuts", SCRIPT)
shortcuts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shortcuts)


def test_main_shortcut_calls_itself_by_its_name_and_serializes():
    actions = shortcuts.main_shortcut("https://agent.example.com", "tok", "Nova", "Yes?")
    calls = [a for a in actions if a["WFWorkflowActionIdentifier"].endswith("runworkflow")]
    assert calls[0]["WFWorkflowActionParameters"]["WFWorkflowName"] == "Nova"
    request = next(a for a in actions if a["WFWorkflowActionIdentifier"].endswith("downloadurl"))
    assert request["WFWorkflowActionParameters"]["WFURL"] == "https://agent.example.com/v1/ask"
    plistlib.dumps(shortcuts.workflow(actions, 1, 1, True), fmt=plistlib.FMT_BINARY)


def test_leaving_home_tells_the_agent_and_shows_what_it_found():
    actions = shortcuts.leave_home("https://agent.example.com/", "tok", "Leaving home")
    kinds = [a["WFWorkflowActionIdentifier"].rsplit(".", 1)[-1] for a in actions]
    assert kinds == ["downloadurl", "getvalueforkey", "conditional", "notification", "conditional"]
    request = actions[0]["WFWorkflowActionParameters"]
    assert request["WFURL"] == "https://agent.example.com/v1/departure"
    assert request["WFHTTPMethod"] == "POST"
    body = request["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"][0]
    # The main Shortcut's conversation: a yes said to it runs the fixes.
    assert body["WFKey"]["Value"]["string"] == "conversation_id"
    assert body["WFValue"]["Value"]["string"] == "siri"
    plistlib.dumps(shortcuts.workflow(actions, 1, 1), fmt=plistlib.FMT_BINARY)


def test_every_language_has_the_same_strings():
    keys = {frozenset(s) for s in shortcuts.STRINGS.values()}
    assert len(keys) == 1
