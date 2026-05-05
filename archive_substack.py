#!/usr/bin/env python3
"""Archive a Substack publication to print-ready PDFs.

Enumerates posts via the public /api/v1/archive endpoint, then renders each
post to PDF using headless Chrome via the DevTools Protocol. For paid
publications, pass a session cookie (substack.sid) so the headless browser
loads as your subscribed self.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ARCHIVE_PATH = "/api/v1/archive?sort=new&search=&offset={offset}&limit={limit}"


def is_rate_limited_pdf(path: Path) -> bool:
    """Cheap heuristic: rate-limit pages render as tiny PDFs containing the phrase."""
    try:
        if path.stat().st_size > 60_000:
            return False
        blob = path.read_bytes()
        return b"Too Many Requests" in blob or b"429" in blob[:2000]
    except Exception:
        return False


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:80] or "untitled"


def fetch_archive(base_url: str, cookie_header: str = "") -> list[dict]:
    posts: list[dict] = []
    offset = 0
    limit = 50
    while True:
        url = base_url.rstrip("/") + ARCHIVE_PATH.format(offset=offset, limit=limit)
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if cookie_header:
            headers["Cookie"] = cookie_header
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.loads(r.read().decode("utf-8"))
        if not batch:
            break
        posts.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return posts


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_devtools(port: int, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                return json.loads(r.read())["webSocketDebuggerUrl"]
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    raise RuntimeError("Chrome DevTools didn't come up")


def render_pdf(ws_url: str, page_url: str, cookie_pair: tuple[str, str] | None,
               cookie_domain: str, out_path: Path) -> None:
    import websocket  # type: ignore

    ws = websocket.create_connection(ws_url, timeout=60)
    msg_id = 0

    def send(method: str, params: dict | None = None) -> dict:
        nonlocal msg_id
        msg_id += 1
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"{method} failed: {data['error']}")
                return data.get("result", {})

    try:
        target = send("Target.createTarget", {"url": "about:blank"})
        target_id = target["targetId"]
        sess = send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = sess["sessionId"]

        def send_s(method: str, params: dict | None = None) -> dict:
            nonlocal msg_id
            msg_id += 1
            ws.send(json.dumps({
                "id": msg_id, "method": method,
                "params": params or {}, "sessionId": session_id,
            }))
            while True:
                data = json.loads(ws.recv())
                if data.get("id") == msg_id and data.get("sessionId") == session_id:
                    if "error" in data:
                        raise RuntimeError(f"{method} failed: {data['error']}")
                    return data.get("result", {})

            return {}

        send_s("Network.enable")
        send_s("Page.enable")

        if cookie_pair:
            name, value = cookie_pair
            send_s("Network.setCookies", {"cookies": [{
                "name": name, "value": value,
                "domain": cookie_domain, "path": "/",
                "secure": True, "httpOnly": True,
            }]})

        send_s("Page.navigate", {"url": page_url})

        # Wait for load event
        deadline = time.time() + 60
        load_done = False
        while time.time() < deadline and not load_done:
            try:
                data = json.loads(ws.recv())
                if data.get("method") == "Page.loadEventFired":
                    load_done = True
            except Exception:
                break

        # Let lazy content settle
        time.sleep(2.5)

        result = send_s("Page.printToPDF", {
            "printBackground": True,
            "preferCSSPageSize": True,
        })
        import base64
        out_path.write_bytes(base64.b64decode(result["data"]))

        send("Target.closeTarget", {"targetId": target_id})
    finally:
        ws.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Archive a Substack to PDFs.")
    p.add_argument("--url", required=True, help="Substack base URL, e.g. https://foo.substack.com")
    p.add_argument("--out", required=True, help="Output directory")
    p.add_argument("--cookie", default="", help='Auth cookie, e.g. "substack.sid=s%%3A..."')
    p.add_argument("--limit", type=int, default=0, help="Cap number of new posts (0 = all)")
    args = p.parse_args()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / ".archived.json"
    archived: set[str] = set()
    if cache_path.exists():
        try:
            archived = set(json.loads(cache_path.read_text()))
        except Exception:
            archived = set()

    cookie_pair: tuple[str, str] | None = None
    if args.cookie:
        if "=" not in args.cookie:
            print("ERROR: --cookie must be NAME=VALUE", file=sys.stderr)
            return 2
        n, v = args.cookie.split("=", 1)
        cookie_pair = (n.strip(), v.strip())

    parsed = urlparse(args.url)
    cookie_domain = parsed.hostname or ""
    if not cookie_domain:
        print("ERROR: bad --url", file=sys.stderr)
        return 2

    print(f"Fetching archive from {args.url} ...", flush=True)
    try:
        posts = fetch_archive(args.url, args.cookie)
    except Exception as e:
        print(f"ERROR fetching archive: {e}", file=sys.stderr)
        return 1
    print(f"  {len(posts)} posts in archive", flush=True)

    new_posts = [p for p in posts if str(p.get("id")) not in archived]
    print(f"  {len(new_posts)} not yet archived", flush=True)
    if args.limit > 0:
        new_posts = new_posts[: args.limit]
    if not new_posts:
        print("Nothing to do.")
        return 0

    if not Path(CHROME).exists():
        print(f"ERROR: Chrome not found at {CHROME}", file=sys.stderr)
        return 1

    port = find_free_port()
    profile = tempfile.mkdtemp(prefix="substack-chrome-")
    chrome_proc = subprocess.Popen(
        [
            CHROME,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-crash-restore-bubble",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = wait_for_devtools(port)
        ok = 0
        for i, post in enumerate(new_posts, 1):
            pid = str(post.get("id"))
            title = post.get("title") or "untitled"
            slug = post.get("slug") or slugify(title)
            date = (post.get("post_date") or "")[:10] or "0000-00-00"
            canonical = post.get("canonical_url") or f"{args.url.rstrip('/')}/p/{slug}"
            fname = f"{date}_{slugify(title)}.pdf"
            out_path = out_dir / fname
            print(f"[{i}/{len(new_posts)}] {fname}", flush=True)
            success = False
            for attempt in range(3):
                if attempt > 0:
                    backoff = 15 * (2 ** (attempt - 1))
                    print(f"  retry {attempt} after {backoff}s ...", flush=True)
                    time.sleep(backoff)
                try:
                    render_pdf(ws_url, canonical, cookie_pair, cookie_domain, out_path)
                    if out_path.exists() and is_rate_limited_pdf(out_path):
                        out_path.unlink()
                        print("  got 'Too Many Requests' page, will retry", flush=True)
                        continue
                    archived.add(pid)
                    cache_path.write_text(json.dumps(sorted(archived)))
                    ok += 1
                    success = True
                    break
                except Exception as e:
                    print(f"  failed: {e}", file=sys.stderr)
            if not success:
                print(f"  giving up on {fname}", file=sys.stderr)
            time.sleep(5)  # be polite
        print(f"Done. {ok}/{len(new_posts)} archived.")
        return 0 if ok == len(new_posts) else 1
    finally:
        chrome_proc.terminate()
        try:
            chrome_proc.wait(timeout=5)
        except Exception:
            chrome_proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
