# Home devices, without Home Assistant

The agent can drive some devices directly, each one on when its settings are filled in.
Home Assistant stays the way to reach everything else, and the two work side by side: a
brand that is not below (another lock, another speaker, a thermostat) goes through Home
Assistant.

| Device | Reached | Settings | Tools |
|---|---|---|---|
| Philips Hue (bridge) | home network | `HUE_BRIDGE_URL`, `HUE_APP_KEY` | `list_hue_lights`, `set_hue_lights`, `activate_hue_scene` |
| Devialet Phantom speaker | home network | `DEVIALET_URL`, `DEVIALET_MAX_VOLUME` | `get_speaker_state`, `get_speaker_track`, `control_speaker` |
| Google TV / Android TV (TV, projector, box) | home network | `ANDROID_TV_HOST`, `ANDROID_TV_CLIENT_CERT`, `ANDROID_TV_CLIENT_KEY`; `ANDROID_TV_NAVIGATOR` | `get_tv_state`, `control_tv`; `navigate_tv` |
| Apple TV, HomePods | home network | `APPLE_DEVICES` | `get_apple_media_state`, `control_apple_media` |
| Nuki smart lock | Nuki's cloud | `NUKI_API_TOKEN` | `get_lock_state`, `get_lock_activity`, `lock_door`, `unlock_door` |
| Netatmo cameras, weather station, thermostat | Netatmo's cloud | `NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET`, `NETATMO_REFRESH_TOKEN` | `get_home_security_status`, `get_camera_events`, `get_netatmo_measures`, `set_camera_monitoring` |
| PetKit feeder, litter box | PetKit's cloud | `PETKIT_USERNAME`, `PETKIT_PASSWORD`, `PETKIT_REGION` | `get_pet_devices_status`, `feed_pet_now`, `clean_litter_box_now` |

`python scripts/setup.py` asks about the lock and the pet devices and names the script to
run for the others.

## Optional libraries

Three devices rest on unofficial libraries, pinned to a version known to work and kept
out of the default install:

| Extra | For | Install |
|---|---|---|
| `android-tv` | Google TV / Android TV | `pip install -e ".[android-tv]"` |
| `apple` | Apple TV and HomePods | `pip install -e ".[apple]"` |
| `petkit` | PetKit | `pip install -e ".[petkit]"` |

`devices` is the three together. With Docker, set `EXTRAS=documents,devices` (or
`EXTRAS=documents,petkit,apple`; `documents` is the image's default, see
[documents.md](documents.md)) in `.env` and run `docker compose build`. When a device's settings
are filled in but its library is missing, the device stays off and the log says which
extra to install; everything else works as before. These libraries follow what the
vendors' own apps do, which can change without notice: a device may stop answering until
the pinned version is raised.

## Reaching the home network

Devices marked "home network" are spoken to by their local address:

- **The agent runs at home**: nothing to do. Give each device a fixed address in your
  router (a DHCP reservation), since the agent finds it by address, not by discovery.
- **The agent runs elsewhere** (a VPS): it needs a way into the home network. With
  Tailscale, make one device at home a
  [subnet router](https://tailscale.com/kb/1019/subnets) for your home network (a
  computer, a NAS, a Raspberry Pi, an Apple TV), approve the route in the admin console,
  add `compose/tailscale.yml` and set `TS_EXTRA_ARGS=--accept-routes` in `.env`. The agent
  then goes through the sidecar's proxy (`HOME_PROXY_URL`, which defaults to
  `HA_PROXY_URL`). Any other VPN works the same way as long as it offers an HTTP proxy
  that accepts `CONNECT` (WireGuard with a small proxy next to it, for instance): put its
  address in `HOME_PROXY_URL`.

Only TCP goes through such a proxy: discovery (mDNS), Bluetooth and Thread stay at home.
That is why the pairing scripts below run once on a computer **at home**, and why the
Apple devices are described by a profile instead of being discovered.

Devices marked with a cloud are reached over the internet from wherever the agent runs.

## Philips Hue

`HUE_BRIDGE_URL` is the bridge's address (e.g. `http://192.168.1.20`; the Hue app shows
it under Settings > My Hue system). To get `HUE_APP_KEY`, press the round button on the
bridge, then within 30 seconds, from a computer at home:

```bash
curl -s -X POST -d '{"devicetype":"home-agent#server"}' http://<bridge address>/api
```

The answer holds `"username"`: that is the key.

