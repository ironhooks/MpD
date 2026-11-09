#!/usr/bin/env python3
"""
CineWW channel stream extractor.

Flow:
  1. GET  {base}/api/main.json        -> channel list (id, title, logo, ...)
  2. GET  {base}/<id>                 -> player route (raw HTML text)
  3. GET  {base}/<id>/js/inline_scripts.js  (if present, merged)
  4. regex the combined response text for mpd / keyId (kid) / key / licenseUrl / noDrm
  5. print everything, write JSON + TXT + M3U

Usage:
  python scripts/extract_streams.py
  python scripts/extract_streams.py --id bein1 --id tnt1
  python scripts/extract_streams.py --ids bein1,tnt1,cbs --out output
  python scripts/extract_streams.py --limit 5 --concurrency 4 --pretty
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

DEFAULT_BASE = os.environ.get("CINE_BASE_URL", "https://cinewwchannels.pages.dev").rstrip("/")
API_PATH = "/api/main.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

HEX32 = r"[0-9a-fA-F]{32}"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# --- extraction patterns (ordered, first hit wins) -------------------------

PAIR_RE = re.compile(
    rf"""kid\s*:\s*["']({HEX32})["']\s*,\s*key\s*:\s*["']({HEX32})["']""", re.I
)

MPD_PATTERNS = [
    re.compile(r"""mpdUrl\s*:\s*["']([^"']+)["']""", re.I),
    re.compile(r"""manifestUrl\s*:\s*["']([^"']+)["']""", re.I),
    re.compile(r"""["'](https?://[^"'\s<>\\]+\.mpd(?:\?[^"'\s<>\\]*)?)["']""", re.I),
    re.compile(r"""(https?://[^\s"'<>\\]+\.mpd(?:\?[^\s"'<>\\]*)?)""", re.I),
]

KID_PATTERNS = [
    re.compile(rf"""keyId\s*:\s*["']({HEX32})["']""", re.I),
    re.compile(rf"""\bkid\s*:\s*["']({HEX32})["']""", re.I),
    re.compile(rf"""cenc:default_KID\s*=\s*["']({UUID})["']""", re.I),
    re.compile(rf"""default_KID\s*[=>:]\s*["']?({UUID})""", re.I),
    re.compile(rf"""keyId\s*:\s*["']({UUID})["']""", re.I),
]

KEY_PATTERNS = [
    re.compile(rf"""\bkey\s*:\s*["']({HEX32})["']""", re.I),
    re.compile(rf"""clearKey\s*:\s*["']({HEX32})["']""", re.I),
    re.compile(rf"""\bkey\s*:\s*["']({HEX32}|[A-Za-z0-9+/=]{{16,64}})["']""", re.I),
]

LICENSE_PATTERNS = [
    re.compile(r"""licenseUrl\s*:\s*["']([^"']+)["']""", re.I),
    re.compile(r"""licenseServer\s*:\s*["']([^"']+)["']""", re.I),
    re.compile(r"""laUrl\s*:\s*["']([^"']+)["']""", re.I),
    re.compile(
        r"""["'](https?://[^"'\s<>]+/(?:license|licence|widevine|wv|drm)[^"'\s<>]*)["']""",
        re.I,
    ),
]

NO_DRM_RE = re.compile(r"""noDrm\s*:\s*(?:true|!0|1)""", re.I)


def norm_hex(value: str) -> str:
    return value.replace("-", "").strip().lower()


def first_match(patterns: Iterable[re.Pattern[str]], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def extract(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {
        "mpd": None,
        "kid": None,
        "key": None,
        "license_url": None,
        "no_drm": bool(NO_DRM_RE.search(text)),
    }

    found["mpd"] = first_match(MPD_PATTERNS, text)

    pair = PAIR_RE.search(text)
    if pair:
        found["kid"] = norm_hex(pair.group(1))
        found["key"] = pair.group(2).strip().lower()

    if not found["kid"]:
        kid = first_match(KID_PATTERNS, text)
        if kid:
            found["kid"] = norm_hex(kid)

    if not found["key"]:
        key = first_match(KEY_PATTERNS, text)
        if key:
            found["key"] = key.strip().lower()

    found["license_url"] = first_match(LICENSE_PATTERNS, text)
    return found


# --- http -----------------------------------------------------------------

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/json,application/javascript,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
    )
    return session


def get_text(
    session: requests.Session,
    url: str,
    timeout: float,
    retries: int,
) -> tuple[int, str]:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            if response.status_code == 200:
                return response.status_code, response.text
            if response.status_code in (404, 410):
                return response.status_code, response.text
            last_exc = RuntimeError(f"HTTP {response.status_code}")
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    raise last_exc if last_exc else RuntimeError("request failed")


# --- channels -------------------------------------------------------------

def load_channels(session: requests.Session, base: str, timeout: float, retries: int) -> list[dict[str, Any]]:
    url = base + API_PATH
    status, body = get_text(session, url, timeout, retries)
    if status != 200:
        raise SystemExit(f"channel list failed: HTTP {status} ({url})")

    data = json.loads(body)
    rows = data.values() if isinstance(data, dict) else data

    channels: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("id") or item.get("slug") or "").strip()
        if not channel_id:
            continue
        channels.append(
            {
                "index": index,
                "id": channel_id,
                "title": str(item.get("title") or item.get("name") or channel_id).strip(),
                "logo": str(item.get("logo") or "").strip(),
                "genre": str(item.get("genre") or "").strip(),
                "language": str(item.get("language") or "").strip(),
                "player_url": str(item.get("iframeSrc") or f"{base}/{channel_id}").strip(),
            }
        )
    return channels


