#!/usr/bin/env python3
"""
New Sounds podcast feed generator.

Strategy:
  1. Fetch the WNYC New Sounds listing page HTML
  2. Extract episode numbers from heading text (e.g. "#5144, Mixed Messages")
  3. For each episode number, query the WNYC story API for metadata + audio URL
  4. Write a valid podcast RSS feed to feed.xml

Episode numbers are cached in episode_cache.json so the API is only hit
once per episode. The cache also stores the full episode metadata, so
re-runs are fast even for large backlogs.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Configuration ─────────────────────────────────────────────────────────────

LISTING_URL = "https://www.wnyc.org/browse/shows/new-sounds"
API_BASE    = "https://api.wnyc.org/api/v1/story"
FEED_FILE   = "feed.xml"
CACHE_FILE  = "episode_cache.json"

FEED_TITLE       = "New Sounds (WNYC)"
FEED_DESCRIPTION = ("New York Public Radio's home for the musically curious "
                    "since 1982. Genre-free music hosted by John Schaefer.")
FEED_LINK        = "https://www.wnyc.org/browse/shows/new-sounds"
FEED_IMAGE       = "https://media.wnyc.org/i/1860/1860/c/80/2024/12/new_sounds_logo.png"
FEED_AUTHOR      = "WNYC / New York Public Radio"
FEED_EMAIL       = "hello@newsounds.org"

REQUEST_DELAY    = 1.5   # seconds between API calls — be polite to WNYC
HEADERS          = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Cache helpers ─────────────────────────────────────────────────────────────

def load_cache():
    if Path(CACHE_FILE).exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


# ── Step 1: get episode numbers from listing page ────────────────────────────

def get_episode_numbers():
    """
    Fetch the WNYC listing page and extract episode numbers from
    heading text like "#5144, Mixed Messages" or "# 4893, Music for..."
    Returns a list of int episode numbers, most recent first.
    """
    print(f"Fetching episode listing from {LISTING_URL} ...")
    resp = requests.get(LISTING_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    numbers = []
    seen = set()

    # Look for headings that contain the episode number pattern
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "li", "span", "p", "div"]):
        text = tag.get_text(" ", strip=True)
        # Matches "#5144", "# 4893", "#4721" etc.
        m = re.search(r"#\s*(\d{4,5})", text)
        if m:
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                numbers.append(n)

    print(f"  Found {len(numbers)} episode numbers: {numbers[:5]}{'...' if len(numbers) > 5 else ''}")
    return numbers


# ── Step 2: fetch episode metadata from WNYC API ─────────────────────────────

def _deep_get(d, *keys):
    """Safely traverse nested dicts."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def fetch_episode(number, cache):
    """
    Fetch metadata + audio URL for a single episode from the WNYC API.
    Returns a dict or None if no audio is available.
    Results are cached so each episode is only fetched once.
    """
    key = str(number)
    if key in cache:
        return cache[key]

    # The WNYC story API slug for New Sounds episodes follows this pattern
    slug = f"new-sounds-{number}"
    url  = f"{API_BASE}/{slug}/"

    time.sleep(REQUEST_DELAY)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        print(f"  Network error for episode {number}: {e}")
        return None

    if resp.status_code == 404:
        print(f"  Episode {number}: not found in API (404), skipping")
        return None
    if not resp.ok:
        print(f"  Episode {number}: API returned {resp.status_code}, skipping")
        return None

    try:
        data = resp.json()
    except Exception:
        print(f"  Episode {number}: could not parse API response as JSON")
        return None

    # Audio URL may live in several places depending on API version
    audio_url = (
        data.get("audio")
        or data.get("audio_url")
        or _deep_get(data, "audio_info", "url")
        or _deep_get(data, "attributes", "audio")
        or _deep_get(data, "attributes", "audio_url")
        or _deep_get(data, "attributes", "audio_info", "url")
    )

    if not audio_url:
        print(f"  Episode {number}: no audio URL found, skipping")
        return None

    # Normalise publication date to RFC 2822 for RSS
    raw_date = (
        data.get("publish_at")
        or data.get("date_published")
        or data.get("newsdate")
        or ""
    )
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
    except Exception:
        pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    # Image: prefer episode art, fall back to show image
    image = (
        _deep_get(data, "image", "url")
        or data.get("image")
        or data.get("thumbnail")
        or FEED_IMAGE
    )
    if isinstance(image, str) and image.startswith("/"):
        image = "https://media.wnyc.org" + image

    episode = {
        "number":           number,
        "title":            data.get("title") or f"New Sounds #{number}",
        "description":      data.get("body") or data.get("tease") or "",
        "url":              (data.get("url")
                             or f"https://www.wnycstudios.org/podcasts/newsounds/episodes/{slug}"),
        "audio_url":        audio_url,
        "pub_date":         pub_date,
        "image":            image if isinstance(image, str) else FEED_IMAGE,
        "duration_seconds": data.get("audio_duration_seconds") or 0,
    }

    cache[key] = episode
    print(f"  Fetched episode {number}: {episode['title'][:60]}")
    return episode


