#!/usr/bin/env python3
"""
Scrape LiveXo "Special Sports" channel list into JSON / M3U / TXT.

How it works:
  1. GET /sports and read the category labels off the filter buttons.
  2. For each label, GET /api/public/special-section?label=<label> and read JSON.
  3. Split each stream "url" into the real URL plus its required request headers
     (the site packs them after a "|", e.g. url|Referer=...&User-Agent=...).
  4. Emit streams/streams.json, streams/streams.m3u, streams/streams.txt.

No third-party packages: stdlib only.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote, unquote, unquote_plus

BASE = os.environ.get("BASE_URL", "https://xo.livexoapp1.workers.dev").rstrip("/")
OUT_DIR = os.environ.get("OUT_DIR", "streams")
SPORTS_URL = f"{BASE}/sports"
API_URL = f"{BASE}/api/public/special-section"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADER_KEYS = {"user-agent", "referer", "origin", "cookie", "authorization"}
RETRIES = 3
SLEEP = 0.5

FALLBACK_LABELS = [
    "SonyLiv Live Event",
    "SonyLiv Event Backup",
    "SONY SPORTS",
    "World Wide",
    "SP",
    "Willow Cricket",
    "Fancode (INDIA ONLY)",
    "FOX",
    "T sports",
    "Star Sports",
    "Fancode Pro +",
    "Fancode Worldwide",
    "SonyLiv Event Worldwide",
]


def fetch(url, timeout=30):
    last = None
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": SPORTS_URL,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
            print(f"  ! attempt {attempt}/{RETRIES} failed: {exc}", file=sys.stderr)
            time.sleep(attempt * 1.5)
    raise RuntimeError(f"GET failed: {url}") from last


def decode_value(raw):
    """Undo URL encoding, tolerating double-encoded values."""
    val = raw
    for _ in range(3):
        nxt = unquote_plus(val)
        if nxt == val:
            break
        val = nxt
    return val.strip()


def split_stream(raw):
    """'https://x/y.mpd|Referer=...&User-Agent=...' -> (url, {headers})."""
    if not raw:
        return None, {}
    url, _, packed = raw.partition("|")
    url = url.strip()
    headers = {}
    if packed:
        for chunk in packed.split("&"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key = decode_value(key)
            value = decode_value(value)
            if key.lower() in HEADER_KEYS and value:
                headers[key] = value
    return (url or None), headers


def get_labels(page_html):
    labels = []
    for match in re.finditer(r"<button[^>]*>(.*?)</button>", page_html, re.S):
        text = html.unescape(re.sub(r"<!--.*?-->|<[^>]+>", "", match.group(1))).strip()
        hit = re.match(r"^(.*?)\s*\((\d+)\)$", text)
        if not hit:
            continue
        label = hit.group(1).strip()
        if not label or label.lower().startswith("all events"):
            continue
        if label not in labels:
            labels.append(label)
    return labels


def fetch_section(label):
    query = (
        f"?label={quote(label)}"
        "&attempt=0&currentCount=0&currentFingerprint="
        f"&_t={int(time.time() * 1000)}"
    )
    payload = json.loads(fetch(API_URL + query))
    if not payload.get("succeeded") or payload.get("isEmpty"):
        return []
    return payload.get("channels") or []


def build_m3u(channels, generated):
    lines = [
        "#EXTM3U",
        f"# Playlist generated {generated} from {SPORTS_URL}",
        "# DRM entries carry a ClearKey kid:key pair; players must support DASH + ClearKey.",
    ]
    for ch in channels:
        name = (ch.get("name") or "Unknown").replace('"', "'")
        logo = ch.get("logo") or ""
        group = (ch.get("group") or "Sports").replace('"', "'")
        lines.append(
            f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{name}'
        )
        for hkey, hval in ch["headers"].items():
            if hkey.lower() == "user-agent":
                lines.append(f"#EXTVLCOPT:http-user-agent={hval}")
            elif hkey.lower() == "referer":
                lines.append(f"#EXTVLCOPT:http-referrer={hval}")
            elif hkey.lower() == "origin":
                lines.append(f"#EXTVLCOPT:http-origin={hval}")
        if ch.get("key"):
            lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
            lines.append(f"#KODIPROP:inputstream.adaptive.license_key={ch['key']}")
        lines.append(ch["url"])
    return "\n".join(lines) + "\n"


def main():
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"-> {SPORTS_URL}")
    try:
        page = fetch(SPORTS_URL)
    except RuntimeError as exc:
        print(f"! could not load page: {exc}", file=sys.stderr)
        return 1

    labels = get_labels(page)
    if not labels:
        print("! no labels parsed from page, using fallback list", file=sys.stderr)
        labels = FALLBACK_LABELS
    print(f"-> {len(labels)} categories: {', '.join(labels)}")

    seen = set()
    channels = []
    for label in labels:
        print(f"-> section: {label}")
        try:
            section = fetch_section(label)
        except Exception as exc:  # noqa: BLE001 - keep going on a bad section
            print(f"  ! section failed: {exc}", file=sys.stderr)
            time.sleep(SLEEP)
            continue
        print(f"   {len(section)} channels")
        for item in section:
            url, headers = split_stream(item.get("url"))
            if not url:
                continue
            name = (item.get("name") or "").strip()
            fingerprint = f"{name}::{url}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            channels.append(
                {
                    "name": name or "Unknown",
                    "group": item.get("group") or item.get("source") or label,
                    "url": url,
                    "headers": headers,
                    "key": item.get("key"),
                    "logo": item.get("logo"),
                    "tvg_id": item.get("tvgId"),
                    "sid": item.get("sid"),
                    "reminder": item.get("reminder"),
                    "routing": item.get("routing"),
                    "watch_page": (
                        f"{BASE}/watch?url={quote(url, safe='')}"
                        f"&name={quote(name)}&from=sports&mode=web"
                    ),
                }
            )
        time.sleep(SLEEP)

    if not channels:
        print("! no channels collected", file=sys.stderr)
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    meta = {
        "source": SPORTS_URL,
        "generated_at": generated,
        "count": len(channels),
        "categories": labels,
    }

    with open(os.path.join(OUT_DIR, "streams.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "channels": channels}, fh, indent=2, ensure_ascii=False)

    with open(os.path.join(OUT_DIR, "streams.m3u"), "w", encoding="utf-8") as fh:
        fh.write(build_m3u(channels, generated))

    with open(os.path.join(OUT_DIR, "streams.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"# {generated} | {len(channels)} channels | {SPORTS_URL}\n")
        for ch in channels:
            fh.write(f"{ch['name']}\t{ch['url']}\n")

    print(f"done: {len(channels)} unique channels -> {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
