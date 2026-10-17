#!/usr/bin/env python3
"""Hämtar senaste Digilistan och/eller Svensktoppen från Sveriges Radio och
lägger låtarna i varsin spellista på YouTube Music."""

import argparse
import html
import json
import re
import sys
import time
from contextlib import suppress
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError

SR_API = "https://api.sr.se/api/v2"
# Listorna vi kan synka. Spellistan på YouTube Music får samma namn som listan.
CHARTS = {
    "digilistan": {"name": "Digilistan", "program_id": 2697, "bubblers": 0},
    # Svensktoppen spelar även veckans tre bubblare, som inte är med på listan.
    "svensktoppen": {"name": "Svensktoppen", "program_id": 2023, "bubblers": 3},
}
PROFILE_DIR = Path(__file__).with_name("browser-profile")
CHOICES_FILE = Path(__file__).with_name("val.json")
YTM_URL = "https://music.youtube.com"
LOGIN_COOKIE = "__Secure-3PAPISID"
LOGIN_TIMEOUT = 300  # sekunder
EPISODES_TO_TRY = 8  # hur många avsnitt bakåt vi letar efter en låtlista
SEARCH_RESULTS = 5  # hur många sökträffar vi jämför
WARN_BELOW = 80  # procent; träffar under detta markeras med !
VERSION_PENALTY = 0.95  # titeln stämmer bara om man bortser från (Radio Edit) o.d.
MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli",
          "augusti", "september", "oktober", "november", "december"]


class NetworkError(Exception):
    """En tjänst gick inte att nå."""


def sr_get(path, **params):
    try:
        response = requests.get(f"{SR_API}/{path}", params={**params, "format": "json"},
                                timeout=30)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as e:
        raise NetworkError("Kunde inte hämta listan från Sveriges Radio.") from e


def fetch_chart(chart):
    """Returnerar (sändningsdatum, [(artist, titel), ...]) med etta först, eller
    None om ingen låtlista finns.

    Tar senaste avsnittet som har en låtlista, eftersom listan för ett nyss
    sänt avsnitt inte alltid är publicerad än. Flödet innehåller också
    webbinslag utan låtlista, som hoppas över på samma sätt."""
    episodes = sr_get("episodes/index", programid=chart["program_id"],
                      size=EPISODES_TO_TRY).get("episodes", [])

    for episode in episodes:
        songs = sr_get("playlists/getplaylistbyepisodeid", id=episode["id"]).get("song", [])
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
        text = sr_get("episodes/get", id=episode["id"]).get("episode", {}).get("text")
        bubblers = find_bubblers(text, tracks, chart["bubblers"])
        if bubblers:
            print("Hoppar över veckans bubblare: "
                  + ", ".join(f"{a} - {t}" for a, t in bubblers))
            tracks = [t for t in tracks if t not in bubblers]
        else:
            print("Kunde inte avgöra vilka som är bubblare, tar med alla låtar.")
    return episode_date(episode), tracks


def episode_date(episode):
    """Gör om SR:s "/Date(1791709380000)/" till t.ex. "11 oktober 2026"."""
    ms = int(re.search(r"\d+", episode["publishdateutc"]).group())
    d = datetime.fromtimestamp(ms / 1000)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


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
        try:
            page.goto(YTM_URL)
        except PlaywrightError as e:
            context.close()
            raise NetworkError("Kunde inte nå YouTube Music.") from e

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
    except requests.RequestException:
        raise
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


def choice_key(artist, title):
    return f"{artist} - {title}".casefold()


