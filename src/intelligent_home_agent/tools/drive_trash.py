"""A local copy of what the agent deletes from a drive, kept 30 days.

Some services keep what is deleted in a trash of their own, others (a NAS over SFTP, a
bucket) don't, and none of those trashes can be emptied back from here. So before deleting
a file or a folder the agent copies it here, on its data volume (it survives restarts and
updates), and can put it back where it was. The disk is not only the agent's: an item over
``max_item_bytes``, a trash over ``max_total_bytes`` or a disk that would keep less than
MIN_FREE_SHARE free is refused, and the user decides whether to delete without a copy.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from intelligent_home_agent.tools.base import ToolError

KEEP_DAYS = 30
MAX_ITEM_BYTES = 1024**3
MAX_TOTAL_BYTES = 5 * 1024**3
MIN_FREE_SHARE = 0.15
UNFINISHED_SECONDS = 86400


class TooBigToKeep(ToolError):
    pass


class LocalTrash:
    def __init__(
        self,
        directory: Path,
        keep_days: float = KEEP_DAYS,
        max_item_bytes: int = MAX_ITEM_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        min_free_share: float = MIN_FREE_SHARE,
        clock: Any = time.time,
        disk_usage: Any = shutil.disk_usage,
    ) -> None:
        self._dir = directory
        self._keep_seconds = keep_days * 86400
        self._max_item = max_item_bytes
        self._max_total = max_total_bytes
        self._min_free_share = min_free_share
        self._clock = clock
        self._disk_usage = disk_usage
        # One copy at a time: the room checked for one must not be taken by another.
        self.lock = asyncio.Lock()

    def _meta(self, item: Path) -> dict[str, Any] | None:
        try:
            return json.loads((item / "meta.json").read_text())
        except (OSError, ValueError):
            return None

    def items(self) -> list[dict[str, Any]]:
        """What is kept, newest first; anything past its days, or never finished, goes."""
        if not self._dir.exists():
            return []
        cutoff = self._clock() - self._keep_seconds
        kept = []
        for item in self._dir.iterdir():
            meta = self._meta(item) if item.is_dir() else None
            if meta is None:
                # Still being copied, or left by a crash: only gone once it is surely stale.
                if item.stat().st_mtime < self._clock() - UNFINISHED_SECONDS:
                    shutil.rmtree(item, ignore_errors=True)
            elif meta["deleted_at"] < cutoff:
                shutil.rmtree(item, ignore_errors=True)
            else:
                kept.append(meta)
        return sorted(kept, key=lambda m: m["deleted_at"], reverse=True)

    def check_room(self, size: int) -> None:
        if size < 0:
            raise TooBigToKeep("Its size is unknown, so no copy can be promised.")
        if size > self._max_item:
            raise TooBigToKeep(
                f"{size / 1024**2:.0f} MB is too big to keep a copy here "
                f"(at most {self._max_item / 1024**2:.0f} MB)."
            )
        if sum(m["bytes"] for m in self.items()) + size > self._max_total:
            raise TooBigToKeep("The local trash is full: delete some of its copies for good first.")
        self._dir.mkdir(parents=True, exist_ok=True)
        disk = self._disk_usage(self._dir)
        if disk.free - size < disk.total * self._min_free_share:
            raise TooBigToKeep("The agent's disk is too full to keep a copy of it.")

    def open(self) -> Path:
        """A new item's folder; its content goes under content/."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
        item = self._dir / f"{stamp}-{uuid.uuid4().hex[:6]}"
        (item / "content").mkdir(parents=True)
        return item

    def close(
        self, item: Path, drive: str, original: str, kind: str, size: int, files: int
    ) -> dict[str, Any]:
        meta = {
            "id": item.name,
            "drive": drive,
            "original": original,
            "kind": kind,
            "bytes": size,
            "files": files,
            "deleted_at": self._clock(),
        }
        (item / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        return meta

    def discard(self, item: Path) -> None:
        shutil.rmtree(item, ignore_errors=True)

    def get(self, item_id: str) -> tuple[dict[str, Any], Path]:
        meta = next((m for m in self.items() if m["id"] == item_id), None)
        if meta is None:
            raise ToolError("Nothing with this id in the local trash: see list_drive_trash.")
        return meta, self._dir / item_id / "content"

    def purge(self, item_id: str) -> dict[str, Any]:
        meta, _ = self.get(item_id)
        shutil.rmtree(self._dir / item_id)
        return meta

    def kept_until(self, meta: dict[str, Any]) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(meta["deleted_at"] + self._keep_seconds))
