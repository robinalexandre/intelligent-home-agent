import pytest

from intelligent_home_agent.tools import (
    ToolError,
    ToolRegistry,
    TurnedDown,
    build_turned_down_tools,
)


async def test_a_turned_down_alert_is_listed_with_the_memory(tmp_path, ctx):
    turned = TurnedDown(tmp_path / "data" / "t.json", "Europe/Paris")
    registry = ToolRegistry(build_turned_down_tools(turned))
    assert turned.prompt_text() == ""
    ctx.tainted = True  # said in reply to a briefing that read emails: still kept
    await registry.run("turn_down_alert", {"about": "Late parcel from the bookshop"}, ctx)
    again = await registry.run("turn_down_alert", {"about": "late parcel from the  bookshop"}, ctx)
    assert again == {"result": "already turned down"}
    assert "- Late parcel from the bookshop (since " in turned.prompt_text()
    listed = await registry.run("list_turned_down_alerts", {}, ctx)
    assert [i["about"] for i in listed["turned_down"]] == ["Late parcel from the bookshop"]
    # Another instance (after a restart) reads the same file.
    assert TurnedDown(tmp_path / "data" / "t.json", "UTC").items() == listed["turned_down"]
    assert await registry.run("bring_back_alert", {"words": "BOOKSHOP"}, ctx) == {"brought_back": 1}
    assert turned.prompt_text() == ""


async def test_vague_words_are_refused_and_a_damaged_file_is_an_empty_list(tmp_path, ctx):
    file = tmp_path / "t.json"
    file.write_text("{not json")
    turned = TurnedDown(file, "UTC")
    assert turned.items() == []
    with pytest.raises(ToolError, match="which alert"):
        turned.add("ok")
    with pytest.raises(ToolError, match="bring back"):
        turned.remove("a")
    assert build_turned_down_tools(None) == []
