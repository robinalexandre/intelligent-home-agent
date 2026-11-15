# Outdoor activities

The agent knows what you do outside and looks at the forecast the way that activity
needs: wind per spot for a wind sport, waves per beach for surf, a dry and mild window
for a ride, a run or a hike. Nothing here assumes a sport: it all follows the activities
you list. Forecasts come from [Open-Meteo](https://open-meteo.com), worldwide, without an
account or a key.

## Activities

`OUTDOOR_ACTIVITIES` is a comma-separated list; the setup script fills it from free text
("I bike and go out a lot" gives `cycling,outdoors`).

| Activity | Judged | On | Tool |
|---|---|---|---|
| surf | per beach (`SURF_SPOTS`) | wave height within the beach's range, period of 8 s or more, wind not onshore; tide times given | `get_surf_forecast` |
| wingfoil, kitesurf, windsurf, sailing | per spot (`WIND_SPOTS`) | wind at least the spot's minimum, from one of its directions; gusts given | `get_wind_forecast` |
| cycling, running, hiking, skiing, climbing, golf, outdoors | at home or in a named place | the activity's own limits on rain, wind, temperature and visibility; UV and snowfall when they matter | `get_outdoor_forecast` |

Only daylight hours count. A wind sport without a spot falls back to the general weather.
The limits of each activity are in `activities.py`; adding one is a few lines there (see
[AGENTS.md](../AGENTS.md)).

## Spots

A spot is yours: a name, where it is, and what makes it work. JSON on one line in `.env`:

```
WIND_SPOTS=[{"name":"North Beach","latitude":0.0,"longitude":0.0,"directions":["W","SW","NW"],"min_knots":13}]
SURF_SPOTS=[{"name":"South Bay","latitude":0.0,"longitude":0.0,"facing":"W","min_wave_m":0.5,"max_wave_m":2.5}]
```

- `directions`: the compass points the spot works with (`N`, `NNE`, `NE`... 16 points);
  empty or absent means any. `min_knots`: the least wind worth going for (12 by default).
- `facing`: the compass point the beach looks at, towards the sea; wind from behind it is
  offshore. `min_wave_m`, `max_wave_m`: the waves you want.
- `active_from`, `active_until` (dates, `2027-06-01`): a spot you are only near part of
  the year, or from the day you move. Outside those days it is left out of "how is it
  this weekend?", and still answers when you name it.
- A spot is found by part of its name ("the pier" for "The Pier, North Beach") when only
  one spot holds those words.

`WIND_MODEL` picks the Open-Meteo model for the wind: `best_match` works everywhere, a
regional model can be sharper near a coast (`icon_seamless`, `gfs_seamless`,
`meteofrance_seamless`...).

**Tides** are an estimate from Open-Meteo's sea-level model, good to about half an hour:
enough to know whether the tide is rising for a session, not a harbour's tide table. No
tide table is built in: they are published per country, each with its own terms.

**A home that moves.** `HOME_LATITUDE` and `HOME_LONGITUDE` say where home is. When you
move, or spend a season elsewhere, add the next homes rather than editing them on the day:

```
HOME_MOVES=[{"active_from":"2027-03-01","latitude":0.0,"longitude":0.0}]
```

The weather "at home", the weather-window activities and their alerts follow from that
day on.

## Wind measured now

A forecast says what should blow; an anemometer says what does. With a source set,
`get_live_wind` gives the mean wind, the gusts and the direction measured near each spot
(wind and surf spots; near home when you have no spot), with the station, its distance
and the time of the reading. Nothing is on by default:

```
LIVE_WIND_SOURCES=openwindmap,metar
LIVE_WIND_RADIUS_KM=20
```

| Source | What | Coverage | Needs |
|---|---|---|---|
| `openwindmap` | [OpenWindMap](https://www.openwindmap.org), the Pioupiou community beacons, open data, a reading every few minutes | Any country, but only where someone installed a beacon: dense in France and around the Alps, sparse elsewhere. Look at the map for your spots. | nothing |
| `metar` | Airports' routine weather reports, from [aviationweather.gov](https://aviationweather.gov/data/api/) | Worldwide, wherever there is an airport or airfield that reports. One reading every 30 to 60 minutes, and the airport may be inland. | nothing |
| `meteofrance` | Météo-France's official stations, a reading every 6 minutes | France only | `METEOFRANCE_APPLICATION_ID`: a free account on [portail-api.meteofrance.fr](https://portail-api.meteofrance.fr), subscribed to the observation packages (DPPaquetObs); the application id is what the portal gives to request tokens |

Each source gives its nearest station with a recent reading within `LIVE_WIND_RADIUS_KM`
of the spot (45 minutes old at most, 90 for an airport). A source that has nothing near a
spot says so, and the others still answer. Only numbers, compass points and station
identifiers reach the model: a beacon's name, written by its owner, never does.

What is not here, and why:

- **Sites that show their readings to subscribers only** (several national wind-sport
  sites do). They cover one country, need your own paid account and a logged-in browser
  session, and their pages are not an interface anyone promised to keep. Not included.
- **Other national weather services.** Many publish their stations' observations (some
  free with a key, some not at all). Each is a class of about a hundred lines beside
  `MeteoFrance` in `tools/live_wind.py`, named in `LIVE_WIND_SOURCES`; airport reports
  already cover every country in the meantime.

## Alerts

"Tell me when it's good for wingfoil at North Beach." "Warn me the day before a dry window
for a ride." An alert is one activity, judged exactly as its forecast tool judges it:

- a wind sport, at one of your spots or at any of them in season: a daylight window with
  enough wind from a good direction (you may give the alert its own minimum in knots);
- surf, at one of your beaches or any of them: a window rated fair or good (or good only);
- any other activity, at home or in a place you name: a window inside the activity's
  limits.

The code watches, not the model: every hour it reads the forecast and, when a window of
at least two hours (`min_hours`) starts within the alert's lead time, it sends **one
message per alert and per good day**, written by the code from the numbers:

```
🌬️ Wingfoil · North Beach, tomorrow 14:00–18:00: 16 kn W, gusts 22 kn. (alert n°1)
```

When nothing fits, nothing is sent. Nothing is sent during the quiet hours either
(22:00 to 8:00 by default); a window found at night is announced in the morning.

For a wind sport, an alert can instead watch **the wind measured at the spot**
("tell me when it actually blows at North Beach"): it needs a source of measured wind
(above), is looked at every ten minutes and speaks at most once a day.

**Where they go.** Alerts leave through the same channels as the tasks' reports
([tasks.md](tasks.md)): a push through ntfy today (`NTFY_URL`), a chat when one is
configured. Without any, there are no alerts and the agent does not offer them. Like the
reports, the last ones can also be fetched by a client: `GET /v1/tasks/reports`.

**Setting them.**

- Ask: "tell me when it's good for surf", "alert me 48 hours ahead of a good day for
  hiking in Hilltown". After a request that read a mail or a web page, the alert waits
  for your yes like any write.
- Or in `.env` (the setup script asks, when a push is configured):

  ```
  ACTIVITY_ALERTS=wingfoil,cycling     # one alert per activity, at any spot / at home
  ACTIVITY_ALERTS_LEAD_HOURS=24        # how early you are told, at most (1 to 72)
  ACTIVITY_ALERTS_QUIET_START=22
  ACTIVITY_ALERTS_QUIET_END=8
  ```

  An activity that cannot be judged (surf without a beach, a wind sport without a spot)
  gets no alert, and the log says why.

**Turning one down.** A push has no button, so say it: "not today" pauses the alert
until tomorrow, "stop with the surf alerts" turns it off for good (an alert from `.env`
stays off too, until you ask for it back). "Which alerts do I have?" lists them.
Turning one down works in any request; bringing one back waits for your yes after a read.

The alerts are kept in `DATA_DIR/activity-alerts.json`.

A generous activity fires often: `outdoors` is happy with any mild dry afternoon, so its
alert is close to a daily message in summer. Prefer alerts for what is rare where you
live.

## What could not be checked

The tests replay recorded shapes of each service's answers; none of them was reachable
from where this was written. Open-Meteo is used as before. The OpenWindMap and
Météo-France readers were ported from code written against the real services; the
airport-report reader is new, written from what is known of the service's interface, and
has not met the real service yet: check it on your first `get_live_wind`.
