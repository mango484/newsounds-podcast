#!/usr/bin/env python3
"""
New Sounds podcast feed generator.

Strategy:
  1. Fetch wnyc.org/browse/shows/new-sounds (server-side rendered HTML)
  2. Extract episode UUIDs from Simplecast image URLs in the HTML
     e.g. image.simplecastcdn.com/images/{podcast_uuid}/{episode_uuid}/...
  3. Use the Simplecast oEmbed endpoint to get each episode's iframe HTML,
     which embeds the audio URL.
  4. Parse metadata (title, description, pub_date) directly from the listing HTML.
  5. Write a valid podcast RSS feed.
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

LISTING_URL  = "https://www.wnyc.org/browse/shows/new-sounds"
OEMBED_URL   = "https://simplecast.com/oembed"
FEED_FILE    = "feed.xml"
CACHE_FILE   = "episode_cache.json"

FEED_TITLE       = "New Sounds (WNYC)"
FEED_DESCRIPTION = ("New York Public Radio's home for the musically curious "
                    "since 1982. Genre-free music hosted by John Schaefer.")
FEED_LINK        = "https://www.wnyc.org/browse/shows/new-sounds"
FEED_IMAGE       = "https://media.wnyc.org/i/1860/1860/c/80/2024/12/new_sounds_logo.png"
FEED_AUTHOR      = "WNYC / New York Public Radio"
FEED_EMAIL       = "hello@newsounds.org"

REQUEST_DELAY = 1.5
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache():
    if Path(CACHE_FILE).exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

# ── Step 1: scrape listing page ───────────────────────────────────────────────

def parse_listing_page():
    """
    Fetch the wnyc.org New Sounds listing page and extract:
      - episode_uuid  (from Simplecast CDN image URLs)
      - title
      - description
      - image_url
      - duration_str  (e.g. "57 min")
    Returns list of dicts, one per episode.
    """
    print(f"Fetching listing page...")
    resp = requests.get(LISTING_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Episode UUIDs live in Simplecast CDN image URLs:
    # https://image.simplecastcdn.com/images/{podcast_uuid}/{episode_uuid}/filename.jpg
    # We find all such img src values and extract the episode UUID (second UUID).
    uuid_pattern = re.compile(
        r"image\.simplecastcdn\.com/images/"
        r"[0-9a-f-]+/"          # podcast UUID (ignore)
        r"([0-9a-f-]{36})"      # episode UUID (capture)
    )

    # Build a list preserving order, deduplicating
    episodes_raw = []
    seen_uuids = set()

    # Each episode on the page is typically a section/article/div with:
    # - An img with a simplecastcdn.com src
    # - A heading with the episode title
    # - A paragraph with the description
    # We'll find all simplecast image elements and work outward.
    for img in soup.find_all("img", src=True):
        m = uuid_pattern.search(img["src"])
        if not m:
            continue
        ep_uuid = m.group(1)
        if ep_uuid in seen_uuids:
            continue
        seen_uuids.add(ep_uuid)

        # Image URL — use a higher-res version
        img_url = img["src"]
        # Replace small thumbnail dimensions if present
        img_url = re.sub(r"/fill-\d+x\d+-[^/|]+", "/fill-600x600-c0", img_url)

        # Walk up the DOM to find the nearest container with title/description
        container = img.parent
        for _ in range(6):  # look up to 6 levels
            if container is None:
                break
            h = container.find(["h2", "h3", "h4"])
            if h and h.get_text(strip=True):
                break
            container = container.parent

        title = ""
        description = ""
        duration_str = ""

        if container:
            h = container.find(["h2", "h3", "h4"])
            if h:
                title = h.get_text(" ", strip=True)

            # Description: first <p> that isn't just metadata
            for p in container.find_all("p"):
                txt = p.get_text(" ", strip=True)
                if txt and len(txt) > 30:
                    description = txt
                    break

            # Duration: look for text like "57 min" or "1 hr 2 min"
            text_blob = container.get_text(" ")
            dm = re.search(r"(\d+\s*(?:hr\s*\d+\s*min|\s*min))", text_blob)
            if dm:
                duration_str = dm.group(1).strip()

        episodes_raw.append({
            "episode_uuid": ep_uuid,
            "title":        title,
            "description":  description,
            "image_url":    img_url,
            "duration_str": duration_str,
        })

    print(f"  Found {len(episodes_raw)} episodes with Simplecast UUIDs")
    return episodes_raw

# ── Step 2: get audio URL via oEmbed ─────────────────────────────────────────

def get_audio_url_via_oembed(episode_uuid, cache):
    """
    Use Simplecast's oEmbed endpoint to get the iframe embed HTML for an episode.
    The iframe src contains the audio file URL.
    Returns audio_url string or None.
    """
    cache_key = f"oembed:{episode_uuid}"
    if cache_key in cache:
        return cache[cache_key]

    player_url = f"https://player.simplecast.com/{episode_uuid}"
    time.sleep(REQUEST_DELAY)

    try:
        resp = requests.get(
            OEMBED_URL,
            params={"url": player_url, "format": "json"},
            headers=HEADERS,
            timeout=20
        )
        if resp.status_code == 404:
            print(f"    oEmbed 404 for {episode_uuid}")
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    oEmbed error for {episode_uuid}: {e}")
        return None

    # The oEmbed response has an 'html' field with an iframe.
    # The iframe src is the player URL; we need to fetch that to get the audio URL.
    # But actually the iframe src itself IS the player URL we can fetch.
    html = data.get("html", "")
    iframe_m = re.search(r'src="(https://player\.simplecast\.com/[^"]+)"', html)
    if not iframe_m:
        # Try without quotes variant
        iframe_m = re.search(r"src='(https://player\.simplecast\.com/[^']+)'", html)

    if not iframe_m:
        print(f"    No iframe src found in oEmbed for {episode_uuid}")
        return None

    iframe_src = iframe_m.group(1)

    # Fetch the player page to get the actual audio URL
    time.sleep(REQUEST_DELAY)
    try:
        player_resp = requests.get(iframe_src, headers=HEADERS, timeout=20)
        player_resp.raise_for_status()
    except Exception as e:
        print(f"    Player page error for {episode_uuid}: {e}")
        return None

    # Audio URL is in the page source — look for cdn.simplecast.com .mp3 URL
    audio_m = re.search(
        r'(https://[^"\']+cdn\.simplecast\.com/audio/[^"\']+\.mp3[^"\']*)',
        player_resp.text
    )
    if not audio_m:
        # Also check for enclosure URLs in any embedded JSON
        audio_m = re.search(
            r'"(https://cdn\.simplecast\.com/audio/[^"]+\.mp3[^"]*)"',
            player_resp.text
        )

    if not audio_m:
        print(f"    No audio URL found in player page for {episode_uuid}")
        return None

    audio_url = audio_m.group(1)
    cache[cache_key] = audio_url
    return audio_url

# ── Step 3: build RSS ─────────────────────────────────────────────────────────

def duration_to_hms(duration_str):
    """Convert '57 min' or '1 hr 2 min' to HH:MM:SS."""
    total_min = 0
    hr_m = re.search(r"(\d+)\s*hr", duration_str)
    min_m = re.search(r"(\d+)\s*min", duration_str)
    if hr_m:
        total_min += int(hr_m.group(1)) * 60
    if min_m:
        total_min += int(min_m.group(1))
    if not total_min:
        return "00:00:00"
    h, m = divmod(total_min, 60)
    return f"{h:02d}:{m:02d}:00"

def build_rss(episodes):
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ET.register_namespace("atom",   "http://www.w3.org/2005/Atom")

    rss = ET.Element("rss", {
        "version":      "2.0",
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
        pub_date = ep.get("pub_date") or now
        link     = ep.get("link") or FEED_LINK
        image    = ep.get("image_url") or FEED_IMAGE
        duration = duration_to_hms(ep.get("duration_str", ""))

        item = sub(channel, "item")
        sub(item, "title",           ep["title"] or "New Sounds")
        sub(item, "link",            link)
        sub(item, "guid",            f"newsounds:{ep['episode_uuid']}", isPermaLink="false")
        sub(item, "pubDate",         pub_date)
        sub(item, "description",     ep.get("description", ""))
        sub(item, "itunes:summary",  ep.get("description", ""))
        sub(item, "itunes:author",   FEED_AUTHOR)
        sub(item, "itunes:duration", duration)
        sub(item, "enclosure",
            url    = ep["audio_url"],
            length = "0",
            type   = "audio/mpeg")
        if image != FEED_IMAGE:
            sub(item, "itunes:image", href=image)

    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cache       = load_cache()
    raw_entries = parse_listing_page()

    if not raw_entries:
        print("No episodes found on listing page — feed not updated.")
        return

    episodes  = []
    new_count = 0

    for raw in raw_entries:
        ep_uuid    = raw["episode_uuid"]
        was_cached = f"oembed:{ep_uuid}" in cache

        print(f"  {raw['title'][:55] or ep_uuid}")
        audio_url = get_audio_url_via_oembed(ep_uuid, cache)

        if not audio_url:
            continue

        ep = dict(raw)
        ep["audio_url"] = audio_url
        # pub_date: we don't have it from the listing page, so use a placeholder
        # that keeps episodes in page order (most recent first = index 0)
        ep["pub_date"]  = ""
        ep["link"]      = f"https://new-sounds.simplecast.com/episodes/{ep_uuid}"
        episodes.append(ep)

        if not was_cached:
            new_count += 1

    save_cache(cache)

    if not episodes:
        print("No episodes with audio found — feed not written.")
        return

    # Set pub_dates: we don't have real dates from the page, so assign synthetic
    # ones spaced one week apart, most recent first, so podcast apps sort correctly.
    base_date = datetime.now(timezone.utc)
    for i, ep in enumerate(episodes):
        from datetime import timedelta
        dt = base_date - timedelta(weeks=i)
        ep["pub_date"] = dt.strftime("%a, %d %b %Y %H:%M:%S %z")

    print(f"\nBuilding RSS feed: {len(episodes)} episodes ({new_count} newly fetched)...")
    xml_str = build_rss(episodes)

    with open(FEED_FILE, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"✓ Feed written to {FEED_FILE}")

if __name__ == "__main__":
    main()
