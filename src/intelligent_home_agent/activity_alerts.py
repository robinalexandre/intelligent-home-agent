"""Activity alerts: a message when conditions are coming up good for something the user does.

"Tell me when it's good for wingfoil at North Beach", "warn me a day ahead of a dry window
for cycling": an alert is one activity of the catalog (activities.py), judged the way its
forecast tool judges it:

- wind sports, per spot: wind at least the spot's minimum, from one of its directions;
- surf, per beach: waves within the beach's range, a long enough period, no onshore wind;
- everything else (cycling, running, hiking, skiing...): the activity's own limits on rain,
  wind, temperature and visibility, at home or in a named place.

The code watches, not the model. Every hour it reads the forecast (Open-Meteo, the same
numbers as the tools) and, when a daylight window of at least ``min_hours`` starts within
``lead_hours``, sends one message (main.tell_user: the chat, else a push), once per alert
and per day of window. A wind alert can instead watch the wind measured at the spot
(``when: now``, tools/live_wind.py), looked at every ten minutes. Nothing is sent when
nothing fits, nor during the quiet hours. The words are the code's: numbers, compass
points, the spot's name.

No alert exists until the user asks for one (set_activity_alert) or the settings name
some (ACTIVITY_ALERTS). Turning one down only makes the agent quieter, so it runs in any
request; setting one up waits for the user's go after a read, like any write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intelligent_home_agent.activities import CATALOG, Activity, match_activities
from intelligent_home_agent.config import HomeNow, SurfSpot, WindSpot
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, describe_whole
from intelligent_home_agent.tools.outdoor import outdoor_days
from intelligent_home_agent.tools.surf import surf_report
from intelligent_home_agent.tools.weather import OpenMeteo, spots_now, wind_report

log = logging.getLogger(__name__)

# How often the loop wakes up: the measured wind is looked at every time, a forecast at
# most once every FORECAST_SECONDS per alert.
WATCH_SECONDS = 600
FORECAST_SECONDS = 3600
MAX_ALERTS = 20
MAX_LEAD_HOURS = 72
KEEP_SENT_DAYS = 10
ICONS = {"wind": "🌬️", "surf": "🌊", "weather": "☀️"}

# What a spot's anemometers measure now (tools/live_wind.live_wind_reporter).
Reporter = Callable[[Any, datetime], Awaitable[dict[str, Any]]]
# Sends the words to the user; falsy when nothing took them (tried again later).
Send = Callable[[str], Awaitable[Any]]
Clock = Callable[[], datetime]


@dataclass
class ActivityAlert:
    id: int
    activity: str
    # Wind and surf: one of the user's spots; empty: any of them in season.
    spot: str = ""
    # Other activities: a named place; empty: home.
    place: str = ""
    latitude: float | None = None
    longitude: float | None = None
    # "forecast": a window coming up. "now": the wind measured at the spot (wind sports).
    when: str = "forecast"
    lead_hours: int = 24
    min_hours: int = 2
    # Wind sports: instead of the spot's own minimum; gusts for "now" alerts only.
    min_knots: float | None = None
    gust_knots: float | None = None
    # Surf: only the windows rated good, not the fair ones.
    good_only: bool = False
    # Created from ACTIVITY_ALERTS rather than asked for in a request.
    from_settings: bool = False
    off: bool = False
    muted_until: str = ""  # YYYY-MM-DD: nothing sent on or before
    sent_for: list[str] = field(default_factory=list)  # days already announced

    def view(self) -> dict[str, Any]:
        """The alert for the model: what it watches and its state, no bookkeeping."""
        out: dict[str, Any] = {"id": self.id, "activity": self.activity, "when": self.when}
        if CATALOG[self.activity].kind == "weather":
            out["place"] = self.place or "home"
        else:
            out["spot"] = self.spot or "any spot in season"
        if self.when == "forecast":
            out["told_up_to_hours_before"] = self.lead_hours
            out["window_of_at_least_hours"] = self.min_hours
        for name in ("min_knots", "gust_knots"):
            if getattr(self, name) is not None:
                out[name] = getattr(self, name)
        if self.good_only:
            out["good_only"] = True
        out["state"] = "off" if self.off else "on"
        if self.muted_until and not self.off:
            out["state"] = f"paused until after {self.muted_until}"
        return out


@dataclass
class Window:
    """A stretch of good hours, or a reading that passes ("now")."""

    day: str  # YYYY-MM-DD
    start: str  # HH:MM
    end: str
    where: str
    kind: str
    values: dict[str, Any]

    @property
    def hours(self) -> int:
        return int(self.end[:2]) - int(self.start[:2])


class Conditions:
    """What the forecasts (and the anemometers) say for an alert, as windows."""

    def __init__(
        self,
        meteo: OpenMeteo,
        *,
        wind_spots: list[WindSpot],
        surf_spots: list[SurfSpot],
        model: str,
        home: HomeNow,
        live: Reporter | None = None,
    ) -> None:
        self.meteo = meteo
        self.wind_spots = wind_spots
        self.surf_spots = surf_spots
        self._model = model
        self.home = home
        self.live = live

    def spots(self, alert: ActivityAlert) -> list[Any]:
        kind = CATALOG[alert.activity].kind
        spots = self.wind_spots if kind == "wind" else self.surf_spots
        today = date.fromisoformat(self.meteo.now_hour()[:10])
        selected = spots_now(spots, today, alert.spot or None)
        if kind == "wind" and alert.min_knots is not None:
            selected = [s.model_copy(update={"min_knots": alert.min_knots}) for s in selected]
        return selected

    async def windows(self, alert: ActivityAlert) -> list[Window]:
        activity = CATALOG[alert.activity]
        days = alert.lead_hours // 24 + 2
        if activity.kind == "wind":
            reports = await wind_report(self.meteo, self.spots(alert), days=days, model=self._model)
            return [
                Window(
                    day["date"],
                    w["from"],
                    w["to"],
                    report["name"],
                    "wind",
                    {
                        "knots": w["avg_knots"],
                        "gusts": w["max_gust_knots"],
                        "direction": w["direction"],
                    },
                )
                for report in reports
                for day in report["days"]
                for w in day.get("rideable_windows", [])
            ]
        if activity.kind == "surf":
            reports = await surf_report(self.meteo, self.spots(alert), days=days, model=self._model)
            return [
                Window(
                    day["date"],
                    w["from"],
                    w["to"],
                    report["name"],
                    "surf",
                    {"waves": w["waves_m"], "period": w["period_s"]},
                )
                for report in reports
                for day in report["days"]
                for w in day.get("surfable_windows", [])
                if w["quality"] == "good" or not alert.good_only
            ]
        if alert.latitude is not None and alert.longitude is not None:
            where, here = alert.place, (alert.latitude, alert.longitude)
        elif (here := self.home()) is not None:
            where = ""
        else:
            raise ToolError("The home location is not configured.")
        found = await outdoor_days(self.meteo, activity, here[0], here[1], days=days)
        return [
            Window(
                day["date"],
                w["from"],
                w["to"],
                where,
                "weather",
                {
                    "temp": w["temp_c"],
                    "rain": w["max_rain_pct"],
                    "wind": w["max_wind_kmh"] if activity.max_wind_kmh is not None else None,
                },
            )
            for day in found
            for w in day.get("good_windows", [])
        ]

    async def measured(self, alert: ActivityAlert, now: datetime) -> list[dict[str, Any]]:
        """Each reading near the alert's spots that passes its thresholds."""
        if self.live is None:
            return []
        passing = []
        for spot in self.spots(alert):
            report = await self.live(spot, now)
            for reading in report.get("readings") or []:
                gusts = reading.get("gust_knots")
                if (reading.get("avg_knots") or 0) < spot.min_knots:
                    continue
                if alert.gust_knots is not None and (gusts or 0) < alert.gust_knots:
                    continue
                if not reading.get("direction_ok", True):
                    continue
                passing.append({**reading, "spot": spot.name})
        return passing


