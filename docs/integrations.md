# Integrations

The agent is an HTTP API. Every integration below is a client of one of these endpoints,
all behind `Authorization: Bearer <AGENT_API_TOKEN>`:

| Endpoint | Body | Use |
|---|---|---|
| `POST /v1/ask` | `{"text": "...", "conversation_id": "...", "context": "...", "display": "voice" \| "notification" \| "chat", "files": [...]}` | A question or command, with a short memory per `conversation_id`. |
| `POST /v1/briefing` | `{"kind": "morning" \| "evening", "context": "...", "push": false}` | The morning briefing or evening recap, written to be read. `push: true` also sends it through ntfy. Each call starts a fresh conversation. |
| `GET /v1/files/{name}?conversation_id=...` | | A file of the conversation, e.g. one the agent made. |
| `POST /v1/reset?conversation_id=...` | | Forget a conversation now, with its saved copy and files. |

Both `ask` and `briefing` return `reply` and `actions` (things the phone should do, such
as creating a reminder).

`display` says where the reply ends up: `voice` (short spoken sentences, the default),
`notification` (written, no questions) or `chat` (written, a little longer, may ask a
question; more rounds of tools are allowed, see `MAX_CHAT_TOKENS`).

`files` sends up to five files with the request, each as
`{"filename": "bill.pdf", "mimetype": "application/pdf", "data": "<base64>"}`. PDFs and
images are read by models that can (with an OpenAI-compatible server, images need a model
that sees, and a PDF is only kept by name); text, Word, Excel, PowerPoint and OpenDocument
files are read as text by any model. Files stay available by name while the conversation
lasts. When the agent makes or fetches a file for you, the response has
`"files": [{"name": ..., "mimetype": ...}]`: download each with `GET /v1/files/{name}`.

When an action waits for your yes (see "Security model" in the README), the reply ends
with a numbered list written by the code; answer in the same `conversation_id`.

What works today, from most to least direct:

| Channel | Free-form questions | Briefing | Needs |
|---|---|---|---|
| Siri Shortcuts | yes | yes, spoken or as a notification | iPhone / HomePod / Mac |
| Home Assistant Assist | yes | yes | Home Assistant |
| Google Home / Nest | no (see below) | yes, through Home Assistant | Home Assistant + Google Assistant integration |
| Alexa | only with your own skill | yes, through Home Assistant | Home Assistant + Alexa integration |
| ntfy push | no | yes | ntfy app or web app |
| Scheduled task (cron, chat app) | n/a | yes | something that can send an HTTPS request on a schedule |

## Siri Shortcuts

Built by `scripts/build_shortcuts.py` (macOS). See [siri-shortcut.md](siri-shortcut.md).

## Home Assistant Assist

Assist is Home Assistant's own voice pipeline: voice satellites, the companion app (on
Android it can be set as the default assistant), the Wear OS and watchOS apps. A custom
sentence with a wildcard hands any request to the agent, and the reply is spoken back.

`configuration.yaml`:

```yaml
rest_command:
  ask_agent:
    url: "https://agent.example.com/v1/ask"
    method: POST
    headers:
      Authorization: !secret agent_authorization   # "Bearer <AGENT_API_TOKEN>" in secrets.yaml
    content_type: "application/json"
    payload: '{"text": {{ text | tojson }}, "conversation_id": "assist"}'
    timeout: 60

intent_script:
  AskAgent:
    action:
      - action: rest_command.ask_agent
        data:
          text: "{{ query }}"
        response_variable: result
      - stop: ""
        response_variable: result
    speech:
      text: "{{ action_response.content.reply }}"
```

`custom_sentences/en/agent.yaml` (one file per language):

```yaml
language: "en"
intents:
  AskAgent:
    data:
      - sentences:
          - "ask the agent {query}"
lists:
  query:
    wildcard: true
```

"Ask the agent whether I need a jacket tonight" then goes to the agent. Everything else
keeps going to Home Assistant's built-in intents, which are faster for plain device
commands. This needs a recent Home Assistant (rest_command responses and `stop` with a
response variable); check the `rest_command` and `intent_script` docs for your version.

