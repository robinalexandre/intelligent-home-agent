# Mail, calendar and contacts

| | |
|---|---|
| **What it does** | Reads your mailboxes (IMAP), calendars (CalDAV) and contacts' birthdays (CardDAV). When you allow it: email drafts, flags, filing, and new calendar events. It never sends or deletes an email. |
| **What you need** | A provider that offers these standards with an app password: iCloud, Gmail (mail), Fastmail, Nextcloud, most hosts. Not Outlook.com, Microsoft 365 or Google Calendar, which need OAuth. |
| **Settings** | `MAIL_ACCOUNTS`, `CALDAV_URL` / `CALDAV_USERNAME` / `CALDAV_PASSWORD`, `CALDAV_WRITE`, `CARDDAV_URL`; the wizard's `mail` and `calendar` items. All of them: [settings.md](settings.md#mail-and-calendar). |
| **Check it works** | `get_recent_emails` and `get_calendar_events` are in the startup log's tools; ask "what's on today?", "any email from my bank this week?". |
| **Verified** | Run against the real service: iCloud (mail, calendar, contacts) and Gmail (mail). Not run: Fastmail, Nextcloud and other servers ([below](#what-was-tried)). |

The agent reads your mailboxes over IMAP, your calendars over CalDAV and your contacts'
birthdays over CardDAV. These are open standards: any provider that offers them with a
password works. Reading is the default. Writing (drafts, flags, filing, new events) is a
choice you make per mail account and for the calendar.

`python3 scripts/setup.py` asks for all of this (the `mail` and `calendar` items of its
menu); the settings are described below for
doing it by hand.

## What the agent does

| | Read-only (default) | With writing turned on |
|---|---|---|
| Mail | Lists recent emails from every folder (inbox, your folders, the trash), searches by sender, recipient, subject or text, sent mail included, and reads one email whole. Its attached documents are kept for the conversation. | Saves a draft in your drafts folder, with files attached; changes or removes a draft; flags an email; files an email into one of your own folders, in the same account or another one; creates a folder. |
| Calendar | Events of a day or a range, from all calendars or the ones you name. | Adds an event to one calendar. |
| Contacts | Birthdays, shown as all-day events in the agenda. | Nothing: contacts are never written. |

What it never does, whatever the settings:

- **It never sends an email.** A draft waits in your mail app; you read it and press Send.
- **It never deletes an email** and never puts one in the trash, the junk, the sent or
  the drafts folder. Filing only goes to folders of your own.
- A draft it replaces or removes goes to the account's trash, where you can get it back.
  When the server can't move a single message there, the draft is left where it is.
- Reading never marks an email as read (folders are opened read-only, bodies are fetched
  with `BODY.PEEK`).
- It never changes or deletes a calendar event, and never invites anyone.

### When it asks first

Emails, drafts and calendar events are written by other people, so a request that read
them is *tainted*: the writes below then wait for your go, listed under the reply with
their exact content, and run only when your next message agrees
([Security model](../README.md#security-model)).

| Action | Runs at once | Waits for your go |
|---|---|---|
| Save a new draft | always (nothing leaves: it is a draft) | |
| Flag an email | in a request that read nothing | after any read |
| Add a calendar event | in a request that read nothing | after any read (an invitation, an email) |
| File an email into a folder | in a request that read nothing, when you asked | after any read; never by itself in a scheduled task |
| Create a mail folder | | always |
| Change or remove a draft | | always |

Every saved draft is noted under the reply by the code, with its recipients and files.
When a draft carries files and the request had read content written by others, the
note also asks you to check the recipients before sending.

## Mail

```bash
MAIL_ACCOUNTS=[{"name":"Personal","host":"imap.example.com","username":"me@example.com","password":"app-password","write":true}]
```

One JSON list on one line, one object per account:

| Field | Default | Meaning |
|---|---|---|
| `name` | required | Short name the agent uses for the account ("Personal", "Work"). |
| `host`, `port` | required, `993` | IMAP server, over TLS. |
| `username`, `password` | required | The login. Use a password made for one app, see below. |
| `write` | `false` | `true` lets the agent save drafts, flag and file in this account. |
| `address` | the username | The From address of drafts, when you log in with a user name that is not your address. |
| `mailbox` | `INBOX` | The inbox. |
| `drafts`, `sent`, `trash` | found by itself | The exact folder names, only when the agent picks the wrong ones (see "Special folders"). |

### Providers

Use an *app password*: a separate password for one program, that you can revoke
without changing your own. Most providers require it as soon as two-step login is on,
and refuse your normal password over IMAP.

| Provider | Host | How to get the password | Notes |
|---|---|---|---|
| iCloud Mail | `imap.mail.me.com` | [account.apple.com](https://account.apple.com) > Sign-In and Security > App-Specific Passwords. Two-factor authentication must be on. | The username is your iCloud Mail address, or only the part before the `@` when the full address is refused; in that case set `address` so drafts carry the right From. The same password works for the calendar and the contacts. |
| Gmail | `imap.gmail.com` | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). 2-Step Verification must be on. | Not available on accounts in the Advanced Protection Program, and a Google Workspace administrator can turn app passwords off. |
| Fastmail | `imap.fastmail.com` | Settings > Privacy & Security > App passwords > New app password, with access to mail (and to calendars and contacts to use them too). | |
| Another provider or your own server | its IMAP host | An app password where the provider has them, else the account's password. | Port 993 with TLS only: STARTTLS on port 143 is not supported. |
| Outlook.com, Microsoft 365 | | | Not supported: they only accept OAuth sign-in for IMAP. |

### What differs between providers, and how it is handled

The agent looks at what the server says rather than at who the provider is, so an
unlisted server follows the same rules.

- **Special folders** (drafts, sent, trash, junk). Found by the marks the server puts on
  them (SPECIAL-USE, RFC 6154: Gmail, Fastmail, iCloud and most current servers), then
  by their usual names in English, French, German, Spanish, Italian and Dutch
  ("Sent Messages" and "Deleted Messages" on iCloud, "Brouillons", "Papierkorb"...).
  When yours are named otherwise, name them in the account: `"drafts":"To finish"`,
  `"sent":"Outgoing"`, `"trash":"Bin"`. A folder named there is never a place emails are
  filed into. Without a drafts folder found or named, drafts go to `Drafts`.
- **Folder separator.** Sub-folders are `Parent/Child` on some servers and
  `Parent.Child` on others; the agent reads the separator from the server. On servers
  that keep every folder under the inbox (`INBOX.Invoices`), new folders go there too.
- **Gmail labels.** In Gmail a folder is a label and "All Mail" holds every email. The
  agent recognises Gmail by a capability the server announces (`X-GM-EXT-1`), not by its
  host name. It then reads the inbox, All Mail (minus what is in the inbox, sent,
  drafts, chats, trash and spam) and the trash, so an email shows once. Filing an
  archived email adds the label and leaves it in All Mail. Gmail's own folders
  (`[Gmail]/...`) may have names in your language; they are found by their marks.
- **Filing across accounts.** The email is copied into the other account's folder with
  its date and read state, the copy is checked, and only then the original is archived
  when its server has an archive that keeps everything (Gmail's All Mail). On other
  servers the original stays where it was: nothing is deleted to complete a move.
- **Moving.** With the server's `MOVE` command where it exists; else copy, then remove
  only that email (`UID EXPUNGE`); on a server with neither, the email is copied and the
  original stays.
- **Searching.** One search term with accents at a time (a limit of IMAP's search
  command), and the recipient must be written without accents. A search reads every
  folder and can take a minute on a large mailbox.
- **Connection limits.** Providers cap the simultaneous connections per account (15 on
  Gmail, your phone's included). The agent opens at most four per account and waits a
  little when the server says it is full.

## Calendar

```bash
CALDAV_URL=https://caldav.example.com/dav/
CALDAV_USERNAME=me@example.com
CALDAV_PASSWORD=app-password
CALDAV_CALENDARS=              # only these, by name; empty: all
CALDAV_WRITE=false             # true: the agent may add events
CALDAV_DEFAULT_CALENDAR=       # where new events go, by name
```

| Provider | `CALDAV_URL` | Password |
|---|---|---|
| iCloud | `https://caldav.icloud.com` | An app-specific password, as for iCloud Mail. Username: your Apple Account email. |
| Fastmail | `https://caldav.fastmail.com/dav/` | An app password with access to calendars. |
| Nextcloud | `https://<your host>/remote.php/dav` | Settings > Security > Devices & sessions > Create new app password. |
| Another CalDAV server (Radicale, Baikal, a hosting provider...) | its DAV address | The account's password, or an app password. |
| Google Calendar, Microsoft 365 | | Not supported: their calendars need OAuth sign-in. A [connector](connectors.md) can reach them when the provider has an MCP server. |

New events go to the calendar named in `CALDAV_DEFAULT_CALENDAR`. Without it, to the
one the server gives as your default (iCloud and Fastmail do; the setting of your phone
or the provider's website), and when the server gives none, to the first calendar that
takes events, among those in `CALDAV_CALENDARS` when you listed some. The agent says
which calendar the event went to.

An event has a title, a start, an end (one hour, or one day for an all-day event, when
none is given), a place and notes. Times are in `TIMEZONE`.

## Contacts' birthdays

```bash
CARDDAV_URL=https://contacts.example.com
CARDDAV_USERNAME=              # empty: the CalDAV login
CARDDAV_PASSWORD=
```

Providers show a "Birthdays" calendar built from your contacts, but usually don't
serve it over CalDAV (iCloud doesn't). So the agent reads the birthdays from the
contacts themselves and shows them as all-day events in the agenda, with the age when
the contact has a birth year. It works without a calendar too.

| Provider | `CARDDAV_URL` |
|---|---|
| iCloud | `https://contacts.icloud.com` |
| Fastmail | `https://carddav.fastmail.com/dav/` |
| Nextcloud | `https://<your host>/remote.php/dav` |
| Another CardDAV server | its DAV address |
| Google Contacts | Not supported (OAuth sign-in). |

The contacts are read at most every six hours and only names and birthdays are kept:
nothing else of a contact reaches the model, and no contact is ever written.

## What was tried

The code was first written against iCloud (mail, calendar, contacts) and Gmail (mail).
The test suite runs it against simulated servers, including one with dotted folder
names under the inbox and no special-folder marks. Fastmail, Nextcloud and other servers
are handled through the standards above but were not tried against the live services:
if something is off with yours, the per-account folder settings are the first thing to
try, and an issue with the provider's name is welcome.
