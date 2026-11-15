import time

from intelligent_home_agent.conversations import (
    Attachment,
    Conversation,
    ConversationStore,
    FileStore,
    file_hash,
    safe_filename,
)


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


def test_peek_never_creates_a_conversation():
    clock = Clock()
    store = ConversationStore(ttl_seconds=60, clock=clock)
    assert store.peek("a") is None
    conv = store.get("a")
    assert store.peek("a") is conv
    clock.now = 100
    assert store.peek("a") is None


def test_a_conversation_can_outlive_the_default_ttl():
    clock = Clock()
    store = ConversationStore(ttl_seconds=60, clock=clock)
    chat = store.get("chat", ttl_seconds=3600)
    voice = store.get("voice")
    clock.now = 600
    assert store.get("chat") is chat
    assert store.get("voice") is not voice


def test_a_saved_conversation_survives_a_restart(tmp_path):
    path = tmp_path / "chat.json"
    clock = Clock()
    store = ConversationStore(ttl_seconds=60, clock=clock)
    chat = store.get("chat", ttl_seconds=3600)
    chat.messages = [{"role": "user", "content": "Hello"}]
    chat.turn = 1
    store.save(chat, path)

    restarted = ConversationStore(ttl_seconds=60, clock=Clock())
    restored = restarted.restore(path)
    assert restored is not None and restored.messages == chat.messages and restored.turn == 1
    assert restarted.get("chat") is restored
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_an_expired_saved_conversation_is_not_restored(tmp_path):
    path = tmp_path / "chat.json"
    store = ConversationStore(ttl_seconds=60)
    store.save(store.get("chat", ttl_seconds=0.001), path)
    time.sleep(0.01)
    assert ConversationStore(ttl_seconds=60).restore(path) is None


def test_files_are_kept_thirty_days_then_deleted(tmp_path):
    now = [1_000_000.0]
    store = FileStore(tmp_path / "files", keep_days=30, clock=lambda: now[0])
    chat = Conversation(id="chat")
    chat.attach_store(store)
    chat.add_file(Attachment("quote.pdf", "application/pdf", b"%PDF-1", "user"))
    chat.keep_file(Attachment("quote.pdf", "application/pdf", b"%PDF-2", "drive"))

    later = Conversation(id="chat")  # e.g. after a restart, or the next day
    later.attach_store(store)
    assert {n: (f.data, f.origin) for n, f in later.files.items()} == {
        "quote.pdf": (b"%PDF-1", "user"),
        "quote (drive).pdf": (b"%PDF-2", "drive"),
    }
    assert all(oct(p.stat().st_mode)[-3:] == "600" for p in (tmp_path / "files").iterdir())

    now[0] += 31 * 86400
    expired = Conversation(id="chat")
    expired.attach_store(store)
    assert expired.files == {}
    assert [p.name for p in (tmp_path / "files").iterdir()] == ["index.json"]


def test_a_kept_file_never_replaces_another_ones_content():
    conv = Conversation(id="c")
    first = conv.keep_file(Attachment("invoice.pdf", "application/pdf", b"one", "mail"))
    same = conv.keep_file(Attachment("invoice.pdf", "application/pdf", b"one", "mail"))
    other = conv.keep_file(Attachment("invoice.pdf", "application/pdf", b"two", "mail"))
    third = conv.keep_file(Attachment("invoice.pdf", "application/pdf", b"three", "mail"))

    assert first.filename == same.filename == "invoice.pdf"
    assert other.filename == "invoice (mail).pdf" and third.filename == "invoice (mail 2).pdf"
    assert conv.files["invoice.pdf"].data == b"one"

    # The agent's own files replace their earlier version.
    conv.keep_file(Attachment("form.pdf", "application/pdf", b"v1", "agent"))
    again = conv.keep_file(Attachment("form.pdf", "application/pdf", b"v2", "agent"))
    assert again.filename == "form.pdf" and conv.files["form.pdf"].data == b"v2"


def test_file_names_are_made_safe_and_found_whatever_the_accents_form():
    assert safe_filename("../../etc/pass\nwd") == "_.._etc_pass_wd"
    assert safe_filename("") == "file"
    assert safe_filename("a" * 200 + ".pdf") == "a" * 96 + ".pdf"
    decomposed = "Café.pdf"  # as some phones send it (NFD)
    file = Attachment(decomposed, "application/pdf", b"%PDF")
    assert file.filename == "Café.pdf"

    conv = Conversation(id="c")
    conv.add_file(file)
    assert decomposed in conv.files and conv.files.get("Café.pdf") is file
    assert file_hash(file) == file_hash(Attachment("other.pdf", "application/pdf", b"%PDF"))


def test_a_proposal_is_numbered_once_and_remembers_the_files_it_names():
    conv = Conversation(id="c", turn=3)
    conv.add_file(Attachment("a.pdf", "application/pdf", b"A"))
    args = {"file_name": "a.pdf", "attachments": ["a.pdf", "missing.pdf"]}

    first, new = conv.propose("save", args, "Save a.pdf")
    again, newer = conv.propose("save", args, "Save a.pdf")
    second, _ = conv.propose("save", {"file_name": "b.pdf"}, "Save b.pdf")

    assert new and not newer and again is first
    assert (first.number, second.number, first.created_turn) == (1, 2, 3)
    assert first.file_hashes == {"a.pdf": file_hash(conv.files["a.pdf"])}
    conv.mark_shown([2])
    conv.drop_unshown()
    assert [p.number for p in conv.proposals] == [2]


def test_a_quote_of_a_clean_reply_is_recognized_even_cut_short():
    conv = Conversation(id="c")
    conv.note_clean_reply("Sprint points:\n- Alex: 20\n- Sam: 13")
    assert conv.said_cleanly("Sprint points: - Alex")
    assert not conv.said_cleanly("Alex: 20")
    assert not conv.said_cleanly("")


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