class ActivityAlerts:
    def __init__(
        self,
        file: Path,
        conditions: Conditions,
        timezone: str,
        *,
        language: str = "en",
        lead_hours: int = 24,
        quiet: tuple[int, int] = (22, 8),
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._file = file
        self.conditions = conditions
        self._tz = ZoneInfo(timezone)
        self._s = strings(language)
        self.lead_hours = lead_hours
        self._quiet = quiet
        self._clock = clock
        self._looked: dict[int, datetime] = {}
        self.alerts: list[ActivityAlert] = []
        try:
            known = {f.name for f in fields(ActivityAlert)}
            self.alerts = [
                ActivityAlert(**{k: v for k, v in a.items() if k in known})
                for a in json.loads(file.read_text(encoding="utf-8"))
                if a.get("activity") in CATALOG
            ]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("Activity alerts unreadable, starting empty: %s", exc)

    def save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(self._file.name + ".tmp")
        tmp.write_text(
            json.dumps([asdict(a) for a in self.alerts], ensure_ascii=False), encoding="utf-8"
        )
        os.chmod(tmp, 0o600)
        tmp.replace(self._file)

    def _local(self) -> datetime:
        return self._clock().astimezone(self._tz)

    def quiet_now(self) -> bool:
        hour = self._local().hour
        start, end = self._quiet
        return hour >= start or hour < end if start > end else start <= hour < end

    # --- The list ------------------------------------------------------------------

    def can_watch(self, activity: Activity, when: str = "forecast") -> str | None:
        """Why an alert for this activity could never fire; None when it can."""
        c = self.conditions
        if activity.kind == "wind" and not c.wind_spots:
            return "No wind spot is configured (WIND_SPOTS): wind sports are judged per spot."
        if activity.kind == "surf" and not c.surf_spots:
            return "No surf spot is configured (SURF_SPOTS): surf is judged per beach."
        if when == "now" and activity.kind != "wind":
            return 'when "now" watches the measured wind: wind sports only.'
        if when == "now" and c.live is None:
            return "No source of measured wind is configured (LIVE_WIND_SOURCES)."
        return None

    def seed(self, activities: list[str]) -> None:
        """One alert per activity named in the settings, created once: an alert the user
        turned off stays off, and one whose activity left the settings goes."""
        before = [asdict(a) for a in self.alerts]
        self.alerts = [a for a in self.alerts if not a.from_settings or a.activity in activities]
        for key in activities:
            if any(a.from_settings and a.activity == key for a in self.alerts):
                continue
            problem = self.can_watch(CATALOG[key])
            if CATALOG[key].kind == "weather" and self.conditions.home() is None:
                problem = "The home location is not configured (HOME_LATITUDE, HOME_LONGITUDE)."
            if problem:
                log.warning("No alert for %s: %s", key, problem)
                continue
            self.alerts.append(
                ActivityAlert(
                    id=self._next_id(),
                    activity=key,
                    lead_hours=self.lead_hours,
                    from_settings=True,
                )
            )
        if [asdict(a) for a in self.alerts] != before:
            self.save()

    def _next_id(self) -> int:
        return max((a.id for a in self.alerts), default=0) + 1

    def get(self, number: Any) -> ActivityAlert:
        try:
            wanted = int(number)
        except (TypeError, ValueError) as exc:
            raise ToolError("id is an alert's number, from list_activity_alerts.") from exc
        for alert in self.alerts:
            if alert.id == wanted:
                return alert
        raise ToolError(f"No activity alert n°{wanted}: see list_activity_alerts.")

    def add(self, activity: str, **given: Any) -> ActivityAlert:
        found, _ = match_activities(activity)
        if len(found) != 1:
            raise ToolError(f"One activity per alert, among: {', '.join(CATALOG)}.")
        if len([a for a in self.alerts if not a.off]) >= MAX_ALERTS:
            raise ToolError(f"{MAX_ALERTS} alerts at most: turn one off first.")
        alert = ActivityAlert(id=self._next_id(), activity=found[0], lead_hours=self.lead_hours)
        for key, value in given.items():
            if value is not None:
                setattr(alert, key, value)
        kind = CATALOG[alert.activity].kind
        if alert.when not in ("forecast", "now"):
            raise ToolError('when is "forecast" or "now".')
        if problem := self.can_watch(CATALOG[alert.activity], alert.when):
            raise ToolError(problem)
        if not 1 <= alert.lead_hours <= MAX_LEAD_HOURS:
            raise ToolError(f"lead_hours runs from 1 to {MAX_LEAD_HOURS}.")
        if not 1 <= alert.min_hours <= 12:
            raise ToolError("min_hours runs from 1 to 12.")
        if kind == "weather":
            alert.spot = ""
            if alert.latitude is None and self.conditions.home() is None:
                raise ToolError("The home location is not configured: give a place.")
        else:
            alert.place, alert.latitude, alert.longitude = "", None, None
            if alert.spot:
                # The spot's own name, and a ToolError for one that is not the user's.
                alert.spot = self.conditions.spots(alert)[0].name
        self.alerts.append(alert)
        self.save()
        return alert

    def turn_down(self, alert: ActivityAlert, *, forever: bool) -> None:
        """The user refused it: never again, or not again today."""
        if forever:
            alert.off = True
        else:
            alert.muted_until = self._local().date().isoformat()
        self.save()

    def resume(self, alert: ActivityAlert) -> None:
        alert.off, alert.muted_until = False, ""
        self.save()

    # --- The watch -----------------------------------------------------------------

    def _name(self, alert: ActivityAlert) -> str:
        return self._s.activity_names.get(alert.activity, alert.activity).capitalize()

    def _day(self, day: str, today: date) -> str:
        s, then = self._s, date.fromisoformat(day)
        if then == today:
            return s.activity_alert_today
        if then == today + timedelta(days=1):
            return s.activity_alert_tomorrow
        return s.activity_alert_weekdays[then.weekday()]

    def _facts(self, window: Window) -> str:
        s, v = self._s, window.values
        if window.kind == "wind":
            parts = [s.activity_alert_wind.format(knots=v["knots"], direction=v["direction"])]
            if v.get("gusts") is not None:
                parts.append(s.activity_alert_gusts.format(gusts=v["gusts"]))
        elif window.kind == "surf":
            parts = [s.activity_alert_waves.format(waves=v["waves"], period=v["period"])]
        else:
            parts = []
            if v.get("temp"):
                low, high = v["temp"]
                parts.append(s.activity_alert_temperature.format(low=low, high=high))
            if v.get("rain") is not None:
                parts.append(s.activity_alert_rain.format(rain=v["rain"]))
            if v.get("wind") is not None:
                parts.append(s.activity_alert_wind_kmh.format(wind=v["wind"]))
        return ", ".join(parts)

    def _forecast_words(self, alert: ActivityAlert, window: Window, today: date) -> str:
        return self._s.activity_alert_forecast.format(
            icon=ICONS[window.kind],
            activity=self._name(alert),
            where=window.where or self._s.activity_alert_home,
            day=self._day(window.day, today),
            start=window.start,
            end=window.end,
            facts=self._facts(window),
            id=alert.id,
        )

    def _now_words(self, alert: ActivityAlert, reading: dict[str, Any]) -> str:
        s = self._s
        station = str(reading.get("station") or reading.get("source") or "")
        if reading.get("distance_km") is not None:
            station += f" ({s.activity_alert_distance.format(km=reading['distance_km'])})"
        window = Window(
            "",
            "",
            "",
            reading["spot"],
            "wind",
            {
                "knots": reading.get("avg_knots"),
                "gusts": reading.get("gust_knots"),
                "direction": reading.get("direction") or "",
            },
        )
        return " ".join(
            s.activity_alert_now.format(
                icon=ICONS["wind"],
                activity=self._name(alert),
                where=reading["spot"],
                facts=self._facts(window),
                time=reading.get("observed_at"),
                station=station,
                id=alert.id,
            ).split()
        )

    async def _look(self, alert: ActivityAlert, local: datetime) -> tuple[str, str] | None:
        """The day to announce and the words for it, when something fits."""
        today = local.date()
        if alert.when == "now":
            if today.isoformat() in alert.sent_for:
                return None
            readings = await self.conditions.measured(alert, self._clock())
            if not readings:
                return None
            best = max(readings, key=lambda r: r.get("avg_knots") or 0)
            return today.isoformat(), self._now_words(alert, best)
        last = self._looked.get(alert.id)
        if last is not None and (local - last).total_seconds() < FORECAST_SECONDS:
            return None
        self._looked[alert.id] = local
        naive = local.replace(tzinfo=None)
        horizon = naive + timedelta(hours=alert.lead_hours)
        coming = [
            w
            for w in await self.conditions.windows(alert)
            if w.hours >= alert.min_hours
            and w.day not in alert.sent_for
            and datetime.fromisoformat(f"{w.day}T{w.start}") <= horizon
        ]
        if not coming:
            return None
        first_day = min(w.day for w in coming)
        best = max((w for w in coming if w.day == first_day), key=lambda w: w.hours)
        return first_day, self._forecast_words(alert, best, today)

    async def check(self, send: Send) -> list[int]:
        """One look at every alert due; the ids of those sent."""
        if self.quiet_now():
            return []
        local = self._local()
        today = local.date().isoformat()
        sent = []
        for alert in self.alerts:
            if alert.off or (alert.muted_until and alert.muted_until >= today):
                continue
            try:
                found = await self._look(alert, local)
            except Exception as exc:
                log.warning("Activity alert %s: no look (%s)", alert.id, exc)
                continue
            if found is None:
                continue
            day, words = found
            if not await send(words):
                # Nothing took it: the forecast is read again on the next round.
                self._looked.pop(alert.id, None)
                continue
            alert.sent_for = sorted({*alert.sent_for, day})[-KEEP_SENT_DAYS:]
            self.save()
            sent.append(alert.id)
        return sent

    async def watch(self, send: Send) -> None:
        while True:
            try:
                await self.check(send)
            except Exception:
                log.exception("Activity alerts check failed")
            await asyncio.sleep(WATCH_SECONDS)


def build_activity_alert_tools(alerts: ActivityAlerts | None, *, language: str) -> list[Tool]:
    if alerts is None:
        return []
    s = strings(language)
    meteo = alerts.conditions.meteo

    async def set_activity_alert(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        given: dict[str, Any] = {
            "spot": str(args.get("spot") or "").strip() or None,
            "when": args.get("when"),
            "lead_hours": _int(args.get("lead_hours")),
            "min_hours": _int(args.get("min_hours")),
            "min_knots": _float(args.get("min_knots")),
            "gust_knots": _float(args.get("gust_knots")),
            "good_only": bool(args.get("good_only")) or None,
        }
        found, _unknown = match_activities(str(args.get("activity") or ""))
        place = str(args.get("place") or "").strip()
        if place and len(found) == 1 and CATALOG[found[0]].kind == "weather":
            location = await meteo.geocode(place, language)
            given |= {
                "place": str(location["name"]),
                "latitude": location["latitude"],
                "longitude": location["longitude"],
            }
        alert = alerts.add(str(args.get("activity") or ""), **given)
        return {"created": alert.view()}

    async def list_activity_alerts(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return {"alerts": [a.view() for a in alerts.alerts]}

    async def mute_activity_alert(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        alert = alerts.get(args.get("id"))
        alerts.turn_down(alert, forever=bool(args.get("forever")))
        return {"now": alert.view()}

    async def resume_activity_alert(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        alert = alerts.get(args.get("id"))
        alerts.resume(alert)
        return {"now": alert.view()}

    return [
        Tool(
            name="set_activity_alert",
            description=(
                "Tell the user by yourself when conditions are coming up good for one of "
                'their outdoor activities ("tell me when it\'s good for surf", "warn me a day '
                'ahead of a dry window for cycling"): the forecast is read every hour and '
                "one message is sent per good day, up to lead_hours before the window. Wind "
                "sports and surf are judged per spot of the user's (omit spot for any in "
                "season), other activities on the weather at home or in a place. For a "
                'wind sport, when "now" watches the wind measured at the spot instead. '
                f"Activities: {', '.join(CATALOG)}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "activity": {"type": "string"},
                    "spot": {"type": "string", "description": "Wind sports and surf."},
                    "place": {"type": "string", "description": "Other activities; omit: home."},
                    "when": {"type": "string", "enum": ["forecast", "now"]},
                    "lead_hours": {
                        "type": "integer",
                        "description": f"How early, 1 to {MAX_LEAD_HOURS}; default "
                        f"{alerts.lead_hours}.",
                    },
                    "min_hours": {
                        "type": "integer",
                        "description": "Shortest window worth a message; default 2.",
                    },
                    "min_knots": {
                        "type": "number",
                        "description": "Wind sports: instead of the spot's own minimum.",
                    },
                    "gust_knots": {
                        "type": "number",
                        "description": 'With when "now": gusts at least.',
                    },
                    "good_only": {
                        "type": "boolean",
                        "description": "Surf: skip the windows rated fair.",
                    },
                },
                "required": ["activity"],
            },
            handler=set_activity_alert,
            needs_clean_turn=True,
            describe=describe_whole(s.activity_alert_label),
        ),
        Tool(
            name="list_activity_alerts",
            description="The activity alerts set, with their number, what they watch and state.",
            input_schema={"type": "object", "properties": {}},
            handler=list_activity_alerts,
        ),
        Tool(
            name="mute_activity_alert",
            description=(
                "The user turns an activity alert down: until tomorrow, or for good with "
                'forever ("stop telling me about the surf"). Never send it again then.'
            ),
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}, "forever": {"type": "boolean"}},
                "required": ["id"],
            },
            handler=mute_activity_alert,
        ),
        Tool(
            name="resume_activity_alert",
            description="Bring back an activity alert the user had turned down or paused.",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
            handler=resume_activity_alert,
            needs_clean_turn=True,
            describe=describe_whole(s.activity_alert_resume_label),
        ),
    ]


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ToolError("Hours are whole numbers.") from exc


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ToolError("Thresholds are numbers of knots.") from exc