def probe_channel(
    session: requests.Session,
    channel: dict[str, Any],
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    record = dict(channel)
    record.update({"ok": False, "http": None, "mpd": None, "kid": None,
                   "key": None, "license_url": None, "no_drm": False, "error": None})

    try:
        status, html = get_text(session, channel["player_url"], timeout, retries)
        record["http"] = status
        if status != 200:
            record["error"] = f"HTTP {status}"
            return record

        blob = html
        script_url = channel["player_url"].rstrip("/") + "/js/inline_scripts.js"
        try:
            js_status, js = get_text(session, script_url, timeout, 1)
            if js_status == 200 and js:
                blob = html + "\n" + js
        except Exception:
            pass

        found = extract(blob)
        record.update(found)
        record["ok"] = bool(found["mpd"] or found["kid"] or found["key"])
        if not record["ok"]:
            record["error"] = "no stream data in response"
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"

    return record


# --- output ---------------------------------------------------------------

def print_record(record: dict[str, Any]) -> None:
    print("=" * 74)
    print(f"ID    : {record['id']}")
    print(f"NAME  : {record['title']}")
    print(f"LOGO  : {record['logo'] or '-'}")
    print(f"GENRE : {record['genre'] or '-'} | LANG: {record['language'] or '-'}")
    print(f"ROUTE : {record['player_url']}")
    print(f"HTTP  : {record['http']}")
    print(f"MPD   : {record.get('mpd') or '-'}")
    print(f"KID   : {record.get('kid') or '-'}")
    print(f"KEY   : {record.get('key') or '-'}")
    if record.get("license_url"):
        print(f"LIC   : {record['license_url']}")
    if record.get("no_drm"):
        print("DRM   : none (clear)")
    if record.get("error"):
        print(f"ERR   : {record['error']}")
    print()


def build_m3u(records: list[dict[str, Any]], base: str) -> str:
    lines = [f'#EXTM3U name="CineWW Channels"']
    for record in records:
        if not record.get("mpd"):
            continue
        lines.append(
            f'#EXTINF:-1 tvg-id="{record["id"]}" tvg-name="{record["title"]}" '
            f'tvg-logo="{record["logo"]}" group-title="{record["genre"] or "Live"}",{record["title"]}'
        )
        if record.get("kid") and record.get("key"):
            lines.append("#KODIPROP:inputstream.adaptive.license_type=org.w3.clearkey")
            lines.append(f'#KODIPROP:inputstream.adaptive.license_key={record["kid"]}:{record["key"]}')
        lines.append(record["mpd"])
    return "\n".join(lines) + "\n"


def build_txt(records: list[dict[str, Any]]) -> str:
    out = []
    for record in records:
        flag = "OK  " if record.get("ok") else "FAIL"
        out.append(
            f"[{flag}] {record['id']} | {record['title']} | mpd={record.get('mpd') or '-'} "
            f"| kid={record.get('kid') or '-'} | key={record.get('key') or '-'}"
            + (f" | err={record['error']}" if record.get("error") else "")
        )
    return "\n".join(out) + "\n"


# --- main -----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Extract mpd/kid/key from CineWW player routes")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--id", dest="ids", action="append", default=[])
    parser.add_argument("--ids", dest="ids_csv", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="output")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--no-m3u", action="store_true")
    parser.add_argument("--fail-on-empty", action="store_true")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    wanted = [value.strip() for value in args.ids if value.strip()]
    wanted += [value.strip() for value in args.ids_csv.split(",") if value.strip()]

    session = make_session()
    channels = load_channels(session, base, args.timeout, args.retries)

    if wanted:
        wanted_lower = {value.lower() for value in wanted}
        channels = [ch for ch in channels if ch["id"].lower() in wanted_lower]
        missing = wanted_lower - {ch["id"].lower() for ch in channels}
        if missing:
            print(f"[warn] not in channel list: {', '.join(sorted(missing))}", file=sys.stderr)

    if args.limit > 0:
        channels = channels[: args.limit]

    if not channels:
        print("no channels to probe", file=sys.stderr)
        return 1 if args.fail_on_empty else 0

    print(f"probing {len(channels)} channel(s) on {base}")
    print(f"concurrency={args.concurrency} timeout={args.timeout}s retries={args.retries}\n")

    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {
            pool.submit(probe_channel, session, channel, args.timeout, args.retries): channel
            for channel in channels
        }
        for future in as_completed(futures):
            records.append(future.result())

    records.sort(key=lambda item: item["index"])

    for record in records:
        print_record(record)

    ok = [record for record in records if record.get("ok")]
    with_mpd = [record for record in records if record.get("mpd")]
    with_keys = [record for record in records if record.get("kid") and record.get("key")]

    print("-" * 74)
    print(f"total={len(records)} parsed={len(ok)} mpd={len(with_mpd)} kid+key={len(with_keys)}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": base,
        "count": len(records),
        "parsed": len(ok),
        "channels": records,
    }
    (out_dir / "streams.json").write_text(
        json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / "streams.txt").write_text(build_txt(records), encoding="utf-8")

    if not args.no_m3u:
        (out_dir / "channels.m3u").write_text(build_m3u(records, base), encoding="utf-8")

    print(f"written: {out_dir/'streams.json'} {out_dir/'streams.txt'}"
          + ("" if args.no_m3u else f" {out_dir/'channels.m3u'}"))

    if args.fail_on_empty and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
