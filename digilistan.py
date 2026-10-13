#!/usr/bin/env python3
"""Hämtar senaste Digilistan från Sveriges Radio och lägger låtarna i
spellistan "Digilistan" på YouTube Music."""

import argparse
import sys
import time
from contextlib import suppress
from pathlib import Path

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from ytmusicapi import YTMusic

SR_PROGRAM_ID = 2697  # Digilistan i P3
SR_API = "https://api.sr.se/api/v2"
PLAYLIST_NAME = "Digilistan"
PROFILE_DIR = Path(__file__).with_name("browser-profile")
YTM_URL = "https://music.youtube.com"
LOGIN_COOKIE = "__Secure-3PAPISID"
LOGIN_TIMEOUT = 300  # sekunder
EPISODES_TO_TRY = 4  # hur många avsnitt bakåt vi letar efter en låtlista


def fetch_digilistan():
    """Returnerar (avsnittstitel, [(artist, titel), ...]) med etta först.

    Tar senaste avsnittet som har en låtlista, eftersom listan för ett nyss
    sänt avsnitt inte alltid är publicerad än."""
    episodes = requests.get(
        f"{SR_API}/episodes/index",
        params={"programid": SR_PROGRAM_ID, "format": "json", "size": EPISODES_TO_TRY},
        timeout=30,
    ).json()["episodes"]

    for episode in episodes:
        songs = requests.get(
            f"{SR_API}/playlists/getplaylistbyepisodeid",
            params={"id": episode["id"], "format": "json"},
            timeout=30,
        ).json().get("song", [])
        if songs:
            break
        print(f"Ingen låtlista för '{episode['title']}' än, provar föregående avsnitt.")
    else:
        sys.exit("Hittade ingen låtlista hos Sveriges Radio, spellistan lämnas orörd.")

    # Listan räknas ner i sändning, så den senast spelade låten är etta.
    songs.sort(key=lambda s: s["starttimeutc"], reverse=True)

    tracks, seen = [], set()
    for s in songs:
        key = (s["artist"].lower(), s["title"].lower())
        if key not in seen:
            seen.add(key)
            tracks.append((s["artist"], s["title"]))
    return episode["title"], tracks


def login_value(cookies):
    return next((c["value"] for c in cookies if c["name"] == LOGIN_COOKIE), None)


def read_cookies(headless, stale=None):
    """Öppnar Chromium med den sparade profilen och returnerar YouTube-cookies.

    I synligt läge väntar den tills användaren har loggat in. En utgången
    inloggning (stale) räknas inte, utan då väntar den på en ny."""
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            # Utan detta vägrar Google ibland inloggning i automatiserade webbläsare.
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(YTM_URL)

        deadline = time.time() + (0 if headless else LOGIN_TIMEOUT)
        try:
            while True:
                cookies = context.cookies(YTM_URL)
                value = login_value(cookies)
                if value is not None and value != stale:
                    return cookies
                if time.time() > deadline:
                    return None
                page.wait_for_timeout(1000)
        except PlaywrightError:
            # Användaren stängde fönstret.
            return None
        finally:
            with suppress(PlaywrightError):
                context.close()


def make_ytmusic(cookies):
    return YTMusic({
        "cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
        "x-goog-authuser": "0",
        "origin": YTM_URL,
        # Räknas om av ytmusicapi vid varje anrop, men måste finnas.
        "authorization": "SAPISIDHASH 0",
    })


def login(stale=None):
    print("Logga in på YouTube Music i webbläsarfönstret som öppnas...")
    cookies = read_cookies(headless=False, stale=stale)
    if cookies is None:
        sys.exit("Ingen inloggning gjordes, avbryter.")
    return make_ytmusic(cookies)


def connect_ytmusic():
    cookies = read_cookies(headless=True)
    if cookies is None:
        return login()
    yt = make_ytmusic(cookies)
    try:
        yt.get_library_playlists(limit=1)
    except Exception:
        # Cookies finns men sessionen har gått ut.
        return login(stale=login_value(cookies))
    return yt


def find_video_id(yt, artist, title):
    results = yt.search(f"{artist} {title}", filter="songs", limit=5)
    for r in results:
        if r.get("videoId"):
            return r["videoId"]
    return None


def get_or_create_playlist(yt, description):
    for p in yt.get_library_playlists(limit=None):
        if p["title"] == PLAYLIST_NAME:
            return p["playlistId"]
    print(f"Skapar spellistan '{PLAYLIST_NAME}'")
    return yt.create_playlist(PLAYLIST_NAME, description, privacy_status="PRIVATE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="visa låtarna utan att ändra något på YouTube Music")
    args = parser.parse_args()

    episode_title, tracks = fetch_digilistan()
    print(f"{episode_title}: {len(tracks)} låtar")
    for i, (artist, title) in enumerate(tracks, 1):
        print(f"{i:3}. {artist} - {title}")

    if args.dry_run:
        return

    yt = connect_ytmusic()

    video_ids, missing = [], []
    for artist, title in tracks:
        vid = find_video_id(yt, artist, title)
        if vid:
            video_ids.append(vid)
        else:
            missing.append(f"{artist} - {title}")

    if not video_ids:
        sys.exit("Inga låtar hittades på YouTube Music, spellistan lämnas orörd.")

    description = f"{episode_title} (Sveriges Radio P3)"
    playlist_id = get_or_create_playlist(yt, description)

    # Byt ut innehållet så att spellistan alltid speglar veckans lista.
    try:
        current = yt.get_playlist(playlist_id, limit=None).get("tracks", [])
    except KeyError:
        # ytmusicapi klarar inte tomma (t.ex. nyss skapade) spellistor.
        current = []
    if current:
        yt.remove_playlist_items(playlist_id, current)
    yt.add_playlist_items(playlist_id, video_ids, duplicates=True)
    yt.edit_playlist(playlist_id, description=description)

    print(f"\nLade till {len(video_ids)} låtar i '{PLAYLIST_NAME}'.")
    if missing:
        print("Hittades inte på YouTube Music:")
        for m in missing:
            print(f"  - {m}")


if __name__ == "__main__":
    main()
