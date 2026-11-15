# Home devices, without Home Assistant

The agent can drive some devices directly, each one on when its settings are filled in.
Home Assistant stays the way to reach everything else, and the two work side by side.

| Device | Settings | Tools |
|---|---|---|
| Philips Hue (bridge) | `HUE_BRIDGE_URL`, `HUE_APP_KEY` | `list_hue_lights`, `set_hue_lights`, `activate_hue_scene` |
| Devialet Phantom speaker | `DEVIALET_URL`, `DEVIALET_MAX_VOLUME` | `get_speaker_state`, `control_speaker` |

More are on the way (locks, thermostats and cameras, media players).

## Reaching the home network

These devices are spoken to on your home network, by their local address:

- **The agent runs at home**: nothing to do. Give each device a fixed address in your
  router (a DHCP reservation), since the agent finds it by address, not by discovery.
- **The agent runs elsewhere** (a VPS): it needs a way into the home network. With
  Tailscale, make one device at home a
  [subnet router](https://tailscale.com/kb/1019/subnets) for your home network (a
  computer, a NAS, an Apple TV), approve the route in the admin console, add
  `compose/tailscale.yml` and set `TS_EXTRA_ARGS=--accept-routes` in `.env`. The agent
  then goes through the sidecar's proxy (`HOME_PROXY_URL`, which defaults to
  `HA_PROXY_URL`).

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

## Safety

Like Home Assistant control, these tools are refused for the rest of a request once it
has read emails, the calendar or other content written by someone else, so a message
can't switch your lights or your speaker.
