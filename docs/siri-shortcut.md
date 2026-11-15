# Siri Shortcut

| | |
|---|---|
| **What it does** | Talk to the assistant with Siri on an iPhone, a HomePod, a Mac or CarPlay; briefings and a "leaving home" check as Shortcuts. |
| **What you need** | The agent reachable from the phone ([setup.md, step 4](setup.md#4-reaching-the-agent)); a Mac to build the Shortcuts in one command, or ten minutes in the Shortcuts app. |
| **Settings** | None on the agent's side beyond `AGENT_API_TOKEN` and `ASSISTANT_NAME`. |
| **Check it works** | "Hey Siri, <assistant name>" → "Yes?" → your request → a spoken reply. |
| **Verified** | Not recorded on this page. `scripts/build_shortcuts.py` needs macOS and could not be run where the other features were ported. |

Siri can't pass free text in its trigger phrase, so the exchange is two steps:
"Hey Siri, <assistant name>" → "Yes?" → your request → spoken reply.

## Ready-made Shortcuts (macOS)

```bash
python scripts/build_shortcuts.py --url https://agent.example.com --token "$AGENT_API_TOKEN" \
  --name "$ASSISTANT_NAME" --language en \
  --quick "Surf check=Is there surf today or tomorrow?" \
  --quick "Wind check=Is there wind for a session?"
open shortcuts/*.shortcut
```

This builds the main Shortcut (named after `--name`, which is also the Siri phrase),
**Briefing**, **Recap** and one Shortcut per `--quick`, signed with your Apple ID; adding
them on the Mac syncs them to the iPhone. `--language fr` gives French prompts and
requests. The files contain the token: don't share them. The sections below explain how to
build them by hand.

- After each reply the API adds `"follow_up": true`, unless you closed the conversation
  ("no thanks", "that's all"), and the Shortcut runs itself with the reply as its input:
  Siri reads it and listens again, without "Hey Siri". Stay silent to stop too.
- Frequent questions get their own Shortcut (`--quick`), so one phrase asks them.
- Briefing and Recap send every open reminder with its due date and `"display":
  "notification"`: the agent writes the reply to be read (digits, no questions) and the
  Shortcut posts it as a notification instead of speaking it, so it can't be missed.

## Build the Shortcut (iPhone, Shortcuts app)

1. New Shortcut, named after your assistant (the name is the Siri phrase; pick something
   easy to say).
2. **Ask for Input**: type *Text*, prompt `Yes?`.
3. **Get Contents of URL**
   - URL: `https://agent.example.com/v1/ask`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <AGENT_API_TOKEN>`, `Content-Type` = `application/json`
   - Request Body: *JSON*
     - `text` = *Provided Input*
     - `conversation_id` = `siri`
4. **Get Dictionary Value**: key `actions` from *Contents of URL*.
5. **Repeat with Each** item in *Dictionary Value* (the agent's phone actions):
   - **Get Dictionary Value** `type` from *Repeat Item*.
   - **If** *Dictionary Value* is `reminder`:
     - **Get Dictionary Value** `title` from *Repeat Item*, then `due` from *Repeat Item*.
     - **If** `due` has any value: **Get Dates from Input** (*due*), then **Add New
       Reminder** with the title, *Remind Me* on, alert at *Dates*.
       **Otherwise**: **Add New Reminder** with the title only.
   - End If.
6. **Get Dictionary Value**: key `reply` from *Contents of URL*.
7. **Show Result**: *Dictionary Value* (Siri reads it aloud).

Plain reminders ("Hey Siri, remind me to...") are faster through Siri itself. The agent's
reminders help when they come out of a conversation ("remind me to answer Sam's email
tonight").

## Morning briefing

A second Shortcut, **Briefing**, sends open reminders along with the request, since the
agent has no direct access to Apple Reminders:

1. **Find Reminders** where *Is Not Completed*, sorted by due date.
2. **Repeat with Each**: **Text**: *Repeat Item* (title) `·` *Due Date*.
3. **Combine Text**: *Repeat Results* with *New Lines*.
4. **Text**: `Open reminders (title · due):` followed by *Combined Text*.
5. **Get Contents of URL** as above, with JSON body:
   - `text` = `Give me the morning briefing`
   - `conversation_id` = `siri`
   - `context` = *Text*
   - `display` = `notification`
6. Same `actions` loop as the main Shortcut, then **Show Notification** with the reply.

What the briefing covers follows `BRIEF_SECTIONS` and `WEATHER_DETAIL`.

To get it without asking: Shortcuts > Automation > **When my alarm is stopped** > Run
*Briefing*, with *Run Immediately* on.

## Evening recap

Duplicate **Briefing** as **Recap** and change `text` to `Give me the evening recap`. It
covers what is still open today and what is coming tomorrow. Automation > **Time of Day**
(e.g. 21:00) > Run *Recap*.

## Leaving home

`python scripts/build_shortcuts.py ... --departure` also builds a **Leaving home**
Shortcut. Then on the iPhone: Shortcuts > Automation > **Leave** > your home address,
**Any Time**, **Run Immediately** (not *Run After Confirmation*) > Run Shortcut *Leaving
home*. By hand, the Shortcut is:

1. **Get Contents of URL**
   - URL: `https://agent.example.com/v1/departure`
   - Method: `POST`
   - Header: `Authorization` = `Bearer <AGENT_API_TOKEN>`
   - Request Body: *JSON*, `conversation_id` = `siri`
2. **Get Dictionary Value**: key `reply` from *Contents of URL*.
3. **If** *Dictionary Value* has any value: **Show Notification** with it.

The agent checks the devices that are set up
([home-devices.md](home-devices.md#leaving-home)). Nothing comes when everything is
locked and off; otherwise the notification lists what was left and what the agent can do
about it. To have it done: "Hey Siri, <assistant name>" → "Yes" (or "Yes except 2"). Both
Shortcuts use the conversation `siri`, which is what links the two; this one is kept for
`CHAT_CONVERSATION_TTL_SECONDS` (12 hours), so the yes can wait until you have parked.

## HomePod

- Home app > Home Settings > your profile > enable **Personal Requests** for the HomePod.
- HomePod runs the Shortcut through your iPhone, so the iPhone must be nearby and online.

## Confirmations

Locks, alarm and covers need a yes in a *second* request:

> "Hey Siri, Assistant" → "Lock the front door" → "Do you want me to lock the front door?"
> "Hey Siri, Assistant" → "Yes" → "Done."

Using the same `conversation_id` (`siri`) is what links the two. Conversations expire after
`CONVERSATION_TTL_SECONDS` (5 minutes by default).

## Security

The token in the Shortcut opens your house to whoever has it: don't share the Shortcut
as-is (sharing includes the header). Rotate `AGENT_API_TOKEN` in `.env` if it leaks.
