#!/usr/bin/env python3
"""Hämtar senaste Digilistan från Sveriges Radio och lägger låtarna i
spellistan "Digilistan" på YouTube Music."""

import argparse
import re
import sys
import time
from contextlib import suppress
from difflib import SequenceMatcher
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
SEARCH_RESULTS = 5  # hur många sökträffar vi jämför
WARN_BELOW = 80  # procent; träffar under detta markeras med !
VERSION_PENALTY = 0.95  # titeln stämmer bara om man bortser från (Radio Edit) o.d.


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


def normalize(text):
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def strip_version(title):
    """Tar bort t.ex. "(Radio Edit)", "[Remix]" och "- Live" från en titel."""
    return re.sub(r"\s*(\(.*?\)|\[.*?\]|\s-\s.*)", "", title)


def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def split_artists(artists):
    parts = re.split(r",|&| feat\.? | ft\.? | x | och ", artists, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def title_score(wanted, found):
    exact = similarity(wanted, found)
    if exact == 1:
        return 1.0
    return max(exact, VERSION_PENALTY * similarity(strip_version(wanted), strip_version(found)))


def artist_score(wanted, found):
    """Hur väl artistlistorna täcker varandra, åt båda hållen."""
    if not wanted or not found:
        return 0.0
    best = [max(similarity(w, f) for f in found) for w in wanted]
    best += [max(similarity(f, w) for w in wanted) for f in found]
    return sum(best) / len(best)


def match_score(artist, title, result):
    found_artists = [a for r in result.get("artists") or [] for a in split_artists(r["name"])]
    return title_score(title, result.get("title", "")) * artist_score(split_artists(artist), found_artists)


def find_song(yt, artist, title):
    """Returnerar (sökträff, matchning 0–100) för den bästa träffen, eller (None, 0)."""
    results = [r for r in yt.search(f"{artist} {title}", filter="songs", limit=SEARCH_RESULTS)
               if r.get("videoId")][:SEARCH_RESULTS]
    if not results:
        return None, 0
    # max() väljer den första vid lika värde, så YouTubes ordning avgör då.
    best = max(results, key=lambda r: match_score(artist, title, r))
    return best, round(100 * match_score(artist, title, best))


def describe(result):
    artists = ", ".join(a["name"] for a in result.get("artists") or [])
    return f"{artists} - {result.get('title', '')}"


def get_or_create_playlist(yt, description):
    for p in yt.get_library_playlists(limit=None):
        if p["title"] == PLAYLIST_NAME:
            return p["playlistId"]
    print(f"Skapar spellistan '{PLAYLIST_NAME}'")
    return yt.create_playlist(PLAYLIST_NAME, description, privacy_status="PRIVATE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="visa låtarna och sökträffarna utan att ändra något på YouTube Music")
    args = parser.parse_args()

    episode_title, tracks = fetch_digilistan()
    print(f"{episode_title}: {len(tracks)} låtar\n")

    # Sökning kräver ingen inloggning, så en provkörning klarar sig utan.
    yt = YTMusic() if args.dry_run else connect_ytmusic()

    video_ids, uncertain, missing = [], 0, 0
    for i, (artist, title) in enumerate(tracks, 1):
        result, score = find_song(yt, artist, title)
        if result is None:
            missing += 1
            print(f"{i:3}.    -   {artist} - {title}  (hittades inte)")
            continue
        video_ids.append(result["videoId"])
        mark = "!" if score < WARN_BELOW else " "
        print(f"{i:3}. {mark}{score:3}%  {artist} - {title}")
        if score < 100:
            uncertain += 1
            print(f"{'':12}-> {describe(result)}")

    print(f"\nExakta träffar: {len(video_ids) - uncertain}, osäkra: {uncertain}, "
          f"saknas: {missing}. ! = under {WARN_BELOW} %, kontrollera gärna.")

    if args.dry_run:
        return

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

    print(f"Lade till {len(video_ids)} låtar i '{PLAYLIST_NAME}'.")


if __name__ == "__main__":
    main()