Rooms, lights and scenes are found by the name you say, accents and case aside; a name
shared by a room and a light means the room. On/off, brightness, white temperature and
colour work for a room, a light or the whole home.

## Devialet Phantom

`DEVIALET_URL` is the speaker's address (e.g. `http://192.168.1.51`): its IP Control API
has no login. `DEVIALET_MAX_VOLUME` (70 by default) caps every volume the agent sets, so a
misheard request can't blast the room. Volume, play, pause, mute, next, a physical input
and standby; streaming (AirPlay, Spotify Connect) starts from your phone.

## Google TV / Android TV

Any device that Google's own TV remote app can drive: a television, a projector, a
streaming box or stick. The agent speaks the same protocol (Android TV Remote v2, TLS on
port 6466). Pair it once, from a computer at home:

```bash
pip install -e ".[android-tv]"
python scripts/android_tv_pair.py 192.168.1.60
```

The device shows a code; type it. The script writes `ANDROID_TV_HOST`,
`ANDROID_TV_CLIENT_CERT` and `ANDROID_TV_CLIENT_KEY` to a file: add its lines to `.env`,
restart the agent, delete the file. The device lists "Home agent" among its paired remotes
(Settings > Remotes & accessories), where you can remove it.

- `ANDROID_TV_NAME` is what you call it (`TV`, `projector`): the agent uses that word.
- `ANDROID_TV_MAX_VOLUME` (70) caps every volume the agent sets.
- **Apps**: Netflix, YouTube, Prime Video and Disney+ are known. Add yours, or drop one,
  with `ANDROID_TV_APPS`, a JSON object:

  ```
  ANDROID_TV_APPS={"my tv app": {"package": "com.example.tv", "link": "mytv://"}, "disney+": null}
  ```

  `package` is the app's id (the end of its Play Store address), `link` a deep link that
  opens it. Google TV no longer opens an app from its id alone, so an app without a deep
  link (`"link": null`) is opened from its Play Store page, whose "Open" button has focus.
  Only list such an app when it is installed: on a missing app that button installs it.
- **Live TV**: when one of the apps takes a channel number typed on the remote, name it
  in `ANDROID_TV_CHANNEL_APP` and the agent can put a channel on by number. Give the
  numbers names with `ANDROID_TV_CHANNELS={"news one": 1, "sports two": 12}`. Left empty,
  there is no channel control.
- **Waking from standby**: in standby the remote port closes on many devices, while Google
  Cast keeps listening if the device's energy settings let the network stay on (often
  called "increased" or "optimised" energy mode). The agent then wakes it by launching
  Cast's default receiver, as a phone casting to it would. A device with the network off
  in standby can't be turned on from here.

What works: on/off, opening an app, a live channel, volume, mute, play/pause/stop,
next/previous, home, back. Reaching something *inside* an app (a film, an episode) is
the navigator's job, below; it is off until you set it up.

### Finding and starting something inside an app (`navigate_tv`)

"Put on the latest episode of that series", "start the replay of last night's match":
the remote protocol alone can't do that, since it presses keys without seeing anything.
With `ANDROID_TV_NAVIGATOR=true` the agent gets `navigate_tv`: it hands the goal to a
helper model that looks at the screen (a screenshot and the names the app gives its
elements, both read over ADB), presses the remote's keys, types in search fields, and
stops when the goal is reached or can't be. It takes a minute or two, and at most
`ANDROID_TV_NAVIGATOR_ACTIONS` (25) actions.

Setting it up, once:

1. **The `adb` program where the agent runs.** With Docker: `SYSTEM_PACKAGES=adb` in
   `.env`, then `docker compose build`. Without: your system's package (`adb` or
   `android-tools-adb`).
2. **ADB on the device.** Settings > System > About, press *Build* seven times; then
   Developer options > Wireless debugging on. Then, from where the agent runs:

   ```bash
   python scripts/android_tv_adb.py pair <pairing port> <code>   # "Pair device with pairing code"
   python scripts/android_tv_adb.py tcpip <port>                  # the port under "IP address & Port"
   python scripts/android_tv_adb.py check                         # prints the app in front
   ```

   In Docker: `docker compose exec -T agent python - check < scripts/android_tv_adb.py`.
   The key stays in `DATA_DIR/android`. Android closes the port when the device
   *restarts* (standby is not a restart): run `tcpip` again then; the pairing stays.
3. `ANDROID_TV_NAVIGATOR=true`, and restart the agent.

Which model drives it:

