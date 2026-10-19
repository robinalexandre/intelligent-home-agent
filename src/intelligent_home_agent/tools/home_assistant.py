"""Home Assistant tools, with guardrails.

Guardrails, enforced in code rather than left to the model:
- Only entities in exposed domains are visible or controllable; denylisted entities never are.
- A service can only target entities of its own domain (no ``light.turn_on`` on a lock),
  and targeting keys can't be smuggled in through ``data``.
- Sensitive domains (locks, alarm, covers by default) are never executed directly: the
  action is parked and only runs after the user confirms in a *later* message, so the
  model can't confirm on the user's behalf within the same turn.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from intelligent_home_agent.ha_client import HomeAssistantClient, HomeAssistantError
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

MAX_LISTED_ENTITIES = 100
_TARGET_KEYS = {"entity_id", "device_id", "area_id", "floor_id", "label_id", "target"}
_KEPT_ATTRIBUTES = {
    "friendly_name",
    "unit_of_measurement",
    "device_class",
    "brightness",
    "color_temp_kelvin",
    "current_temperature",
    "temperature",
    "target_temp_low",
    "target_temp_high",
    "hvac_action",
    "hvac_modes",
    "preset_mode",
    "current_position",
    "media_title",
    "media_artist",
    "source",
    "volume_level",
    "battery_level",
    "humidity",
    "fan_mode",
    "percentage",
}


@dataclass(frozen=True)
class EntityPolicy:
    exposed_domains: frozenset[str]
    sensitive_domains: frozenset[str]
    denylist: frozenset[str]

    @classmethod
    def from_lists(
        cls, exposed: list[str], sensitive: list[str], denylist: list[str]
    ) -> EntityPolicy:
        return cls(frozenset(exposed), frozenset(sensitive), frozenset(denylist))

    def is_visible(self, entity_id: str) -> bool:
        return _domain_of(entity_id) in self.exposed_domains and entity_id not in self.denylist

    def is_sensitive(self, domain: str) -> bool:
        return domain in self.sensitive_domains


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _compact(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes", {})
    return {
        "entity_id": state["entity_id"],
        "name": attrs.get("friendly_name", state["entity_id"]),
        "state": state.get("state"),
    }


def build_home_assistant_tools(ha: HomeAssistantClient, policy: EntityPolicy) -> list[Tool]:
    async def _visible_state(entity_id: str) -> dict[str, Any]:
        if not policy.is_visible(entity_id):
            raise ToolError(f"Entity {entity_id} is not available to the assistant.")
        try:
            return await ha.get_state(entity_id)
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc

    async def _execute(
        domain: str, service: str, entity_ids: list[str], data: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            await ha.call_service(domain, service, {**data, "entity_id": entity_ids})
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc
        return {"status": "done", "domain": domain, "service": service, "entity_ids": entity_ids}

    # -- list_entities ---------------------------------------------------------------
    async def list_entities(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        domain = args.get("domain")
        search = _normalize(args.get("search") or "")
        try:
            states = await ha.get_states()
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc
        results = []
        for state in states:
            entity_id = state["entity_id"]
            if not policy.is_visible(entity_id):
                continue
            if domain and _domain_of(entity_id) != domain:
                continue
            compact = _compact(state)
            if search and search not in _normalize(f"{entity_id} {compact['name']}"):
                continue
            results.append(compact)
        return {
            "count": len(results),
            "entities": results[:MAX_LISTED_ENTITIES],
            "truncated": len(results) > MAX_LISTED_ENTITIES,
        }

    # -- get_entity_state ------------------------------------------------------------
    async def get_entity_state(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        state = await _visible_state(args["entity_id"])
        attrs = {k: v for k, v in state.get("attributes", {}).items() if k in _KEPT_ATTRIBUTES}
        return {
            "entity_id": state["entity_id"],
            "state": state.get("state"),
            "last_changed": state.get("last_changed"),
            "attributes": attrs,
        }

    # -- call_service ----------------------------------------------------------------
    async def call_service(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        domain: str = args["domain"]
        service: str = args["service"]
        raw_ids = args["entity_id"]
        entity_ids = [raw_ids] if isinstance(raw_ids, str) else list(raw_ids)
        data: dict[str, Any] = dict(args.get("data") or {})

        if domain not in policy.exposed_domains:
            raise ToolError(f"Domain '{domain}' is not available to the assistant.")
        if not entity_ids:
            raise ToolError("At least one entity_id is required.")
        if smuggled := _TARGET_KEYS & data.keys():
            raise ToolError(f"Targets go in entity_id only, not in data: {sorted(smuggled)}.")
        names = []
        for entity_id in entity_ids:
            if _domain_of(entity_id) != domain:
                raise ToolError(f"{entity_id} is not a '{domain}' entity.")
            state = await _visible_state(entity_id)
            names.append(_compact(state)["name"])

        if policy.is_sensitive(domain):
            summary = f"{domain}.{service} on {', '.join(names)}"
            action = ctx.conversation.add_pending(
                domain=domain,
                service=service,
                entity_ids=entity_ids,
                data=data,
                summary=summary,
            )
            return {
                "status": "needs_confirmation",
                "confirmation_id": action.id,
                "summary": summary,
                "instruction": "Ask the user to confirm out loud. Only call "
                "confirm_pending_action after they say yes in their next message.",
            }
        return await _execute(domain, service, entity_ids, data)

    # -- confirm_pending_action ------------------------------------------------------
    async def confirm_pending_action(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        conv = ctx.conversation
        action = conv.pending.get(args["confirmation_id"])
        if action is None:
            raise ToolError("No pending action with this id (it may have expired). Start over.")
        if conv.turn <= action.created_turn:
            raise ToolError("The user has not confirmed yet. Ask them and wait for their answer.")
        del conv.pending[action.id]
        return await _execute(action.domain, action.service, action.entity_ids, action.data)

    # -- get_weather_forecast --------------------------------------------------------
    async def get_weather_forecast(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        forecast_type = args.get("type", "daily")
        entity_id = args.get("entity_id")
        if not entity_id:
            listed = await list_entities({"domain": "weather"}, _)
            if not listed["entities"]:
                raise ToolError("No weather entity is available in Home Assistant.")
            entity_id = listed["entities"][0]["entity_id"]
        await _visible_state(entity_id)
        try:
            resp = await ha.call_service(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": forecast_type},
                return_response=True,
            )
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc
        forecast = (resp or {}).get("service_response", {}).get(entity_id, {}).get("forecast", [])
        limit = 5 if forecast_type == "daily" else 12
        return {"entity_id": entity_id, "type": forecast_type, "forecast": forecast[:limit]}

    entity_id_schema = {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "description": "Target entity ids, all in the same domain as the service.",
    }
    return [
        Tool(
            name="list_entities",
            description=(
                "List the home's devices and sensors with their current state. Filter by "
                "domain (light, switch, cover, climate, sensor...) and/or a search string "
                "matched against entity id and name. Use it to find entity ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "search": {"type": "string"},
                },
            },
            handler=list_entities,
        ),
        Tool(
            name="get_entity_state",
            description="Get the detailed current state of one entity.",
            input_schema={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            handler=get_entity_state,
        ),
        Tool(
            name="call_service",
            description=(
                "Control devices by calling a Home Assistant service, e.g. light.turn_on, "
                "light.turn_off, cover.close_cover, climate.set_temperature, scene.turn_on, "
                "media_player.volume_set. Extra parameters (brightness_pct, temperature, ...) "
                "go in data. Sensitive actions return needs_confirmation instead of running."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "service": {"type": "string"},
                    "entity_id": entity_id_schema,
                    "data": {"type": "object"},
                },
                "required": ["domain", "service", "entity_id"],
            },
            handler=call_service,
            needs_clean_turn=True,
        ),
        Tool(
            name="confirm_pending_action",
            description=(
                "Execute a sensitive action previously returned as needs_confirmation, only "
                "after the user explicitly said yes in a later message."
            ),
            input_schema={
                "type": "object",
                "properties": {"confirmation_id": {"type": "string"}},
                "required": ["confirmation_id"],
            },
            handler=confirm_pending_action,
            needs_clean_turn=True,
        ),
        Tool(
            name="get_weather_forecast",
            description="Get the weather forecast from Home Assistant (daily or hourly).",
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "type": {"type": "string", "enum": ["daily", "hourly"]},
                },
            },
            handler=get_weather_forecast,
        ),
    ]
