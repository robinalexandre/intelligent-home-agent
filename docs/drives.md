# File drives

The agent can work with the files you keep in a drive: Google Drive, iCloud Drive,
OneDrive, Dropbox, a NAS, a bucket. It goes through [rclone](https://rclone.org), which
speaks to some seventy storage services with one API, running as a sidecar container next
to the agent. Several drives can be set up at once; each has a short name the agent uses.

- [What the agent does](#what-the-agent-does)
- [Which services](#which-services), and [what was really tested](#what-was-tested)
- [Setup](#setup), then one section per kind of login:
  [browser login](#browser-login-oauth), [password and code](#password-and-code-icloud-drive),
  [password or key](#password-or-key)
- [Sharing only some folders, and other wrappers](#sharing-only-some-folders-and-other-wrappers)
- [The index](#the-index), [search inside the files](#search-inside-the-files),
  [when a login fails](#when-a-login-fails), [settings](#settings)

## What the agent does

- **Browse, search, read.** It lists a folder, finds files by words of their name or
  folders, says what changed lately and how much a folder holds, and reads a file (PDF,
  image, text, office document; 100 MB at most).
- **Search inside the files** (PDF, Word, Excel, PowerPoint, OpenDocument, text), from an
  index of their text kept on the agent's disk. Scanned PDFs too, when the model reads
  them (see [below](#search-inside-the-files)).
- **File things away.** It creates folders, copies files (within a drive or from one drive
  to another) and saves there a file you sent or one a tool fetched. Gathering files is a
  copy: the originals stay where they are. A file or a folder is moved or renamed only
  when you ask for exactly that.
- **It never replaces a file.** When a name is taken, the copy gets a number
  (`Quote_2.pdf`). rclone itself would overwrite in place, so the agent checks the name
  in the destination's listing right before each write. A folder is always copied or moved
  into a new folder, as rclone would overwrite files already at the destination.
- **It deletes only on your own message**, one file or one folder at a time (a folder
  with files only when you asked for its contents too). Before deleting it copies the file
  or the folder to its data volume (`DATA_DIR/drive-trash`, 30 days by default), and can
  put it back where it was, under a free name. What is too big to copy (see
  [settings](#settings)), or has no known size (a Google Doc), is not deleted unless you
  then agree to delete it without a copy. Many services also keep their own trash; the
  agent says which when it knows.

Writes follow the rules of the rest of the agent. A folder listing, a search or a file
read *taints* the request, since names and contents are written by others: after that,
every write waits for your go, listed under the reply exactly as it will run. So text
hidden in a document can't make the agent copy private files into a shared folder or
delete anything. In a scheduled task a deletion always waits for you.

A drive marked `read_only` is never written to: the agent only lists, searches and reads
it (and may copy *from* it to another drive). With only read-only drives, the tools that
write don't exist at all.

## Which services

The agent only uses rclone's remote-control API (`operations/list`, `stat`, `copyfile`,
`uploadfile`...), which is the same for every backend. So any backend rclone has should
work; what differs is how you log in, how fast it lists, and whether it keeps a trash.
`backend` below is rclone's type name, which you give in `DRIVES` to get the right advice
when a login fails.

**Personal drives**

| Service | `backend` | Login | Notes |
|---|---|---|---|
| Google Drive | `drive` | [browser](#browser-login-oauth) | Google Docs, Sheets and Slides are read as exported Office files; they have no size, so the agent can't keep a copy before deleting one. |
| iCloud Drive | `iclouddrive` | [password and code](#password-and-code-icloud-drive) | Login renewed by hand every 30 days. Lists one folder at a time. |
| OneDrive (personal, business, SharePoint) | `onedrive` | [browser](#browser-login-oauth) | |
| Dropbox | `dropbox` | [browser](#browser-login-oauth) | |
| Google Photos | `google photos` | [browser](#browser-login-oauth) | Google's API only shows the photos and videos rclone itself uploaded, not your library: good for sending pictures there, not for finding them. Nothing can be deleted. Set it `read_only`, or expect to only add. |
| Box | `box` | [browser](#browser-login-oauth) | |
| MEGA | `mega` | [password](#password-or-key) | |
| pCloud | `pcloud` | [browser](#browser-login-oauth) | |
| Proton Drive | `protondrive` | [password](#password-or-key), plus a 2FA code once | rclone calls this backend beta. |
| Others: Yandex Disk, Jottacloud, HiDrive, Koofr, Zoho WorkDrive, put.io... | as rclone names it | see [rclone's list](https://rclone.org/overview/) | |

**NAS and self-hosted**

| Service | `backend` | Login | Notes |
|---|---|---|---|
| WebDAV, including Nextcloud and ownCloud | `webdav` | [password](#password-or-key) | For Nextcloud, an app password. |
| SMB (Windows shares, most NAS) | `smb` | [password](#password-or-key) | The remote's path starts with the share: `nas:share/folder`. |
| SFTP (any machine with SSH) | `sftp` | [password or key](#password-or-key) | |
| Seafile | `seafile` | [password](#password-or-key) | |
| FTP | `ftp` | [password](#password-or-key) | |

**Object storage**

| Service | `backend` | Login | Notes |
|---|---|---|---|
| Amazon S3 and compatibles (Cloudflare R2, MinIO, Wasabi, Scaleway, DigitalOcean Spaces...) | `s3` | [keys](#password-or-key) | The remote's path starts with the bucket: `s3:bucket/prefix`. Reading files is billed by some providers: see `content` in [settings](#settings). |
| Backblaze B2 | `b2` | [keys](#password-or-key) | |
| Google Cloud Storage | `google cloud storage` | [a service account file](#password-or-key) | |
| Azure Blob Storage | `azureblob` | [keys](#password-or-key) | |

A bucket has no real folders and no trash: a deleted object is gone unless versioning is
on, so the agent's own copy matters most there.

### What was tested

Be aware of what "works" means here.

- **Tested against the real service: iCloud Drive only.** The code this module comes from
  ran daily against iCloud Drive (folder walk, search, reading, creating folders, copies,
  moves, deletions with the local copy, renewal every 30 days).
- **Everything else should work, and has not been run against the real service.** The
  reason to expect it to work: the agent calls the same rclone API whatever the backend,
  and never a service directly. The reasons it might not: a backend that answers a call
  differently (no modification time, no size, case-sensitive names, no empty folders),
  rate limits during the index, or an error text the agent doesn't recognize as a failed
  login.
- **Only tested with a simulated rclone:** several drives at once, copies from one drive
  to another, the one-call recursive listing, read-only drives. The automated tests cover
  them, no real sidecar did.

If you run it against another service, a note in an issue on what worked and what didn't
is welcome.

## Setup

1. **Add the sidecar.** Run `python scripts/setup.py` and answer yes to "File drives", or
   by hand in `.env`:

   ```
   COMPOSE_FILE=docker-compose.yml:compose/drive.yml     # plus the files you already use
   DRIVE_PASSWORD=<a long random string, e.g. openssl rand -hex 24>
   DRIVES=[{"name":"gdrive","remote":"gdrive:","backend":"drive"}]
   ```

   `compose/drive.yml` starts rclone with no published port, on a network only the agent
   shares, and it refuses to start without `DRIVE_PASSWORD`. Its logins live in the
   `drive_config` volume.

2. **Log each drive in**, as the section for its kind of login says below. In every case
   you create a *remote* in rclone's config, under the name you wrote in `DRIVES` before
   the colon (`gdrive:` is the remote `gdrive`).

3. **Start and check.**

   ```bash
   docker compose up -d
   docker compose exec drive rclone lsd gdrive:     # your top-level folders
   docker compose logs agent | grep tools:          # list_drive_folder and the others
   ```

   Then ask the agent "what's in my Documents folder?". Search by name answers once the
   first index is built: a minute or so after the start for a service that lists fast,
   many minutes for a big iCloud Drive.

Each entry of `DRIVES` takes:

| Key | |
|---|---|
| `name` | Short name the tools take: lowercase letters, digits, `_`. |
| `remote` | The rclone remote, with an optional folder to stay inside: `gdrive:`, `nas:Documents`, `s3:bucket/papers`. |
| `backend` | rclone's type of the storage (tables above). Optional: without it the agent gives general advice when the login fails, and doesn't know about the service's trash. |
| `label` | What you call it ("Home NAS"). Default: the service's name. |
| `read_only` | `true`: list, search and read only. |
| `content` | `false`: leave this drive out of the search inside files. |

Without Docker, run rclone yourself, on the same machine as the agent, and point the
agent at it:

```bash
rclone rcd --rc-addr=127.0.0.1:5572 --rc-serve --rc-user=agent --rc-pass=<DRIVE_PASSWORD>
# .env: DRIVE_URL=http://127.0.0.1:5572
```

Never give the agent a remote that is a path of the machine (`/home/...`): the settings
refuse anything that isn't a named remote.

### Browser login (OAuth)

Google Drive, OneDrive, Dropbox, Box, pCloud, Google Photos and most consumer drives log
in through a web page. The server has no browser, so the page opens on your computer and
you paste the result back:

1. On the server, start rclone's setup inside the sidecar's image:

   ```bash
   docker compose run --rm -it --entrypoint rclone drive config
   ```

   Choose `n` (new remote), the name from `DRIVES` (`gdrive`), then the service. Keep the
   defaults until *"Use web browser to automatically authenticate?"* and answer **n**.
   rclone prints a command starting with `rclone authorize`.
2. On a computer with a browser and [rclone installed](https://rclone.org/install/), run
   that command. The login page opens; approve it. rclone prints a token.
3. Paste the token back on the server, finish the questions, and quit (`q`).
4. `docker compose restart drive`, so the sidecar reads the new config.

rclone then renews the token by itself and writes it back to its config. It stops working
when the service revokes it: you changed your password, removed rclone's access, or left
it unused for months (Box: 60 days, OneDrive: about 90). Then renew it:

```bash
docker compose run --rm -it --entrypoint rclone drive config reconnect gdrive:
docker compose restart drive
```

Google Drive: rclone's shared app is rate-limited, so its documentation recommends
[your own client ID](https://rclone.org/drive/#making-your-own-client-id). If you make
one, publish the app ("In production"): while it is in "Testing", Google expires the
login every 7 days. To let the agent read only, choose the `drive.readonly` scope during
setup *and* set `read_only` in `DRIVES`.

### Password and code (iCloud Drive)

Apple has no API for iCloud Drive: rclone logs in like a browser, with the Apple ID's own
password and a two-factor code.

- **App-specific passwords are refused**: it must be the account's password. rclone's
  config stores it obscured, not encrypted, in the `drive_config` volume of your server.
- **Advanced Data Protection must be off**, and "Access iCloud Data on the Web" on
  (iPhone: Settings > your name > iCloud).
- **The login lasts 30 days**, then must be renewed by hand.

```bash
docker compose run --rm -it --entrypoint rclone drive config
```

Choose `n`, the name from `DRIVES` (`icloud`), the type `iclouddrive`, then your Apple ID
and password. A code arrives on your Apple devices: type it. Quit, then
`docker compose restart drive`.

Every 30 days (the agent says when the login was refused):

```bash
docker compose run --rm -it --entrypoint rclone drive config reconnect icloud:
docker compose restart drive
```

### Password or key

WebDAV, SMB, SFTP, FTP, Seafile, S3 and compatibles, B2, Azure, MEGA: a password or a key
in the config, and nothing expires. Create the remote the same way:

```bash
docker compose run --rm -it --entrypoint rclone drive config
docker compose restart drive
```

or in one line, for instance:

```bash
# Nextcloud (an app password from its security settings)
docker compose run --rm --entrypoint rclone drive config create cloud webdav \
  url=https://cloud.example.com/remote.php/dav/files/sam vendor=nextcloud \
  user=sam pass=<app password> --obscure

# A NAS over SMB
docker compose run --rm --entrypoint rclone drive config create nas smb \
  host=192.168.1.50 user=sam pass=<password> --obscure

# A machine over SFTP, with a password (for a key: copy it into the drive_config volume
# and give key_file=/config/rclone/<file>)
docker compose run --rm --entrypoint rclone drive config create box sftp \
  host=192.168.1.51 user=sam pass=<password> --obscure

# S3 or a compatible service
docker compose run --rm --entrypoint rclone drive config create s3 s3 \
  provider=AWS access_key_id=<id> secret_access_key=<secret> region=eu-west-1
```

Good practice: give rclone its own account or key, limited to the folders or the bucket
the agent should see, and read-only at the service when you only want reading.

- Proton Drive asks for the account's password and, when the account has one, a 2FA code
  typed once at setup.
- Google Cloud Storage on a server is simplest with a service account file, copied into
  the volume and given as `service_account_file=/config/rclone/<file>`.
- A NAS on your home network is reached from the sidecar like any address of that
  network; when the agent runs elsewhere, the NAS must be reachable from there (a VPN, or
  the WebDAV your NAS publishes).

## Sharing only some folders, and other wrappers

rclone can wrap remotes into another remote, which you then give as a drive. Add these to
the config with `rclone config` (same command as above); for `backend`, give the type of
the storage *behind* the wrapper, since that is where the login is.

- **A folder in `remote`** is the simplest: `"remote": "gdrive:Shared with the agent"`.
- **`combine`**: several folders shown as one tree, to share a few folders and nothing
  else.

  ```ini
  [papers]
  type = combine
  upstreams = Documents=icloud:Documents Desktop=icloud:Desktop
  ```

  with `{"name":"icloud","remote":"papers:","backend":"iclouddrive"}`.
- **`alias`**: another name for a folder of a remote.
- **`union`**: several remotes merged into one tree.
- **`crypt`**: files encrypted at the service; the agent sees them decrypted, and the
  key is in rclone's config.

## The index

Browsing a folder and reading a file are live. Search by name and "what changed lately"
use an index of file names, kept in `DATA_DIR/drive-index-<name>.json` and rebuilt in the
background once a day (`DRIVE_INDEX_HOURS`), so today's changes may be missing from it.

How long a rebuild takes depends on the service:

- Where the service can list a whole tree in one request (rclone's `ListR` feature:
  typically Google Drive, OneDrive, Dropbox, S3 and the other object stores), the agent
  asks rclone for one recursive listing. This is fast, and you can lower
  `DRIVE_INDEX_HOURS`.
- Elsewhere (iCloud Drive answers one folder at a time; WebDAV, SMB, SFTP and FTP too),
  it walks the folders itself, eight at a time. A big drive takes many minutes.
- The agent asks rclone which case applies; when the one-request listing fails, it falls
  back to the walk. The log says which was used (`recursive` or `folders`).

Either way the index leaves out what nobody looks for by name: hidden folders, build
output and caches (`node_modules`, `build`, `cache`...), folders of compiled or generated
files, and everything *inside* a code project or installed software (a folder with
`.git`, `package.json`, a Gradle project...), which is indexed as one entry. You can still
browse into those.

If too many folders can't be read during a rebuild, the previous index is kept and the
rebuild is tried again ten minutes later.

## Search inside the files

With `DRIVE_CONTENT=true` (the default once drives are set up), the agent keeps the text
of your PDFs, office documents and text files in a search index on its own disk
(`DATA_DIR/drive-content-<name>.sqlite`), so it can find "the contract with number 4471".
Each hour it downloads the files that are new or changed, 150 at most, newest first: the
first pass over a big drive takes a few days, and nothing unchanged is read twice. Files
over 30 MB are skipped. Set `"content": false` on a drive that is too big or billed by the
download.

**Scanned PDFs** have no text to index. With `DRIVE_OCR=model` the agent sends them to the
model to be transcribed, 10 per hour at most, 20 MB and 40 pages at most each. Know that:

- each scan is a paid request to your model provider, and the document leaves your
  server for it (like any file you ask the agent to read);
- **it only works with `LLM_PROVIDER=anthropic` today**: the other providers are reached
  through an API that takes no PDF. With them `DRIVE_OCR=model` changes nothing: the log
  says so at start, scans stay findable by name, and PDFs with a text layer are indexed
  as usual.

## When a login fails

The agent tells you a drive's login was refused and what to do, according to `backend`.
rclone's own error is in the agent's log (`docker compose logs agent | grep Drive`), and
`docker compose logs drive` has the sidecar's.

| What the agent says | What to do |
|---|---|
| "the login was refused", browser-login services | `rclone config reconnect <remote>:` as in [browser login](#browser-login-oauth). |
| "the login was refused", iCloud Drive | The 30 days are over: [renew it](#password-and-code-icloud-drive). |
| "its login never expires: check..." | The server is down or moved, or the password or key changed: fix the remote with `rclone config`. |
| "rclone's config has no remote for ..." | The name before the colon in `DRIVES` is not a remote of the config: create it, or fix the name. |
| "the rclone sidecar refused the agent" | `DRIVE_PASSWORD` is not the same for the agent and the sidecar: restart both. |
| "the rclone sidecar can't be reached" | `docker compose ps`: is `drive` running? It stops at once without `DRIVE_PASSWORD`. |

## Settings

| Setting | Default | |
|---|---|---|
| `DRIVE_URL` | set by `compose/drive.yml` | Where rclone's API is. |
| `DRIVE_PASSWORD` | unset | Shared by the agent and the sidecar. |
| `DRIVES` | `[]` | The drives (above). |
| `DRIVE_INDEX_HOURS` | `24` | How old the index of names may get. |
| `DRIVE_TRASH_DAYS` | `30` | How long the copy of a deleted file is kept. |
| `DRIVE_TRASH_MAX_ITEM_MB` | `1024` | The biggest file or folder copied before a deletion. |
| `DRIVE_TRASH_MAX_TOTAL_MB` | `5120` | The most the copies may take; also, the disk must keep 15 % free. |
| `DRIVE_CONTENT` | `true` | Search inside the files. |
| `DRIVE_OCR` | `off` | `model`: the model reads scanned PDFs (Anthropic only). |
| `DRIVE_OCR_MODEL` | a small Claude model | The model that reads them. |

Email is not a drive: mailboxes are read over IMAP (`MAIL_ACCOUNTS`).