- With a Claude subscription (`ENGINE=subscription`, [subscription.md](subscription.md))
  it runs on the plan, and on the API only when the plan can't take it (a usage limit).
- Otherwise it runs on your provider's API, whichever it is: the screenshots go through
  the same client as the rest, so an OpenAI-compatible server works as long as the
  model **reads pictures and calls tools**. A text-only model would press keys blind.
- `ANDROID_TV_NAVIGATOR_MODEL` picks the model. Empty, it is a small Claude model with
  Anthropic (fast and cheap is what this job wants: one request per key press or so),
  and `LLM_MODEL` with any other provider.
- If `adb` is missing, or there is neither a subscription nor an API, `navigate_tv`
  stays off and the log says why at startup.

What it may and may not do: the helper only has the remote's keys, text entry, opening
one of the device's apps, and looking. It is told never to buy, rent or sign in, and to
stop when a screen asks for that; that is an instruction to a model, not a lock, so keep
purchases behind a PIN on the device, as you would for a child. What apps show is text
written by others: the helper's report taints the request like a web page would, and
`navigate_tv` itself is an action, so after a read it waits for your yes. A blank
screenshot in a video app means a protected video is playing, which the helper takes
as success when playing was the goal.

What was run for real: this helper was used on one Google TV projector, on the Claude
subscription with the Anthropic API as fallback, before it was published here. Other
providers' models go through the same loop but were not tried on a real screen.

## Apple TV and HomePods

`get_apple_media_state` says whether the Apple TV and each HomePod is on, what plays and
the Apple TV's app in front; `control_apple_media` pauses or plays, or turns the Apple TV
off (sleep). A HomePod can't be turned off, only paused, and one used as the Apple TV's
speakers shows the TV's state. It goes through [pyatv](https://pyatv.dev).

On a computer at home (on macOS the terminal needs Local Network access):

```bash
pip install -e ".[apple]"
python scripts/apple_devices.py profile 192.168.1.61 192.168.1.62   # every device's address
python scripts/apple_devices.py pair 192.168.1.61 Companion         # the Apple TV only
python scripts/apple_devices.py pair 192.168.1.61 AirPlay
```

`profile` prints the value of `APPLE_DEVICES` (one line of JSON: put it in `.env` between
single quotes); each `pair` shows a PIN on the TV, typed at the prompt, and prints the
value of `APPLE_TV_COMPANION_CREDENTIALS`, then `APPLE_TV_AIRPLAY_CREDENTIALS`. HomePods
need no pairing. Run `profile` again after a device changes address.

## Nuki smart lock

The agent drives the lock through the Nuki Web API, so the lock must be reachable from
Nuki's cloud (a Nuki Bridge, or a lock with Wi-Fi or Thread built in) with Nuki Web turned
on in the Nuki app. Sign in at web.nuki.io, open API, generate a token with the smartlock,
action and log permissions, and put it in `NUKI_API_TOKEN`. Some accounts need Nuki's paid
plan for the Web API; the agent says so when Nuki refuses.

- `get_lock_state`: locked or not, the door sensor, the battery.
- `get_lock_activity`: who came and went, and how.
- `lock_door`: locks, and waits up to 15 seconds for the lock to confirm.
- `unlock_door`: never opens on the model's word alone. Without unlock codes it appears
  as a numbered line under the reply and runs on your yes in your next message. With
  them (below), it asks for one.

Only smart locks are driven; an Opener or a Box is listed but never acted on.

### Unlock codes

So that opening the door asks for proof only you have, and no injected message can open
it: either the 6-digit code of an authenticator app, or the letters of a secret phrase at
3 positions the agent draws at random (valid 5 minutes, once).

```bash
pip install "qrcode[pil]"
python scripts/unlock_codes_setup.py --totp --phrase
```

It opens a QR code to scan with the authenticator app, checks a code, asks the phrase
twice without showing it (12 letters or more; positions count letters and digits only, no
spaces, accents or punctuation), and writes `UNLOCK_TOTP_SECRET` and `UNLOCK_PHRASE` to a
file to add to `.env`. Either one alone is fine (`--totp` or `--phrase`). Each code works
once; 5 wrong answers in a row block opening for 15 minutes. With a phrase, every answer
shows 3 of its letters in the conversation: change it now and then (`--phrase` alone).

## Netatmo (cameras, weather station, thermostat)

