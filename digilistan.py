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


def split_title(title):
    """Flyttar gästartister ur titeln: "Låt + A (feat. B)" -> ("Låt", ["A", "B"])."""
    guests = []
    feat = r"\s*[\(\[](?:feat\.?|ft\.?|with)\s+([^\)\]]+)[\)\]]"
    guests += re.findall(feat, title, flags=re.IGNORECASE)
    title = re.sub(feat, "", title, flags=re.IGNORECASE)
    title, *plus = title.split(" + ", 1)
    guests += plus
    return title.strip(), [a for g in guests for a in split_artists(g)]


def match_score(artist, title, result):
    wanted_title, wanted_guests = split_title(title)
    found_title, found_guests = split_title(result.get("title", ""))
    wanted_artists = list(dict.fromkeys(split_artists(artist) + wanted_guests))
    found_artists = [a for r in result.get("artists") or [] for a in split_artists(r["name"])]
    found_artists = list(dict.fromkeys(found_artists + found_guests))
    return title_score(wanted_title, found_title) * artist_score(wanted_artists, found_artists)


def search_songs(yt, artist, title, query=None):
    """Returnerar [(sökträff, matchning 0–100), ...] med bästa matchningen först."""
    results = [r for r in yt.search(query or f"{artist} {title}", filter="songs",
                                    limit=SEARCH_RESULTS)
               if r.get("videoId")][:SEARCH_RESULTS]
    scored = [(r, round(100 * match_score(artist, title, r))) for r in results]
    # sort() är stabil, så YouTubes ordning avgör vid lika värde.
    scored.sort(key=lambda rs: rs[1], reverse=True)
    return scored


def choose_song(yt, artist, title, scored):
    """Låter användaren välja bland sökträffarna. Returnerar (sökträff, matchning)
    eller (None, 0) om låten hoppas över."""
    indent = " " * 12
    while True:
        if scored:
            for n, (result, score) in enumerate(scored, 1):
                print(f"{indent}{n}. {score:3}%  {describe(result)}")
        else:
            print(f"{indent}Inga träffar.")
        answer = input(f"{indent}Välj [Enter = 1, s = hoppa över, annat = sök på nytt]: ").strip()
        if not answer:
            return scored[0] if scored else (None, 0)
        if answer.lower() == "s":
            return None, 0
        if answer.isdigit() and 1 <= int(answer) <= len(scored):
            return scored[int(answer) - 1]
        scored = search_songs(yt, artist, title, query=answer)


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
    parser.add_argument("-i", "--valj", action="store_true",
                        help="välj själv bland sökträffarna för låtar som inte matchar till 100 %%")
    args = parser.parse_args()
    if args.valj and not sys.stdin.isatty():
        sys.exit("--valj kräver en terminal att svara i.")

    episode_title, tracks = fetch_digilistan()
    print(f"{episode_title}: {len(tracks)} låtar\n")

    # Sökning kräver ingen inloggning, så en provkörning klarar sig utan.
    yt = YTMusic() if args.dry_run else connect_ytmusic()

    video_ids, uncertain, missing = [], 0, 0
    for i, (artist, title) in enumerate(tracks, 1):
        scored = search_songs(yt, artist, title)
        result, score = scored[0] if scored else (None, 0)
        if result is None:
            print(f"{i:3}.    -   {artist} - {title}  (hittades inte)")
        else:
            mark = "!" if score < WARN_BELOW else " "
            print(f"{i:3}. {mark}{score:3}%  {artist} - {title}")

        if args.valj and score < 100:
            result, score = choose_song(yt, artist, title, scored)
        elif result is not None and score < 100:
            print(f"{'':12}-> {describe(result)}")

        if result is None:
            missing += 1
            continue
        video_ids.append(result["videoId"])
        if score < 100:
            uncertain += 1

    print(f"\nExakta träffar: {len(video_ids) - uncertain}, osäkra: {uncertain}, "
          f"saknas: {missing}. ! = under {WARN_BELOW} %.")
    if (uncertain or missing) and not args.valj:
        print("Kör med -i för att själv välja bland träffarna för osäkra låtar.")

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
