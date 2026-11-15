import pytest

from intelligent_home_agent.tools import ToolError
from intelligent_home_agent.tools.deliveries import Deliveries, build_delivery_tools


async def test_a_parcel_is_noted_updated_and_forgotten_a_week_after_delivery(tmp_path, ctx):
    now = [1_000_000.0]
    deliveries = Deliveries(tmp_path / "d.json", clock=lambda: now[0])
    note, listing = build_delivery_tools(deliveries)

    noted = await note.handler(
        {"item": "Desk lamp", "shop": "Lumen", "carrier": "FastPost", "expected": "2026-10-24"},
        ctx,
    )
    await note.handler(
        {"id": noted["noted"], "status": "pickup_ready", "pickup": "Corner shop"}, ctx
    )
    parcels = (await listing.handler({}, ctx))["deliveries"]

    assert parcels == [
        {
            "id": noted["noted"],
            "status": "pickup_ready",
            "item": "Desk lamp",
            "shop": "Lumen",
            "carrier": "FastPost",
            "expected": "2026-10-24",
            "pickup": "Corner shop",
        }
    ]
    assert listing.taints and not note.needs_clean_turn

    await note.handler({"id": noted["noted"], "status": "delivered"}, ctx)
    now[0] += 8 * 86400
    assert (await listing.handler({}, ctx))["deliveries"] == []


async def test_bad_notes_are_refused(tmp_path, ctx):
    note, _ = build_delivery_tools(Deliveries(tmp_path / "d.json"))
    with pytest.raises(ToolError, match="item is required"):
        await note.handler({"carrier": "UPS"}, ctx)
    with pytest.raises(ToolError, match="status is one of"):
        await note.handler({"item": "x", "status": "lost"}, ctx)
    with pytest.raises(ToolError, match="No parcel"):
        await note.handler({"id": "nope", "status": "delivered"}, ctx)