On dev.netatmo.com, sign in with your Netatmo account, open My apps > Create, and set the
redirect URI to `http://localhost:8586` (the script prints it; `--port` changes it). On a
computer with a browser, run `python scripts/netatmo_login.py` with the app's client id
and secret, sign in on the page it opens and accept. It writes three lines to add to
`.env`. Netatmo may renew the refresh token; the agent keeps the current one in
`DATA_DIR/netatmo.json`.

- `get_home_security_status`: the cameras, on or off, and the people they recognise, at
  home or away.
- `get_camera_events`: who was seen, movement, a camera going offline.
- `get_netatmo_measures`: the weather station's modules and each room's thermostat.
- `set_camera_monitoring`: turns a camera's monitoring on or off, always after your yes.

The agent reads events, not pictures or video.

## PetKit (feeder and litter box)

The agent reads the feeder and the litter box through PetKit's cloud with the unofficial
`pypetkitapi` library: food level, today's meals and the next one, litter level, waste
bin, the pet's visits and the last cleaning. It can give a meal of 1 to 3 portions or
start a cleaning cycle.

PetKit allows one session per account, so logging in from the agent logs your phone out:
create a second PetKit account, share the feeder and litter box with it from the app, and
put it in `PETKIT_USERNAME` / `PETKIT_PASSWORD`. `PETKIT_REGION` is the account's country
code (`US`, `DE`, `FR`...; `HOME_COUNTRY` when empty). An account created with an emailed
code has no password: set one in the app first.

With `NTFY_URL` set, the supplies are looked at in the daytime every
`PETKIT_CHECK_MINUTES` (180) and a push is sent only when something needs doing: the
litter under `PETKIT_LITTER_ALERT_PERCENT` (50) and the food under
`PETKIT_FOOD_ALERT_PERCENT` (45, or the feeder's own low flag when it gives no level),
once each, again only after the level came back up; the waste bin full at once and then
every `PETKIT_BIN_REMIND_MINUTES` (120) until the box says it is empty. Nothing is read or
sent between `PETKIT_QUIET_START` and `PETKIT_QUIET_END` (22 to 8 h).
`PETKIT_ALERTS=false` turns the watch off.

## Leaving home

`POST /v1/departure` tells the agent you just left. It reads the devices that are set up,
whichever they are, and answers with what it found, every word written by the code:

```
🏠 You left, but:
🔓 "Front door" is not locked.
💡 2 light(s) on: Kitchen, Hall.

Waiting for your go ("yes", or "yes except 2"):
1. Lock "Front door"
2. Turn off the lights
```

`reply` is empty when everything is locked and off. Nothing is locked or turned off by
the check itself: each fix waits, like any other write, for your yes in the same
conversation (`conversation_id`, `default` unless you send one), kept for
`CHAT_CONVERSATION_TTL_SECONDS` (12 hours).
`"push": true` also sends the message through ntfy.

What is checked:

- directly driven devices: a Nuki lock (not locked; a door left open is said but not
  offered, as the bolt would close on nothing; a lock that can't be read is always said),
  Hue lights on, the Devialet speaker playing, the Android TV device on, an Apple TV on, a
  HomePod playing;
- Home Assistant, for each kind no direct device covers: lights on (`light`), media
  players playing (`media_player`), locks unlocked (`lock`). A Home Assistant lock is said
  but not offered: locks are a sensitive domain there, so ask for it in your own words
  and confirm.

Who calls it is up to you:

- an iPhone: `python scripts/build_shortcuts.py ... --departure` builds a **Leaving home**
  Shortcut, run by a personal automation when you leave
  ([siri-shortcut.md](siri-shortcut.md#leaving-home));
- Home Assistant: an automation on your `person` or `zone` entity calling the endpoint
  with a `rest_command`, with `"push": true`;
- any geofencing app that can send an HTTP request.

Not done here: noticing by itself that you left (from your phone leaving the Wi-Fi, for
instance). `DEPARTURE_CHECK=false` turns the endpoint off.

## Safety

- Like Home Assistant control, the tools that act (lights, speaker, TV, lock, camera
  monitoring, feeding, cleaning) don't run for the rest of a request once it has read
  emails, the calendar or other content written by someone else: they wait for your go
  instead, so a message can't switch your lights or lock you out.
- Opening a door needs your code or your yes, every time (see Nuki above). Turning a
  camera's monitoring off always needs your yes.
- What these accounts report (who came in, what the cameras saw, the pet's day) is your
  own data: after reading it, a web search in the same request waits for your go.
- The track a speaker plays is written by whoever streams: reading it counts as reading
  someone else's text.
