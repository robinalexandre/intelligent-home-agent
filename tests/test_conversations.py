from intelligent_home_agent.conversations import Conversation, ConversationStore


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_store_expires_idle_conversations():
    clock = Clock()
    store = ConversationStore(ttl_seconds=60, clock=clock)
    conv = store.get("a")
    conv.messages.append({"role": "user", "content": "hi"})
    clock.now = 30
    assert store.get("a") is conv
    clock.now = 100
    assert store.get("a").messages == []


def test_trim_never_starts_with_tool_result():
    conv = Conversation(id="c")
    conv.messages = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "{}"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    conv.trim(4)
    assert conv.messages[0] == {"role": "user", "content": "two"}
