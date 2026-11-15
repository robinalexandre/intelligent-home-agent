import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from intelligent_home_agent.config_crypt import ConfigCrypt, CryptError, patterns_from_attributes

# A key file in the format of `git-crypt export-key` (format 2), made for this test from
# counting bytes (AES key 00..1f, HMAC key 40..7f), and `notes/note.md` as git-crypt
# stores it with that key, computed apart with openssl (AES-256-CTR) and HMAC-SHA1: the
# codec must read and write exactly these bytes.
KEY = (
    "AEdJVENSWVBUS0VZAAAAAgAAAAAAAAABAAAABAAAAAAAAAADAAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZ"
    "GhscHR4fAAAABQAAAEBAQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVpbXF1eX2BhYmNkZWZnaGlqa2xtbm9wcXJz"
    "dHV2d3h5ent8fX5/AAAAAA=="
)
ENCRYPTED = base64.b64decode(
    "AEdJVENSWVBUAM8NTObUMWYG+MsUJ/+Q5fUwp+W5OdO41G302YDco4usC5YJEjjTUA0ngCgLvmbMPE2aa11l3jiT"
    "+1mcvPDPhmeu6N0kbh/cJXd77qjl/2aWpg=="
)
PLAIN = "Acme Books is the accounting firm.\nSecond line, with accents: été.\n".encode()


def crypt() -> ConfigCrypt:
    return ConfigCrypt.from_base64(KEY, ["notes/**", "nova/**", "*.secret"])


def test_reads_and_writes_what_git_crypt_does():
    c = crypt()
    assert c.decrypt(ENCRYPTED) == PLAIN
    assert c.encrypt(PLAIN) == ENCRYPTED
    assert ConfigCrypt.is_encrypted(ENCRYPTED) and not ConfigCrypt.is_encrypted(PLAIN)


def test_plain_files_pass_through_and_the_perimeter_decides_writes():
    c = crypt()
    assert c.decrypt(PLAIN) == PLAIN
    assert c.encrypts("notes/a.md") and c.encrypts("nova/memory.md")
    assert c.encrypts("other/x.secret") and not c.encrypts("reminders/a.md")
    assert c.encode("reminders/a.md", PLAIN) == PLAIN
    assert c.encode("/notes/note.md", PLAIN) == ENCRYPTED


def test_a_wrong_key_or_a_damaged_file_is_refused():
    other = ConfigCrypt(b"k" * 32, b"h" * 64)
    with pytest.raises(CryptError):
        other.decrypt(ENCRYPTED)
    with pytest.raises(CryptError):
        crypt().decrypt(ENCRYPTED[:-1] + bytes([ENCRYPTED[-1] ^ 1]))
    with pytest.raises(CryptError):
        ConfigCrypt.from_base64("not base64 at all!")
    with pytest.raises(CryptError):
        ConfigCrypt.from_key_file(b"\0GITCRYPTKEY" + b"\0\0\0\1")
    with pytest.raises(CryptError):
        ConfigCrypt(b"short", b"h" * 64)


def test_patterns_come_from_gitattributes():
    text = (
        "* text=auto eol=lf\n*.json merge=json\n"
        "# encrypted folders\n"
        "notes/** filter=git-crypt diff=git-crypt\n"
        "nova/** filter=git-crypt diff=git-crypt\n"
    )
    assert patterns_from_attributes(text) == ["notes/**", "nova/**"]
    # Without a perimeter nothing new is encrypted; a file found encrypted still reads.
    bare = ConfigCrypt.from_base64(KEY)
    assert bare.patterns == [] and not bare.encrypts("nova/memory.md")
    assert bare.decrypt(ENCRYPTED) == PLAIN


@pytest.mark.skipif(shutil.which("git-crypt") is None, reason="git-crypt is not installed")
def test_against_the_installed_git_crypt(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "notes").mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run(["git-crypt", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git-crypt", "export-key", str(tmp_path / "key")], cwd=repo, check=True)
    (repo / ".gitattributes").write_text("notes/** filter=git-crypt diff=git-crypt\n")
    text = "Hello, this is a note to encrypt, with an accent: été.\n" * 40
    (repo / "notes" / "n.md").write_text(text)
    subprocess.run([*git, "add", ".gitattributes", "notes/n.md"], check=True)
    staged = subprocess.run([*git, "show", ":notes/n.md"], check=True, capture_output=True).stdout
    c = ConfigCrypt.from_key_file((tmp_path / "key").read_bytes())
    assert c.decrypt(staged) == text.encode()
    assert c.encrypt(text.encode()) == staged