When Home Assistant reaches the agent and the agent reaches Home Assistant, give the agent
a dedicated, non-admin HA user and keep `HA_SENSITIVE_DOMAINS` on: a lock or alarm still
needs a "yes" in a second request.

## Briefing on a speaker, from Home Assistant

An automation calls the briefing endpoint and speaks or pushes the reply:

```yaml
rest_command:
  agent_briefing:
    url: "https://agent.example.com/v1/briefing"
    method: POST
    headers:
      Authorization: !secret agent_authorization
    content_type: "application/json"
    payload: '{"kind": "{{ kind }}"}'
    timeout: 120

script:
  morning_briefing:
    alias: Morning briefing
    sequence:
      - action: rest_command.agent_briefing
        data:
          kind: morning
        response_variable: result
      - action: tts.speak
        target:
          entity_id: tts.home_assistant_cloud   # or your TTS entity
        data:
          media_player_entity_id: media_player.kitchen_speaker
          message: "{{ result.content.reply }}"
```

Trigger the script from an automation (time of day, alarm, presence), from a dashboard
button, or from Google Home or Alexa as below.

## Google Home / Google Assistant

Google ended custom Conversational Actions in 2023, so there is no supported way to send
free-form speech from Google Assistant to your own server. What does work:

- Expose the `morning_briefing` script above through Home Assistant's Google Assistant
  integration (Home Assistant Cloud, or the manual setup). Scripts show up in Google Home
  as scenes.
- Create a Google Home routine with your own starter phrase ("Hey Google, morning
  briefing") whose action activates that scene.
- Home Assistant speaks the reply on a Google Cast / Nest speaker with `tts.speak` (Cast
  speakers are `media_player` entities in Home Assistant), or pushes it with ntfy.

For free-form questions on Android, use the Home Assistant companion app as the default
assistant (Assist, above).

## Alexa

- Same pattern as Google: expose the script through Home Assistant's Alexa integration
  (scripts appear as scenes), trigger it from an Alexa routine with a custom phrase.
- Speaking arbitrary text on an Echo is not possible with the official Alexa integration.
  The community "Alexa Media Player" integration (HACS, unofficial, can break when Amazon
  changes things) adds a notify service that does it; otherwise push the reply with ntfy
  or play it on another speaker.
- Free-form questions need your own Alexa custom skill (Alexa Skills Kit, can stay in
  development mode for your account) with an `AMAZON.SearchQuery` slot, whose endpoint
  forwards the text to `/v1/ask` and returns `reply` as speech. This repository does not
  ship that skill.

## Phone and desktop notifications (ntfy)

[ntfy](https://ntfy.sh) is a simple publish/subscribe push service with apps for Android
and iOS and a web app with desktop notifications. Self-host it or use the public server.

1. Set `NTFY_URL` to a topic URL. On the public server anyone who knows the topic can read
   it: make the name long and random (`scripts/setup.py` suggests one), or self-host with
   access control and set `NTFY_TOKEN`.
2. Subscribe to the same topic in the ntfy app.
3. Call `/v1/briefing` with `"push": true`; the reply comes back in the response too.

## Scheduled tasks (cron, a chat or coding-assistant app)

Anything that can run on a schedule and send an HTTPS request can deliver the briefing:

```bash
curl -s https://agent.example.com/v1/briefing \
  -H "Authorization: Bearer $AGENT_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"kind": "morning", "push": true}'
```

- **cron or a systemd timer** on any machine that can reach the agent.
- **A scheduled task in a chat or coding-assistant app** (for example the scheduled tasks
  in the Claude app, or any agent with a scheduler): ask it to call the endpoint above every
  weekday morning and post the reply in the conversation. The reply then shows up in the
  app on desktop and mobile, and with `"push": true` on ntfy too. The task's environment
  must be able to reach the agent's URL, and it needs the token: prefer the app's secret
  or environment settings over pasting the token in the task's prompt.
- **Home Assistant**, with the `morning_briefing` script and a time trigger.

Briefings sent this way have no phone reminders in `context`; add them if your scheduler
can read them.
