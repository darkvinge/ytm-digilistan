#!/usr/bin/env python3
"""Hämtar senaste Digilistan och/eller Svensktoppen från Sveriges Radio och
lägger låtarna i varsin spellista på YouTube Music."""

import argparse
import html
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

SR_API = "https://api.sr.se/api/v2"
# Listorna vi kan synka. Spellistan på YouTube Music får samma namn som listan.
CHARTS = {
    "digilistan": {"name": "Digilistan", "program_id": 2697, "bubblers": 0},
    # Svensktoppen spelar även veckans tre bubblare, som inte är med på listan.
    "svensktoppen": {"name": "Svensktoppen", "program_id": 2023, "bubblers": 3},
}
PROFILE_DIR = Path(__file__).with_name("browser-profile")
YTM_URL = "https://music.youtube.com"
LOGIN_COOKIE = "__Secure-3PAPISID"
LOGIN_TIMEOUT = 300  # sekunder
EPISODES_TO_TRY = 8  # hur många avsnitt bakåt vi letar efter en låtlista
SEARCH_RESULTS = 5  # hur många sökträffar vi jämför
WARN_BELOW = 80  # procent; träffar under detta markeras med !
VERSION_PENALTY = 0.95  # titeln stämmer bara om man bortser från (Radio Edit) o.d.


def fetch_chart(chart):
    """Returnerar (avsnittstitel, [(artist, titel), ...]) med etta först, eller
    None om ingen låtlista finns.

    Tar senaste avsnittet som har en låtlista, eftersom listan för ett nyss
    sänt avsnitt inte alltid är publicerad än. Flödet innehåller också
    webbinslag utan låtlista, som hoppas över på samma sätt."""
    episodes = requests.get(
        f"{SR_API}/episodes/index",
        params={"programid": chart["program_id"], "format": "json", "size": EPISODES_TO_TRY},
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
        print(f"Ingen låtlista för '{episode['title']}', provar föregående avsnitt.")
    else:
        print("Hittade ingen låtlista hos Sveriges Radio, spellistan lämnas orörd.")
        return None

    # Listan räknas ner i sändning, så den senast spelade låten är etta.
    songs.sort(key=lambda s: s["starttimeutc"], reverse=True)

    tracks, seen = [], set()
    for s in songs:
        key = (s["artist"].lower(), s["title"].lower())
        if key not in seen:
            seen.add(key)
            tracks.append((s["artist"], s["title"]))

    if chart["bubblers"]:
        # Avsnittslistan saknar avsnittstexten, så den hämtas separat.
        text = requests.get(
            f"{SR_API}/episodes/get",
            params={"id": episode["id"], "format": "json"},
            timeout=30,
        ).json()["episode"].get("text")
        bubblers = find_bubblers(text, tracks, chart["bubblers"])
        if bubblers:
            print("Hoppar över veckans bubblare: "
                  + ", ".join(f"{a} - {t}" for a, t in bubblers))
            tracks = [t for t in tracks if t not in bubblers]
        else:
            print("Kunde inte avgöra vilka som är bubblare, tar med alla låtar.")
    return episode["title"], tracks


def find_bubblers(text, tracks, expected):
    """Letar upp låtarna vars artist nämns i avsnittstextens mening om bubblare,
    t.ex. "A (Låt), B (Låt) och C (Låt) är veckans bubblare.". Returnerar dem
    bara om det blir exakt så många som förväntat, annars en tom lista."""
    sentences = re.split(r"(?<=[.!?])\s+|\n", html.unescape(text or ""))
    bubbler_text = " ".join(s for s in sentences if "bubblare" in s.casefold())
    if not bubbler_text:
        return []
    padded = f" {normalize(bubbler_text)} "
    found = [t for t in tracks
             if any(f" {normalize(a)} " in padded for a in split_artists(t[0]))]
    return found if len(found) == expected else []


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


def get_or_create_playlist(yt, name, description):
    for p in yt.get_library_playlists(limit=None):
        if p["title"] == name:
            return p["playlistId"]
    print(f"Skapar spellistan '{name}'")
    return yt.create_playlist(name, description, privacy_status="PRIVATE")


def sync_chart(yt, chart, args):
    print(f"=== {chart['name']} ===")
    fetched = fetch_chart(chart)
    if fetched is None:
        return
    episode_title, tracks = fetched
    print(f"{episode_title}: {len(tracks)} låtar\n")

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
        print("Inga låtar hittades på YouTube Music, spellistan lämnas orörd.")
        return

    name = chart["name"]
    description = episode_title if name in episode_title else f"{name}: {episode_title}"
    description += " (Sveriges Radio)"
    playlist_id = get_or_create_playlist(yt, name, description)

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

    print(f"Lade till {len(video_ids)} låtar i '{name}'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--svensktoppen", action="store_true",
                       help="synka Svensktoppen i stället för Digilistan")
    which.add_argument("--alla", action="store_true",
                       help="synka både Digilistan och Svensktoppen")
    parser.add_argument("--dry-run", action="store_true",
                        help="visa låtarna och sökträffarna utan att ändra något på YouTube Music")
    parser.add_argument("-i", "--valj", action="store_true",
                        help="välj själv bland sökträffarna för låtar som inte matchar till 100 %%")
    args = parser.parse_args()
    if args.valj and not sys.stdin.isatty():
        sys.exit("--valj kräver en terminal att svara i.")

    if args.alla:
        charts = list(CHARTS.values())
    elif args.svensktoppen:
        charts = [CHARTS["svensktoppen"]]
    else:
        charts = [CHARTS["digilistan"]]

    # Sökning kräver ingen inloggning, så en provkörning klarar sig utan.
    yt = YTMusic() if args.dry_run else connect_ytmusic()

    for n, chart in enumerate(charts):
        if n:
            print()
        sync_chart(yt, chart, args)


if __name__ == "__main__":
    main()