def load_choices():
    """Dina tidigare val från -i: {"artist - titel": {"videoId", "match"} eller {"skip"}}."""
    try:
        return json.loads(CHOICES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        print(f"Kunde inte läsa {CHOICES_FILE.name}, struntar i sparade val.")
        return {}


def save_choices(choices):
    CHOICES_FILE.write_text(json.dumps(choices, ensure_ascii=False, indent=2, sort_keys=True)
                            + "\n", encoding="utf-8")


def find_playlist(yt, name):
    """Returnerar (spellistans id, spellistan) eller (None, None) om den inte finns."""
    for p in yt.get_library_playlists(limit=None):
        if p["title"] == name:
            try:
                return p["playlistId"], yt.get_playlist(p["playlistId"], limit=None)
            except KeyError:
                # ytmusicapi klarar inte tomma (t.ex. nyss skapade) spellistor.
                return p["playlistId"], {"tracks": []}
    return None, None


def sync_chart(yt, chart, args, choices):
    name = chart["name"]
    print(f"=== {name} ===")
    fetched = fetch_chart(chart)
    if fetched is None:
        return
    date, tracks = fetched
    # Ordningen står i beskrivningen, så att byte av ordning räknas som en ändring.
    order = ", i radioordning" if args.radioordning else ""
    description = f"{name} {date}{order} (Sveriges Radio)"
    print(f"Sändes {date}: {len(tracks)} låtar")

    playlist_id = playlist = None
    same_week = False
    if not args.dry_run:
        playlist_id, playlist = find_playlist(yt, name)
        # Beskrivningen innehåller sändningsdatumet, så samma beskrivning
        # betyder att spellistan redan har den här veckans lista. Med -i eller
        # --glom-val kan valen ändras, så då söker vi och jämför låtarna nedan.
        # Med -f uppdateras spellistan alltid.
        same_week = (bool(playlist) and playlist.get("description") == description
                     and not args.force)
        if same_week and not (args.valj or args.glom_val):
            print(f"Spellistan '{name}' har redan den här listan, inget att göra.")
            return
    print()

    video_ids = []
    counts = {"exact": 0, "chosen": 0, "uncertain": 0, "missing": 0, "skipped": 0}
    for i, (artist, title) in enumerate(tracks, 1):
        key = choice_key(artist, title)
        saved = choices.get(key)
        if saved:
            if saved.get("skip"):
                print(f"{i:3}.   val  {artist} - {title}  (hoppas över)")
                counts["skipped"] += 1
            else:
                print(f"{i:3}.   val  {artist} - {title}")
                print(f"{'':12}-> {saved['match']}")
                video_ids.append(saved["videoId"])
                counts["chosen"] += 1
            continue

        scored = search_songs(yt, artist, title)
        result, score = scored[0] if scored else (None, 0)
        if result is None:
            print(f"{i:3}.    -   {artist} - {title}  (hittades inte)")
        else:
            mark = "!" if score < WARN_BELOW else " "
            print(f"{i:3}. {mark}{score:3}%  {artist} - {title}")

        if args.valj and score < 100:
            result, score = choose_song(yt, artist, title, scored)
            if not args.dry_run:
                choices[key] = ({"skip": True} if result is None else
                                {"videoId": result["videoId"], "match": describe(result)})
                save_choices(choices)
            if result is None:
                counts["skipped"] += 1
                continue
            video_ids.append(result["videoId"])
            counts["chosen"] += 1
            continue

        if result is None:
            counts["missing"] += 1
            continue
        if score < 100:
            print(f"{'':12}-> {describe(result)}")
        video_ids.append(result["videoId"])
        counts["exact" if score == 100 else "uncertain"] += 1

    labels = {"exact": "exakta träffar", "chosen": "dina val", "uncertain": "osäkra",
              "missing": "saknas", "skipped": "överhoppade"}
    summary = ", ".join(f"{labels[k]}: {n}" for k, n in counts.items()
                        if n or k in ("exact", "uncertain", "missing"))
    print(f"\n{summary[0].upper()}{summary[1:]}. ! = under {WARN_BELOW} %.")
    if (counts["uncertain"] or counts["missing"]) and not args.valj:
        print("Kör med -i för att själv välja bland träffarna för osäkra låtar.")

    if args.dry_run:
        return

    if not video_ids:
        print("Inga låtar hittades på YouTube Music, spellistan lämnas orörd.")
        return

    if args.radioordning:
        # Som i sändningen: sista placeringen först och ettan sist.
        video_ids.reverse()

    if same_week and [t.get("videoId") for t in playlist["tracks"]] == video_ids:
        print(f"Spellistan '{name}' har redan de här låtarna, inget att göra.")
        return

    if playlist_id is None:
        print(f"Skapar spellistan '{name}'")
        playlist_id = yt.create_playlist(name, description, privacy_status="PRIVATE")
    elif playlist["tracks"]:
        # Byt ut innehållet så att spellistan alltid speglar veckans lista.
        yt.remove_playlist_items(playlist_id, playlist["tracks"])
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
                        help="välj själv bland sökträffarna för låtar som inte matchar till 100 %%; "
                             "valen sparas och används automatiskt nästa gång")
    parser.add_argument("--glom-val", action="store_true",
                        help="glöm alla sparade val från -i")
    parser.add_argument("-f", "--force", action="store_true",
                        help="uppdatera spellistan även om den redan har veckans lista")
    parser.add_argument("-r", "--radioordning", action="store_true",
                        help="lägg låtarna i samma ordning som i radio, med ettan sist")
    args = parser.parse_args()
    if args.valj and not sys.stdin.isatty():
        sys.exit("--valj kräver en terminal att svara i.")

    if args.glom_val:
        CHOICES_FILE.unlink(missing_ok=True)
        print("Sparade val är borttagna.")

    if args.alla:
        charts = list(CHARTS.values())
    elif args.svensktoppen:
        charts = [CHARTS["svensktoppen"]]
    else:
        charts = [CHARTS["digilistan"]]

    choices = load_choices()
    try:
        # Sökning kräver ingen inloggning, så en provkörning klarar sig utan.
        yt = YTMusic() if args.dry_run else connect_ytmusic()
        for n, chart in enumerate(charts):
            if n:
                print()
            try:
                sync_chart(yt, chart, args, choices)
            except NetworkError as e:
                print(f"{e} Försök igen senare.")
    except (NetworkError, requests.RequestException, YTMusicServerError):
        sys.exit("Kunde inte nå YouTube Music. Försök igen senare.")
    except KeyboardInterrupt:
        sys.exit("\nAvbrutet.")


if __name__ == "__main__":
    main()