# ── Step 3: build RSS ─────────────────────────────────────────────────────────

def format_duration(seconds):
    try:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"
    except Exception:
        return "00:00:00"


def build_rss(episodes):
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ET.register_namespace("atom",   "http://www.w3.org/2005/Atom")

    rss = ET.Element("rss", {
        "version":        "2.0",
        "xmlns:itunes":   "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "xmlns:atom":     "http://www.w3.org/2005/Atom",
    })
    channel = ET.SubElement(rss, "channel")

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, tag, attrs)
        if text is not None:
            el.text = text
        return el

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    sub(channel, "title",           FEED_TITLE)
    sub(channel, "link",            FEED_LINK)
    sub(channel, "description",     FEED_DESCRIPTION)
    sub(channel, "language",        "en-us")
    sub(channel, "lastBuildDate",   now)
    sub(channel, "itunes:author",   FEED_AUTHOR)
    sub(channel, "itunes:summary",  FEED_DESCRIPTION)
    sub(channel, "itunes:explicit", "no")

    owner = sub(channel, "itunes:owner")
    sub(owner, "itunes:name",  FEED_AUTHOR)
    sub(owner, "itunes:email", FEED_EMAIL)

    sub(channel, "itunes:image", href=FEED_IMAGE)
    chan_img = sub(channel, "image")
    sub(chan_img, "url",   FEED_IMAGE)
    sub(chan_img, "title", FEED_TITLE)
    sub(chan_img, "link",  FEED_LINK)

    ET.SubElement(channel, "itunes:category", {"text": "Music"})

    for ep in episodes:
        item = sub(channel, "item")
        sub(item, "title",              ep["title"])
        sub(item, "link",               ep["url"])
        sub(item, "guid",               ep["url"], isPermaLink="true")
        sub(item, "pubDate",            ep["pub_date"])
        sub(item, "description",        ep["description"])
        sub(item, "itunes:summary",     ep["description"])
        sub(item, "itunes:author",      FEED_AUTHOR)
        sub(item, "itunes:duration",    format_duration(ep["duration_seconds"]))
        sub(item, "enclosure",
            url    = ep["audio_url"],
            length = "0",
            type   = "audio/mpeg")
        ep_img = ep.get("image") or FEED_IMAGE
        if ep_img != FEED_IMAGE:
            sub(item, "itunes:image", href=ep_img)

    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cache   = load_cache()
    numbers = get_episode_numbers()

    if not numbers:
        print("No episode numbers found — the page structure may have changed.")
        print("The feed.xml will not be updated.")
        return

    episodes  = []
    new_count = 0

    for number in numbers:
        was_cached = str(number) in cache
        ep = fetch_episode(number, cache)
        if ep:
            episodes.append(ep)
            if not was_cached:
                new_count += 1

    save_cache(cache)

    if not episodes:
        print("No episodes with audio found — feed not written.")
        return

    # Sort newest first by episode number (higher = newer)
    episodes.sort(key=lambda e: e["number"], reverse=True)

    print(f"\nBuilding RSS feed: {len(episodes)} episodes ({new_count} newly fetched)...")
    xml_str = build_rss(episodes)

    with open(FEED_FILE, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"✓ Feed written to {FEED_FILE}")


if __name__ == "__main__":
    main()
