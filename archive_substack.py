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
POST_PATH = "/api/v1/posts/{slug}"


def fetch_post_body(base_url: str, slug: str, cookie_header: str = "") -> dict:
    url = base_url.rstrip("/") + POST_PATH.format(slug=slug)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_post_body_via_page(canonical_url: str, cookie_header: str = "") -> str:
    """Fallback: scrape body HTML from the rendered post page."""
    headers = {"User-Agent": UA, "Accept": "text/html"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    req = urllib.request.Request(canonical_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    # Substack embeds the post in <div class="body markup"> (or similar).
    m = re.search(
        r'<div[^>]+class="[^"]*\bbody markup\b[^"]*"[^>]*>(.*?)</div>\s*(?:<div[^>]+class="[^"]*subscribe-widget|<footer)',
        html, re.DOTALL,
    )
    if m:
        return m.group(1)
    # Fallback: any body markup div, lazier match
    m = re.search(r'<div[^>]+class="[^"]*\bbody markup\b[^"]*"[^>]*>(.*)', html, re.DOTALL)
    return m.group(1) if m else ""


def html_to_markdown(html: str) -> str:
    import html2text
    h = html2text.HTML2Text()
    h.body_width = 0  # disable line wrapping
    h.ignore_links = False
    h.ignore_images = False
    h.protect_links = True
    return h.handle(html or "")


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", path)
    if not m:
        return ".jpg"
    ext = m.group(1).lower()
    return f".{ext}" if ext in {"jpg", "jpeg", "png", "gif", "webp", "svg", "avif"} else ".jpg"


_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def download_images_and_rewrite(md: str, images_dir: Path) -> str:
    counter = {"n": 0}

    def repl(match: re.Match) -> str:
        alt = match.group(1)
        url = match.group(2)
        if not url.startswith(("http://", "https://")):
            return match.group(0)
        counter["n"] += 1
        filename = f"img-{counter['n']:03d}{_ext_from_url(url)}"
        local = images_dir / filename
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            images_dir.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            return f"![{alt}](images/{filename})"
        except Exception as e:
            print(f"    image fetch failed: {url} ({e})", file=sys.stderr)
            return match.group(0)

    return _MD_IMG_RE.sub(repl, md)


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
    p.add_argument("--no-markdown", action="store_true", help="Skip markdown export")
    p.add_argument("--no-pdf", action="store_true", help="Skip PDF export")
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

    want_pdf = not args.no_pdf
    want_md = not args.no_markdown

    def base_name_for(post: dict) -> str:
        title = post.get("title") or "untitled"
        date = (post.get("post_date") or "")[:10] or "0000-00-00"
        return f"{date}_{slugify(title)}"

    def pdf_path_for(post: dict) -> Path:
        return out_dir / f"{base_name_for(post)}.pdf"

    def md_path_for(post: dict) -> Path:
        return out_dir / "markdown" / base_name_for(post) / "post.md"

    def is_complete(post: dict) -> bool:
        ok = True
        if want_pdf:
            ok = ok and pdf_path_for(post).exists()
        if want_md:
            ok = ok and md_path_for(post).exists()
        return ok

    # Reconcile cache: if a post is in the cache but a required artifact
    # is missing on disk, drop it so we re-fetch what's missing.
    stale = {str(p.get("id")) for p in posts
             if str(p.get("id")) in archived and not is_complete(p)}
    if stale:
        archived -= stale
        cache_path.write_text(json.dumps(sorted(archived)))
        print(f"  {len(stale)} previously-archived posts incomplete on disk, will re-fetch", flush=True)

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
            # The API's by-slug endpoint expects the slug from the URL path,
            # which may differ from post.slug (older posts get a hash suffix).
            api_slug = urlparse(canonical).path.rsplit("/p/", 1)[-1].rstrip("/") or slug
            base = base_name_for(post)
            pdf_path = pdf_path_for(post)
            md_path = md_path_for(post)
            print(f"[{i}/{len(new_posts)}] {base}", flush=True)

            pdf_ok = pdf_path.exists() if want_pdf else True
            md_ok = md_path.exists() if want_md else True

            if want_pdf and not pdf_ok:
                for attempt in range(3):
                    if attempt > 0:
                        backoff = 15 * (2 ** (attempt - 1))
                        print(f"  pdf retry {attempt} after {backoff}s ...", flush=True)
                        time.sleep(backoff)
                    try:
                        render_pdf(ws_url, canonical, cookie_pair, cookie_domain, pdf_path)
                        if pdf_path.exists() and is_rate_limited_pdf(pdf_path):
                            pdf_path.unlink()
                            print("  got 'Too Many Requests' page, will retry", flush=True)
                            continue
                        pdf_ok = True
                        break
                    except Exception as e:
                        print(f"  pdf failed: {e}", file=sys.stderr)

            if want_md and not md_ok:
                try:
                    body_html = ""
                    try:
                        data = fetch_post_body(args.url, api_slug, args.cookie)
                        body_html = data.get("body_html") or ""
                    except urllib.error.HTTPError as e:
                        if e.code != 404:
                            raise
                    if not body_html:
                        body_html = fetch_post_body_via_page(canonical, args.cookie)
                    if not body_html:
                        raise RuntimeError("could not extract body (paywalled or wrong cookie?)")
                    md = html_to_markdown(body_html)
                    md_dir = md_path.parent
                    md_dir.mkdir(parents=True, exist_ok=True)
                    md = download_images_and_rewrite(md, md_dir / "images")
                    front = (
                        f"# {title}\n\n"
                        f"*Published {date}*\n\n"
                        f"[Original]({canonical})\n\n"
                        f"---\n\n"
                    )
                    md_path.write_text(front + md)
                    md_ok = True
                except Exception as e:
                    print(f"  markdown failed: {e}", file=sys.stderr)

            if pdf_ok and md_ok:
                archived.add(pid)
                cache_path.write_text(json.dumps(sorted(archived)))
                ok += 1
            else:
                missing = []
                if want_pdf and not pdf_ok: missing.append("pdf")
                if want_md and not md_ok: missing.append("md")
                print(f"  incomplete: missing {', '.join(missing)}", file=sys.stderr)

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
