#!/usr/bin/env python3
"""
New Sounds podcast feed generator.
Scrapes episode listings from wnyc.org and fetches audio URLs
from the WNYC publisher API, then writes a valid podcast RSS feed.
"""

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Configuration ────────────────────────────────────────────────────────────

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

# How long to wait between API requests (be polite to WNYC's servers)
REQUEST_DELAY = 1.5  # seconds

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_cache():
    if Path(CACHE_FILE).exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def get_episode_slugs():
    """
    Parse the WNYC New Sounds browse page for episode slugs.
    Returns a list of slug strings like '4721-ostinati'.
    """
    print("Fetching episode listing from wnyc.org...")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewSoundsFeedBot/1.0)"}
    resp = requests.get(LISTING_URL, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    slugs = []
    seen = set()

    # Episode links follow the pattern /podcasts/newsounds/episodes/{slug}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/podcasts/newsounds/episodes/([\w-]+)", href)
        if m:
            slug = m.group(1)
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)

    # Also check wnyc.org story links as a fallback
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/story/(new-sounds-[\w-]+)", href)
        if m:
            slug = m.group(1)
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)

    print(f"  Found {len(slugs)} episode slugs.")
    return slugs


def fetch_episode_data(slug, cache):
    """
    Fetch episode metadata + audio URL from the WNYC API.
    Returns a dict with keys: title, description, url, audio_url,
    pub_date, image, duration_seconds.
    Returns None if the episode has no audio.
    """
    if slug in cache:
        return cache[slug]

    # Try the wnycstudios slug format first, then the wnyc story format
    urls_to_try = [
        f"{API_BASE}/{slug}/",
    ]
    # If slug starts with a number it's a wnycstudios episode slug;
    # also try the wnyc.org story variant
    if not slug.startswith("new-sounds"):
        urls_to_try.append(f"{API_BASE}/new-sounds-{slug}/")

    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewSoundsFeedBot/1.0)"}

    data = None
    for api_url in urls_to_try:
        try:
            time.sleep(REQUEST_DELAY)
            resp = requests.get(api_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                break
            elif resp.status_code == 404:
                continue
        except Exception as e:
            print(f"  Warning: could not fetch {api_url}: {e}")
            continue

    if not data:
        print(f"  Skipping {slug} (no API data found)")
        return None

    # The WNYC API returns audio info in a few possible locations
    audio_url = (
        data.get("audio")
        or data.get("audio_url")
        or (data.get("audio_info") or {}).get("url")
        or None
    )

    if not audio_url:
        # Try nested under 'attributes'
        attrs = data.get("attributes", {})
        audio_url = (
            attrs.get("audio")
            or attrs.get("audio_url")
            or (attrs.get("audio_info") or {}).get("url")
            or None
        )

    if not audio_url:
        print(f"  Skipping {slug} (no audio URL in API response)")
        return None

    # Normalise pub_date to RFC 2822 for RSS
    raw_date = data.get("publish_at") or data.get("date_published") or ""
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
    except Exception:
        pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    image = (
        data.get("image")
        or data.get("thumbnail")
        or FEED_IMAGE
    )
    # WNYC image URLs are often paths; make them absolute
    if image and image.startswith("/"):
        image = "https://media.wnyc.org" + image

    episode = {
        "slug":             slug,
        "title":            data.get("title", slug),
        "description":      data.get("body", data.get("tease", "")),
        "url":              data.get("url", f"https://www.wnycstudios.org/podcasts/newsounds/episodes/{slug}"),
        "audio_url":        audio_url,
        "pub_date":         pub_date,
        "image":            image,
        "duration_seconds": data.get("audio_duration_seconds", 0),
    }

    cache[slug] = episode
    return episode


def format_duration(seconds):
    """Convert seconds to HH:MM:SS for iTunes duration tag."""
    try:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"
    except Exception:
        return "00:00:00"


def build_rss(episodes):
    """Build a podcast-compatible RSS XML string from a list of episode dicts."""

    # Register iTunes namespace
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ET.register_namespace("atom",   "http://www.w3.org/2005/Atom")

    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "xmlns:atom":   "http://www.w3.org/2005/Atom",
    })
    channel = ET.SubElement(rss, "channel")

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, tag, attrs)
        if text is not None:
            el.text = text
        return el

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    sub(channel, "title",          FEED_TITLE)
    sub(channel, "link",           FEED_LINK)
    sub(channel, "description",    FEED_DESCRIPTION)
    sub(channel, "language",       "en-us")
    sub(channel, "lastBuildDate",  now)
    sub(channel, "itunes:author",  FEED_AUTHOR)
    sub(channel, "itunes:summary", FEED_DESCRIPTION)
    sub(channel, "itunes:explicit","no")

    owner = sub(channel, "itunes:owner")
    sub(owner, "itunes:name",  FEED_AUTHOR)
    sub(owner, "itunes:email", FEED_EMAIL)

    img = sub(channel, "itunes:image", href=FEED_IMAGE)
    chan_img = sub(channel, "image")
    sub(chan_img, "url",   FEED_IMAGE)
    sub(chan_img, "title", FEED_TITLE)
    sub(chan_img, "link",  FEED_LINK)

    ET.SubElement(channel, "itunes:category", {"text": "Music"})

    for ep in episodes:
        item = sub(channel, "item")
        sub(item, "title",       ep["title"])
        sub(item, "link",        ep["url"])
        sub(item, "guid",        ep["url"], isPermaLink="true")
        sub(item, "pubDate",     ep["pub_date"])
        sub(item, "description", ep["description"])
        sub(item, "itunes:summary", ep["description"])
        sub(item, "itunes:author",  FEED_AUTHOR)
        sub(item, "itunes:duration", format_duration(ep["duration_seconds"]))
        sub(item, "enclosure",
            url=ep["audio_url"],
            length="0",
            type="audio/mpeg")
        if ep.get("image") and ep["image"] != FEED_IMAGE:
            sub(item, "itunes:image", href=ep["image"])

    # Pretty-print
    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    cache = load_cache()
    slugs = get_episode_slugs()

    if not slugs:
        print("No episodes found — the listing page structure may have changed.")
        return

    episodes = []
    new_count = 0

    for slug in slugs:
        was_cached = slug in cache
        ep = fetch_episode_data(slug, cache)
        if ep:
            episodes.append(ep)
            if not was_cached:
                new_count += 1

    save_cache(cache)

    if not episodes:
        print("No episodes with audio found.")
        return

    print(f"\nBuilding RSS feed with {len(episodes)} episodes ({new_count} new)...")
    xml_str = build_rss(episodes)

    with open(FEED_FILE, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"Feed written to {FEED_FILE}")


if __name__ == "__main__":
    main()
