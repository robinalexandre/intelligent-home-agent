"""Parcels the user is waiting for, kept by the agent itself.

A shipping email may be deleted or archived by the time the parcel comes, so it is noted
when it is read. The list is the agent's own notes from what others wrote (shops, carriers): it can
be written in any request, as it acts on nothing, and reading it back taints the request
like the emails it came from. Delivered parcels are forgotten after a week.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

KEEP_DELIVERED_DAYS = 7
MAX_DELIVERIES = 50
MAX_FIELD_CHARS = 300
FIELDS = ("item", "shop", "carrier", "tracking", "expected", "status", "pickup", "note")
STATUSES = ("ordered", "shipped", "out_for_delivery", "pickup_ready", "delivered", "issue")


class Deliveries:
    def __init__(self, file: Path, clock: Any = time.time) -> None:
        self._file = file
        self._clock = clock

    def all(self) -> list[dict[str, Any]]:
        try:
            items = json.loads(self._file.read_text())
        except (OSError, ValueError):
            return []
        cutoff = self._clock() - KEEP_DELIVERED_DAYS * 86400
        return [d for d in items if d.get("status") != "delivered" or d["updated"] >= cutoff]

    def _write(self, items: list[dict[str, Any]]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(items[-MAX_DELIVERIES:], ensure_ascii=False))
        tmp.replace(self._file)

    def note(self, fields: dict[str, Any], delivery_id: str | None = None) -> dict[str, Any]:
        items = self.all()
        clean = {k: " ".join(str(v).split())[:MAX_FIELD_CHARS] for k, v in fields.items() if v}
        if clean.get("status") and clean["status"] not in STATUSES:
            raise ToolError(f"status is one of {', '.join(STATUSES)}.")
        if delivery_id:
            found = next((d for d in items if d["id"] == delivery_id), None)
            if found is None:
                raise ToolError("No parcel with this id: see list_deliveries.")
            found.update(clean, updated=self._clock())
        else:
            if not clean.get("item"):
                raise ToolError("item is required for a new parcel.")
            found = {"id": uuid.uuid4().hex[:6], "status": "ordered", **clean}
            found["updated"] = self._clock()
            items.append(found)
        self._write(items)
        return found


def build_delivery_tools(deliveries: Deliveries) -> list[Tool]:
    async def note_delivery(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        saved = deliveries.note({k: args.get(k) for k in FIELDS}, args.get("id"))
        return {"noted": saved["id"]}

    async def list_deliveries(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return {
            "deliveries": [{k: v for k, v in d.items() if k != "updated"} for d in deliveries.all()]
        }

    properties: dict[str, Any] = {k: {"type": "string"} for k in FIELDS}
    properties["expected"] = {"type": "string", "description": "YYYY-MM-DD, or a range"}
    properties["status"] = {"type": "string", "enum": list(STATUSES)}
    properties["pickup"] = {"type": "string", "description": "Pickup point and its hours"}
    properties["id"] = {"type": "string", "description": "To update a parcel already noted"}
    return [
        Tool(
            name="note_delivery",
            description=(
                "Note a parcel the user waits for, or update it (with its id), as soon as you "
                "read an order, shipping or delivery email: it may be gone later. One per "
                "order; a pickup code goes in pickup, never a password."
            ),
            input_schema={"type": "object", "properties": properties},
            handler=note_delivery,
        ),
        Tool(
            name="list_deliveries",
            description="The parcels the user waits for, from your notes (delivered ones a week).",
            input_schema={"type": "object", "properties": {}},
            handler=list_deliveries,
            taints=True,
        ),
    ]
