#!/usr/bin/env python3
"""
New Sounds podcast feed generator.

Strategy:
  1. Fetch the WNYC Atom feed at wnyc.org/atomfeeds/shows/newsounds
     Each entry contains a per-episode Simplecast RSS URL in its <link>.
  2. For each entry, fetch that individual Simplecast episode RSS feed
     to get the audio enclosure URL and full metadata.
  3. Assemble everything into a single podcast RSS feed.

The Atom feed returns ~20 episodes. Results are cached so each episode
Simplecast feed is only fetched once. Re-runs are fast.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

ATOM_FEED_URL = "https://www.wnyc.org/atomfeeds/shows/newsounds"
FEED_FILE     = "feed.xml"
CACHE_FILE    = "episode_cache.json"

FEED_TITLE       = "New Sounds (WNYC)"
FEED_DESCRIPTION = ("New York Public Radio's home for the musically curious "
                    "since 1982. Genre-free music hosted by John Schaefer.")
FEED_LINK        = "https://www.wnyc.org/browse/shows/new-sounds"
FEED_IMAGE       = "https://media.wnyc.org/i/1860/1860/c/80/2024/12/new_sounds_logo.png"
FEED_AUTHOR      = "WNYC / New York Public Radio"
FEED_EMAIL       = "hello@newsounds.org"

REQUEST_DELAY = 1.0
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

# ── Step 1: parse WNYC Atom feed ──────────────────────────────────────────────

def get_atom_entries():
    """
    Fetch the WNYC Atom feed and return a list of dicts with:
      - title
      - simplecast_feed_url  (the per-episode Simplecast RSS URL)
      - pub_date
      - description
    """
    print(f"Fetching WNYC Atom feed...")
    resp = requests.get(ATOM_FEED_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    # Parse Atom XML — register namespaces to avoid 'ns0:' prefixes
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "media": "http://search.yahoo.com/mrss/",
    }

    root = ET.fromstring(resp.content)

    entries = []
    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        title = title_el.text.strip() if title_el is not None and title_el.text else ""

        # The per-episode Simplecast feed URL is in a <link> element.
        # There may be several <link> elements; find the one pointing to simplecast.
        sc_url = None
        for link_el in entry.findall("atom:link", ns):
            href = link_el.get("href", "")
            if "simplecast.com" in href:
                sc_url = href
                break

        if not sc_url:
            # Also check plain text content for a simplecast URL
            content_el = entry.find("atom:content", ns)
            if content_el is not None and content_el.text:
                m = re.search(r'https://feeds\.simplecast\.com/[\w-]+', content_el.text)
                if m:
                    sc_url = m.group(0)

        if not sc_url:
            print(f"  Skipping '{title}' — no Simplecast URL found in entry")
            continue

        # Publication date
        pub_el = entry.find("atom:published", ns) or entry.find("atom:updated", ns)
        pub_date_raw = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

        # Summary / description
        summary_el = entry.find("atom:summary", ns) or entry.find("atom:content", ns)
        description = ""
        if summary_el is not None and summary_el.text:
            # Strip any HTML tags from description
            description = re.sub(r"<[^>]+>", "", summary_el.text).strip()

        entries.append({
            "title":               title,
            "simplecast_feed_url": sc_url,
            "pub_date_raw":        pub_date_raw,
            "description":         description,
        })

    print(f"  Found {len(entries)} entries in Atom feed")
    return entries

# ── Step 2: fetch audio URL from per-episode Simplecast feed ─────────────────

def fetch_audio_from_simplecast_feed(sc_url, cache):
    """
    Each entry's Simplecast URL is an RSS feed for that single episode.
    Parse it to extract the audio enclosure URL, duration, and image.
    Returns a dict or None on failure.
    """
    if sc_url in cache:
        return cache[sc_url]

    time.sleep(REQUEST_DELAY)
    try:
        resp = requests.get(sc_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    Network error fetching {sc_url}: {e}")
        return None

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"    XML parse error for {sc_url}: {e}")
        return None

    channel = root.find("channel")
    if channel is None:
        return None

    item = channel.find("item")
    if item is None:
        return None

    # Audio URL from enclosure tag
    enclosure = item.find("enclosure")
    audio_url = enclosure.get("url") if enclosure is not None else None

    if not audio_url:
        return None

    # Duration from itunes:duration
    ns_itunes = "http://www.itunes.com/dtds/podcast-1.0.dtd"
    dur_el = item.find(f"{{{ns_itunes}}}duration")
    duration_str = dur_el.text.strip() if dur_el is not None and dur_el.text else "00:00:00"

    # Episode image (prefer item-level, fall back to channel-level)
    img_el = item.find(f"{{{ns_itunes}}}image")
    if img_el is None:
        img_el = channel.find(f"{{{ns_itunes}}}image")
    image = img_el.get("href") if img_el is not None else FEED_IMAGE

    # pub date from item
    pub_el = item.find("pubDate")
    pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

    # Full description from item
    desc_el = item.find("description") or item.find(f"{{{ns_itunes}}}summary")
    description = ""
    if desc_el is not None and desc_el.text:
        description = re.sub(r"<[^>]+>", "", desc_el.text).strip()

    # Title from item
    title_el = item.find("title")
    title = title_el.text.strip() if title_el is not None and title_el.text else ""

    # Episode page link
    link_el = item.find("link")
    link = link_el.text.strip() if link_el is not None and link_el.text else sc_url

    result = {
        "audio_url":   audio_url,
        "duration":    duration_str,
        "image":       image or FEED_IMAGE,
        "pub_date":    pub_date,
        "description": description,
        "title":       title,
        "link":        link,
    }

    cache[sc_url] = result
    return result

# ── Step 3: build RSS ─────────────────────────────────────────────────────────

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
        item = sub(channel, "item")
        sub(item, "title",           ep["title"])
        sub(item, "link",            ep["link"])
        sub(item, "guid",            ep["link"], isPermaLink="true")
        sub(item, "pubDate",         ep["pub_date"])
        sub(item, "description",     ep["description"])
        sub(item, "itunes:summary",  ep["description"])
        sub(item, "itunes:author",   FEED_AUTHOR)
        sub(item, "itunes:duration", ep["duration"])
        sub(item, "enclosure",
            url    = ep["audio_url"],
            length = "0",
            type   = "audio/mpeg")
        if ep.get("image") and ep["image"] != FEED_IMAGE:
            sub(item, "itunes:image", href=ep["image"])

    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cache   = load_cache()
    entries = get_atom_entries()

    if not entries:
        print("No entries found in Atom feed — feed not updated.")
        return

    episodes  = []
    new_count = 0

    for entry in entries:
        sc_url    = entry["simplecast_feed_url"]
        was_cached = sc_url in cache

        print(f"  Processing: {entry['title'][:60]}")
        ep_data = fetch_audio_from_simplecast_feed(sc_url, cache)

        if not ep_data:
            print(f"    → no audio found, skipping")
            continue

        # Merge: prefer richer data from Simplecast RSS, fall back to Atom entry
        episode = {
            "title":       ep_data["title"] or entry["title"],
            "link":        ep_data["link"],
            "pub_date":    ep_data["pub_date"] or entry.get("pub_date_raw", ""),
            "description": ep_data["description"] or entry.get("description", ""),
            "audio_url":   ep_data["audio_url"],
            "duration":    ep_data["duration"],
            "image":       ep_data["image"],
        }
        episodes.append(episode)
        if not was_cached:
            new_count += 1

    save_cache(cache)

    if not episodes:
        print("No episodes with audio found — feed not written.")
        return

    print(f"\nBuilding RSS feed: {len(episodes)} episodes ({new_count} newly fetched)...")
    xml_str = build_rss(episodes)

    with open(FEED_FILE, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"✓ Feed written to {FEED_FILE}")

if __name__ == "__main__":
    main()
