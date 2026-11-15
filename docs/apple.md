# If you use Apple devices: Reminders, Notes and Find My

Optional, and only useful with an iCloud account. Without it the agent still makes
reminders on an iPhone: the Siri Shortcut creates them from the agent's `actions`
([siri-shortcut.md](siri-shortcut.md)) and sends the open ones with the briefing. This page
is for when you want more: the agent reading and changing your reminders by itself, from
any client (a push, a chat, a scheduled briefing), reading your notes, and finding your
devices.

Other Apple things live elsewhere: the calendar, contacts' birthdays and iCloud Mail work
over the standard protocols with an app-specific password
([mail-calendar.md](mail-calendar.md)), iCloud Drive through rclone
([drives.md](drives.md)), an Apple TV or HomePods on the home network
([home-devices.md](home-devices.md)).

## What the agent can do

- **Reminders**: reads the open reminders of every list (or of `ICLOUD_REMINDER_LISTS`),
  adds one (`create_reminder`), changes its title, due date or notes (`update_reminder`),
  and ticks it (`complete_reminder`). A repeating reminder moves to its next date, as on
  the iPhone, when its rule is "every N days, weeks, months or years"; on any other rule
  (set weekdays, the month's last day, an end) the agent can't compute the next date and
  asks you to tick it on a device, so a series never ends by mistake. Without a list, a
  new reminder goes to `ICLOUD_REMINDERS_DEFAULT_LIST`, or else to the list with the most
  open reminders.
- **Notes**: searches the notes by words of their title or text, and reads one whole.
  Locked notes stay closed. It doesn't write notes (see below).
- **Find My**: your devices, and on request your family members' (each with its owner's
  first name), located on demand: online or not, battery and charging, position, whether
  at home (`HOME_LATITUDE`, `HOME_LONGITUDE`), a map link; and the sound that helps find
  one. Lost mode and erasing are not offered.

What it may do by itself:

- Writes (a reminder added, changed or ticked, a device made to ring) run right away when
  your own message asks. After anything was read in the request, the reminders themselves
  included, they wait for your go, shown whole in the list under the reply.
- Reading reminders or notes taints the request like reading an email: reminders can come
  from shared lists, notes from shared folders.
- Where people are is private: after a Find My read, the request opens no new website of
  the agent's own choosing and a web search waits for your go, so no address or query can
  carry a position out. Ringing a device still runs right away.

## How it works, and what it costs you

Apple has no public API for these apps. The agent uses
[pyicloud](https://github.com/timlaing/pyicloud), which speaks the private API of
icloud.com. It is pinned to one version (`pyicloud==2026.10.1` in `pyproject.toml`), since
Apple changes that API without notice and the library follows: expect this feature to
break now and then until the pin is raised.

- The login takes the **Apple ID's own password and a two-factor code**. App-specific
  passwords are refused by these services. You type both once, in the agent's container;
  the agent keeps only the trusted session that login leaves (`DATA_DIR/icloud-web`, on
  the agent's volume, readable by the agent only), never the password. That session is a
  key to the whole iCloud account: treat the machine that runs the agent accordingly.
- **Advanced Data Protection** must be off, and "Access iCloud Data on the Web" on
  (Settings > your name > iCloud on the iPhone).
- Reminders are read whole once from their change feed, then only what changed, so a call
  stays a few requests even with hundreds of reminders.
- A Find My call asks every device, the family's included, for a fresh position and waits
  a few seconds for the answers: it costs the devices a little battery.
- Notes can't be written: the library only reads them, and a note's body is a versioned
  document the agent would have to build itself; a wrong one could break the note on
  every device.

## Setup

`python scripts/setup.py` asks for this under "other devices" (choose `icloud`). By hand:

1. Install the library. With Docker, add `icloud` to `EXTRAS` in `.env` (e.g.
   `EXTRAS=documents,icloud`) and rebuild; without Docker, `pip install ".[icloud]"`.
2. In `.env`: `ICLOUD_APPLE_ID=you@example.com`, then `docker compose up -d --build`.
   `ICLOUD_NOTES=false` or `ICLOUD_FIND_MY=false` leaves one of them out.
3. Log in, inside the running agent (the password is asked, not echoed):
   ```bash
   docker compose exec agent python -m intelligent_home_agent.icloud_web
   ```
   Approve the sign-in on one of your devices and type the six-digit code. It is done only
   once it prints `Logged in: N Reminders lists`. Without Docker, run the same
   `python -m` command as the user that runs the agent, from the folder it runs in, so the
   session lands in its `DATA_DIR`.
4. Ask "what are my reminders?", "where is my phone?".

Until step 3 is done the tools exist and answer that the login is needed.

## When the login expires

How long a session lasts is Apple's decision; count on weeks, not months, and on it ending
early when you change the Apple ID's password or sign devices out.

- When Apple ends it, every tool answers "The iCloud login (Reminders, Notes, Find My) has
  expired", and the agent tells you with the command to run: step 3 again, nothing else.
  Run it in the agent's container: run on the host, it would leave the session where the
  agent never looks.
- **Find My asks for a fresh login of its own** well before the session ends, sometimes
  hours after the login, while Reminders and Notes keep working. Only the password would
  renew it, and the agent doesn't keep it. It says "Find My asks for a fresh iCloud
  login"; the same step 3 brings it back for a while. If you mostly want reminders, set
  `ICLOUD_FIND_MY=false` and ignore this.

Nothing here could be tried against a real iCloud account while this was written for the
public repository: the code is the one that runs in the author's own installation, with its
tests, but your account may differ (a managed Apple ID, a region where iCloud on the web is
served differently). If the login itself fails, check the pyicloud project's issues first.
