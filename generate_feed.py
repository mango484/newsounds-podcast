#!/usr/bin/env python3
"""
New Sounds podcast feed generator.

Strategy:
  1. Fetch the WNYC Atom feed at wnyc.org/atomfeeds/shows/newsounds
     Each entry contains a per-episode Simplecast feed URL like
     feeds.simplecast.com/{episode-uuid}
  2. Fetch each per-episode Simplecast RSS feed to get the audio
     enclosure URL, duration, pub_date, and description.
  3. Write a valid podcast RSS feed.

Per-episode Simplecast feed URLs are cached so each is only fetched once.
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

REQUEST_DELAY = 1.5

# Rotate through a few realistic user-agent strings
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

def get_headers(index=0):
    return {
        "User-Agent": USER_AGENTS[index % len(USER_AGENTS)],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
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

# ── Step 1: get per-episode Simplecast feed URLs from WNYC Atom feed ──────────

def get_episode_feed_urls():
    """
    Fetch the WNYC Atom feed and extract per-episode Simplecast RSS URLs.
    Each looks like: https://feeds.simplecast.com/5d999f9f-fb0d-4efa-8bf7-0179b04613c4
    Returns list of (title, sc_feed_url, description) tuples.
    """
    print(f"Fetching WNYC Atom feed...")

    # Try a few times with different headers since WNYC blocks some user agents
    resp = None
    for attempt in range(3):
        try:
            time.sleep(attempt * 2)
            r = requests.get(
                ATOM_FEED_URL,
                headers=get_headers(attempt),
                timeout=20,
                allow_redirects=True
            )
            if r.status_code == 200:
                resp = r
                break
            print(f"  Attempt {attempt+1}: HTTP {r.status_code}")
        except requests.RequestException as e:
            print(f"  Attempt {attempt+1} failed: {e}")

    if resp is None:
        print("  Could not fetch Atom feed after 3 attempts.")
        return []

    content_type = resp.headers.get("content-type", "")
    print(f"  Status: {resp.status_code}, Content-Type: {content_type}, Size: {len(resp.content)} bytes")

    # Try to parse as XML (Atom)
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        # Might be HTML — try BeautifulSoup to find Simplecast URLs in the body
        print("  Not XML — scanning for Simplecast URLs in response body...")
        urls = re.findall(r'https://feeds\.simplecast\.com/[\w-]+', resp.text)
        print(f"  Found {len(urls)} Simplecast URLs by regex scan")
        return [("", url, "") for url in dict.fromkeys(urls).keys()]

    # Atom namespace
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = []
    seen = set()

    for entry in root.findall("atom:entry", ns):
        # Title
        title_el = entry.find("atom:title", ns)
        title = title_el.text.strip() if title_el is not None and title_el.text else ""

        # Per-episode Simplecast feed URL — look in all link elements and content
        sc_url = None

        for link_el in entry.findall("atom:link", ns):
            href = link_el.get("href", "")
            if "feeds.simplecast.com" in href:
                sc_url = href
                break

        if not sc_url:
            # Search in content/summary text
            for tag in ["atom:content", "atom:summary"]:
                el = entry.find(tag, ns)
                if el is not None and el.text:
                    m = re.search(r'https://feeds\.simplecast\.com/[\w-]+', el.text)
                    if m:
                        sc_url = m.group(0)
                        break

        if not sc_url or sc_url in seen:
            continue
        seen.add(sc_url)

        # Description
        for tag in ["atom:summary", "atom:content"]:
            el = entry.find(tag, ns)
            if el is not None and el.text:
                desc = re.sub(r"<[^>]+>", "", el.text).strip()
                break
        else:
            desc = ""

        entries.append((title, sc_url, desc))

    print(f"  Found {len(entries)} episode feed URLs")
    return entries

# ── Step 2: fetch audio from per-episode Simplecast RSS feed ──────────────────

def fetch_episode_from_sc_feed(sc_url, fallback_title, fallback_desc, cache):
    """
    Fetch a per-episode Simplecast RSS feed and extract audio + metadata.
    Returns dict or None.
    """
    if sc_url in cache:
        return cache[sc_url]

    time.sleep(REQUEST_DELAY)
    try:
        resp = requests.get(sc_url, headers=get_headers(), timeout=20)
        if resp.status_code == 404:
            print(f"    404 for {sc_url}")
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    Error fetching {sc_url}: {e}")
        return None

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"    XML parse error: {e}")
        return None

    channel = root.find("channel")
    if channel is None:
        return None
    item = channel.find("item")
    if item is None:
        return None

    enclosure = item.find("enclosure")
    audio_url = enclosure.get("url") if enclosure is not None else None
    if not audio_url:
        return None

    ns_itunes = "http://www.itunes.com/dtds/podcast-1.0.dtd"

    dur_el = item.find(f"{{{ns_itunes}}}duration")
    duration = dur_el.text.strip() if dur_el is not None and dur_el.text else "00:00:00"

    img_el = (item.find(f"{{{ns_itunes}}}image")
              or channel.find(f"{{{ns_itunes}}}image"))
    image = img_el.get("href") if img_el is not None else FEED_IMAGE

    pub_el = item.find("pubDate")
    pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

    title_el = item.find("title")
    title = (title_el.text.strip()
             if title_el is not None and title_el.text
             else fallback_title)

    link_el = item.find("link")
    link = (link_el.text.strip()
            if link_el is not None and link_el.text
            else sc_url)

    desc_el = item.find("description") or item.find(f"{{{ns_itunes}}}summary")
    desc = ""
    if desc_el is not None and desc_el.text:
        desc = re.sub(r"<[^>]+>", "", desc_el.text).strip()
    if not desc:
        desc = fallback_desc

    result = {
        "title":     title,
        "link":      link,
        "pub_date":  pub_date,
        "description": desc,
        "audio_url": audio_url,
        "duration":  duration,
        "image":     image or FEED_IMAGE,
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
    entries = get_episode_feed_urls()

    if not entries:
        print("No episode feed URLs found. Exiting without updating feed.")
        return

    episodes  = []
    new_count = 0

    for title, sc_url, desc in entries:
        was_cached = sc_url in cache
        print(f"  {title[:55] or sc_url}")
        ep = fetch_episode_from_sc_feed(sc_url, title, desc, cache)
        if ep:
            episodes.append(ep)
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
