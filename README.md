# Substack Archiver

A small Python script that downloads Substack posts (free or paid) as
**both print-ready PDFs and self-contained Markdown** (with images
downloaded locally). Designed to run on a schedule via `launchd` on
macOS so new posts are archived automatically.

## How it works

1. Calls the Substack archive API with your session cookie to enumerate
   every post you have access to.
2. For each post not already archived, produces:
   - A **PDF** — opens the post in headless Chrome with your cookie
     injected and prints to PDF (paid content unlocks).
   - A **Markdown folder** — fetches the post HTML from the API,
     converts to Markdown, downloads every image locally, and rewrites
     image references to point at the local files. Self-contained;
     works offline forever.
3. Records archived post IDs in `.archived.json`. Re-runs only fetch
   what's missing — including artifacts you've deleted from disk.
4. Throttles between fetches and retries on rate-limit pages.

### Output layout

```
OUTPUT_DIR/
├── 2026-05-05_post-title.pdf           ← print-ready
├── 2026-05-04_another-post.pdf
├── markdown/
│   ├── 2026-05-05_post-title/
│   │   ├── post.md                     ← self-contained
│   │   └── images/
│   │       ├── img-001.jpg
│   │       └── img-002.png
│   └── 2026-05-04_another-post/
│       └── ...
└── .archived.json                      ← cache (do not edit)
```

Skip either format with `--no-pdf` or `--no-markdown`.

## Quick setup

There's exactly **one file you edit**: `config.sh`.

```bash
git clone https://github.com/<you>/substack-archiver.git
cd substack-archiver
chmod +x run.sh archive_substack.py

# 1. Install Python deps
pip3 install --break-system-packages websocket-client html2text

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
