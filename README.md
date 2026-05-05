# Substack Archiver

A small, dependency-light Python script that downloads Substack posts (free
or paid) as print-ready PDFs. Designed to run on a schedule via `launchd`
on macOS so new posts are archived automatically.

## How it works

1. Calls the Substack archive API with your session cookie to enumerate
   every post you have access to.
2. For each post not already archived, opens it in headless Chrome and
   prints to PDF using the Chrome DevTools Protocol — your auth cookie is
   injected so paid content unlocks.
3. Records archived post IDs in `.archived.json` so re-runs only fetch
   new posts. Safe to run as often as you like.
4. Throttles between fetches and retries on rate-limit pages.

Files are named `YYYY-MM-DD_post-title.pdf` so they sort chronologically.

## Quick setup

There's exactly **one file you edit**: `config.sh`.

```bash
git clone https://github.com/<you>/substack-archiver.git
cd substack-archiver
chmod +x run.sh archive_substack.py

# 1. Install the only Python dep
pip3 install --break-system-packages websocket-client

# 2. Create your local config (gitignored)
cp config.sh.example config.sh

# 3. Open config.sh and fill in the 3 required values (see below)
$EDITOR config.sh

# 4. Test
./run.sh
```

If you're on python.org Python 3 and hit `CERTIFICATE_VERIFY_FAILED`, run:

```bash
"/Applications/Python 3.13/Install Certificates.command"
```

## What to put in `config.sh`

`config.sh.example` is the template. Copy it to `config.sh` and set:

| Variable        | What it is                                                                 |
|-----------------|----------------------------------------------------------------------------|
| `SUBSTACK_URL`  | Your publication, e.g. `https://yourname.substack.com`                     |
| `OUTPUT_DIR`    | Where PDFs land. Created if missing.                                       |
| `COOKIE`        | `substack.sid=<value>` — see "Grabbing your cookie" below.                 |
| `REQUIRE_MOUNT` | Optional. External-drive mount point; script no-ops if not mounted.       |
| `PYTHON_BIN`    | Optional. Path to your Python 3. Default: `/usr/local/bin/python3`.        |

That's the whole configuration surface. `run.sh` and the launchd plist
read from `config.sh` and never need editing for value changes.

### Grabbing your cookie

For paid Substacks, the cookie tells Substack you're a paying subscriber.

1. In Chrome, log into your Substack publication.
2. Open DevTools (Cmd+Option+I) → **Application** tab → **Cookies** in
   the left sidebar → click the publication's domain.
3. Find the row named `substack.sid` and copy the **Value** (starts with
   `s%3A`, ends without trailing whitespace).
4. In `config.sh`:
   ```
   COOKIE="substack.sid=s%3A...your_value..."
   ```

The cookie typically lasts months. If PDFs start coming back paywalled,
re-grab it.

## Automate with launchd (macOS)

The included plist runs the archiver every Sunday at 7am.

1. Open `com.example.substack-archiver.plist` and replace each
   `/ABSOLUTE/PATH/TO/substack-archiver/` with the actual path to your
   clone (3 occurrences). launchd doesn't expand `$HOME`.
2. Install and load:
   ```bash
   cp com.example.substack-archiver.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.example.substack-archiver.plist
   ```
3. Verify:
   ```bash
   launchctl list | grep substack-archiver
   launchctl start com.example.substack-archiver   # test fire
   tail -f archiver.log
   ```
4. To unload later:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.example.substack-archiver.plist
   ```

The Mac must be awake at the scheduled time for launchd to fire.

## Troubleshooting

- **PDFs blank or paywalled**: cookie expired or pasted with stray
  whitespace. Re-grab from a fresh DevTools session.
- **"Too Many Requests" PDFs**: Substack rate-limited. The script auto-
  retries with backoff and detects these so they aren't cached as done.
  If they persist, increase the inter-post delay in `archive_substack.py`.
- **`CERTIFICATE_VERIFY_FAILED`**: run the Python "Install Certificates"
  command shown above.
- **WebSocket 403 from DevTools**: handled via `--remote-allow-origins=*`.
- **launchd job doesn't fire**: Mac was asleep, or the plist paths are
  wrong. Check `archiver.err`.

## Security

`config.sh` (which contains your session cookie) is gitignored. Only
`config.sh.example` ships in the repo. Don't commit your real `config.sh`.
