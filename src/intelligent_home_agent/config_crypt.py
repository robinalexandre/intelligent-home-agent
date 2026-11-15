"""git-crypt as a codec, for the files the agent reads and writes through the GitHub API.

Optional. A repository may encrypt some of its folders with git-crypt: a git filter that
encrypts at commit and decrypts at checkout. The API knows nothing of filters, so when the
agent's memory lives in such a repository (MEMORY_STORE=github, docs/memory.md) it encrypts
before each write and decrypts after each read, in git-crypt's own format: what it writes
is byte for byte what ``git-crypt`` would have committed, and every computer reads it
through its unlocked clone. Needs the ``cryptography`` package (the ``crypt`` extra).

The format (git-crypt 0.5 and later, key format 2): a file is ``\\0GITCRYPT\\0``, a 12-byte
nonce, then the text in AES-256-CTR. The nonce is the first 12 bytes of the HMAC-SHA1 of the
plaintext, so the same text always gives the same bytes (git needs that) and a wrong key or a
damaged file is caught on decryption. The key file (``git-crypt export-key``) is
``\\0GITCRYPTKEY``, a format version, header fields, then key entries, each a list of
(type, length, value) fields: the AES key, the HMAC key and a version.
"""

from __future__ import annotations

import base64
import fnmatch
import hmac
import importlib.util
import struct
from hashlib import sha1

FILE_MAGIC = b"\0GITCRYPT\0"
KEY_MAGIC = b"\0GITCRYPTKEY"
KEY_FORMAT = 2
NONCE_LEN = 12
AES_KEY_LEN = 32
HMAC_KEY_LEN = 64
FIELD_END, FIELD_VERSION, FIELD_AES_KEY, FIELD_HMAC_KEY = 0, 1, 3, 5
ATTRIBUTE = "filter=git-crypt"


def available() -> bool:
    """Whether the optional cryptography package is installed."""
    return importlib.util.find_spec("cryptography") is not None


class CryptError(Exception):
    pass


class ConfigCrypt:
    def __init__(self, aes_key: bytes, hmac_key: bytes, patterns: list[str] | None = None) -> None:
        if len(aes_key) != AES_KEY_LEN or len(hmac_key) != HMAC_KEY_LEN:
            raise CryptError("not a git-crypt key")
        self._aes = aes_key
        self._hmac = hmac_key
        # The folders kept encrypted, as .gitattributes patterns ("notes/**", "*.secret").
        self.patterns = list(patterns or [])

    @classmethod
    def from_key_file(cls, data: bytes, patterns: list[str] | None = None) -> ConfigCrypt:
        if not data.startswith(KEY_MAGIC):
            raise CryptError("not a git-crypt key file")
        pos = len(KEY_MAGIC)
        (version,) = struct.unpack(">I", data[pos : pos + 4])
        pos += 4
        if version != KEY_FORMAT:
            raise CryptError(f"git-crypt key format {version} is not supported")
        pos = _skip_fields(data, pos)
        best: tuple[int, bytes, bytes] | None = None
        while pos < len(data):
            entry_version, aes, mac = -1, b"", b""
            while True:
                (field,) = struct.unpack(">I", data[pos : pos + 4])
                pos += 4
                if field == FIELD_END:
                    break
                (length,) = struct.unpack(">I", data[pos : pos + 4])
                pos += 4
                value = data[pos : pos + length]
                pos += length
                if field == FIELD_VERSION:
                    (entry_version,) = struct.unpack(">I", value)
                elif field == FIELD_AES_KEY:
                    aes = value
                elif field == FIELD_HMAC_KEY:
                    mac = value
            if aes and mac and (best is None or entry_version > best[0]):
                best = (entry_version, aes, mac)
        if best is None:
            raise CryptError("the git-crypt key file holds no key")
        return cls(best[1], best[2], patterns)

    @classmethod
    def from_base64(cls, text: str, patterns: list[str] | None = None) -> ConfigCrypt:
        try:
            data = base64.b64decode("".join(text.split()), validate=True)
        except ValueError as exc:
            raise CryptError("the key is not base64") from exc
        return cls.from_key_file(data, patterns)

    @staticmethod
    def is_encrypted(data: bytes) -> bool:
        return data.startswith(FILE_MAGIC)

    def encrypts(self, path: str) -> bool:
        path = path.strip("/")
        return any(_matches(path, pattern) for pattern in self.patterns)

    def encrypt(self, plain: bytes) -> bytes:
        nonce = hmac.new(self._hmac, plain, sha1).digest()[:NONCE_LEN]
        return FILE_MAGIC + nonce + self._ctr(nonce).update(plain)

    def decrypt(self, data: bytes) -> bytes:
        """The plaintext of an encrypted file; anything else comes back unchanged."""
        if not self.is_encrypted(data):
            return data
        header = len(FILE_MAGIC) + NONCE_LEN
        nonce = data[len(FILE_MAGIC) : header]
        plain = self._ctr(nonce).update(data[header:])
        if not hmac.compare_digest(hmac.new(self._hmac, plain, sha1).digest()[:NONCE_LEN], nonce):
            raise CryptError("wrong key or damaged encrypted file")
        return plain

    def encode(self, path: str, plain: bytes) -> bytes:
        """What to write at ``path``: encrypted when the path is in an encrypted folder."""
        return self.encrypt(plain) if self.encrypts(path) else plain

    def _ctr(self, nonce: bytes):  # noqa: ANN202 - a cryptography encryptor context
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as exc:  # the optional "crypt" extra
            raise CryptError(
                'git-crypt support needs the cryptography package: pip install ".[crypt]"'
            ) from exc
        return Cipher(algorithms.AES(self._aes), modes.CTR(nonce + b"\0\0\0\0")).encryptor()


def patterns_from_attributes(text: str) -> list[str]:
    """The git-crypt patterns of a .gitattributes file."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not parts[0].startswith("#") and ATTRIBUTE in parts[1:]:
            out.append(parts[0])
    return out


def _skip_fields(data: bytes, pos: int) -> int:
    while True:
        (field,) = struct.unpack(">I", data[pos : pos + 4])
        pos += 4
        if field == FIELD_END:
            return pos
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        pos += 4 + length


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    if "/" not in pattern:
        return fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatch(path, pattern)
