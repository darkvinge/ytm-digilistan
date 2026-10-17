# ytm-digilistan

Synkar senaste [Digilistan i P3](https://www.sverigesradio.se/listprogramip3) och [Svensktoppen](https://www.sverigesradio.se/poddar/svensktoppen) från Sveriges Radio till spellistorna **Digilistan** och **Svensktoppen** på ditt YouTube Music-konto.

Varje körning ersätter spellistans innehåll med veckans lista, med ettan först (eller sist, se `-r` nedan). Har spellistan redan den senaste listan händer ingenting, om du inte kör med `-f` (`--force`).

## Krav

- Linux eller macOS med `bash`
- Python 3.10 eller senare

Allt annat (Python-paket och en Chromium-webbläsare) installeras automatiskt första gången.

## Användning

```sh
git clone https://github.com/darkvinge/ytm-digilistan.git
cd ytm-digilistan
./synka-digilistan.sh
```

Första gången öppnas ett Chromium-fönster på music.youtube.com. Logga in med ditt Google-konto. Skriptet märker själv när du är inloggad, stänger fönstret och synkar listan. Inloggningen sparas, så nästa gång körs allt i bakgrunden utan att något fönster öppnas.

Utan flaggor synkas Digilistan. Välj lista så här:

```sh
./synka-digilistan.sh --svensktoppen   # bara Svensktoppen
./synka-digilistan.sh --alla           # både Digilistan och Svensktoppen
```

Vill du lyssna i samma ordning som i radio, med sista placeringen först och ettan sist, kör du med `-r` (`--radioordning`):

```sh
./synka-digilistan.sh -r
```

Ordningen gäller tills du kör utan `-r` igen, och då vänds spellistan tillbaka.

Vill du bara se veckans lista och vilka låtar som hittas, utan att ändra något på YouTube Music:

```sh
./synka-digilistan.sh --dry-run
```

### Svensktoppens bubblare

I Svensktoppen spelas också veckans tre bubblare, som inte är med på listan. Sveriges Radios låtlista skiljer inte på dem, så skriptet letar i avsnittstexten efter meningen om bubblare, till exempel *"Tomas Andersson Wij (Mina Vänner), Greczula (Love=Art) och Chris Kläfford (Ain´t Built For Leaving) är veckans bubblare."*. Hittar skriptet exakt tre låtar vars artist nämns där hoppas de över. Annars tas alla låtar med, eftersom det inte går att avgöra vilka som är bubblare. Avsnittstexten nämner inte alltid bubblarna.

### Matchning

Varje låt får ett matchningsvärde som visar hur väl titel och artist från Digilistan stämmer med det som hittades på YouTube Music:

```
 19.   95%  Miley Cyrus - Bass Persuades (Radio Edit)
            -> Miley Cyrus - Bass Persuades
 22. ! 77%  Pinkpantheress, Zara Larsson - Stateside
            -> PinkPantheress - Stateside
```

- **100 %**: titel och artister stämmer exakt (skiftläge och skiljetecken räknas inte).
- **95 %**: titeln stämmer bara om man bortser från tillägg som *(Radio Edit)* eller *(Remix)*.
- **Under 80 %**: markeras med `!`. Ofta är det fel version, till exempel originalet i stället för en version med en gästartist, men det kan också vara fel låt.

Skriptet jämför de fem första sökträffarna och väljer den som matchar bäst. Gästartister i titeln, som *(feat. Zara Larsson)* eller *+ Zara Larsson*, räknas som artister.

### Välja själv

Med `-i` (eller `--valj`) får du välja själv för varje låt som inte matchar till 100 %:

```sh
./synka-digilistan.sh -i
```

```
 19.   95%  Miley Cyrus - Bass Persuades (Radio Edit)
            1.  95%  Miley Cyrus - Bass Persuades
            2.  76%  Miley Cyrus - Bass Persuades Remixx
            3.  33%  Miley Cyrus - Maybe You're Right
            Välj [Enter = 1, s = hoppa över, annat = sök på nytt]:
```

- **Enter** tar förslaget.
- **En siffra** väljer en annan träff.
- **`s`** hoppar över låten, så att den inte kommer med i spellistan.
- **Annan text** gör en ny sökning, till exempel om rätt version inte finns bland träffarna.

Dina val sparas i `val.json` och används automatiskt nästa gång samma låt finns på listan, så du behöver bara välja en gång per låt. Sådana låtar visas med `val` i stället för ett matchningsvärde:

```
 19.   val  Miley Cyrus - Bass Persuades (Radio Edit)
            -> Miley Cyrus - Bass Persuades Remixx
 26.   val  Taylor Swift - I Knew It, I Knew You  (hoppas över)
```

Vill du ändra dig tar du bort alla sparade val med `--glom-val` och kör sedan med `-i` igen:

```sh
./synka-digilistan.sh --glom-val -i
```

Flaggan går att kombinera med `--dry-run` om du vill prova utan att ändra spellistan. Valen sparas inte vid en provkörning.

## Så fungerar det

1. Hämtar låtlistan för senaste avsnittet av Digilistan eller Svensktoppen från [Sveriges Radios öppna API](https://api.sr.se/api/documentation/v2/).
2. Startar Chromium med en egen profil (`browser-profile/`) och läser ut inloggningscookies för YouTube Music. Om du inte är inloggad visas fönstret så att du kan logga in.
3. Söker upp varje låt på YouTube Music med [ytmusicapi](https://github.com/sigma67/ytmusicapi) och väljer den träff som matchar bäst (se [Matchning](#matchning)).
4. Skapar spellistan **Digilistan** eller **Svensktoppen** (privat) om den inte finns och fyller den med veckans låtar.

## Filer

| Fil | Beskrivning |
| --- | --- |
| `synka-digilistan.sh` | Det enda du behöver köra. Installerar beroenden vid behov och startar synken. |
| `digilistan.py` | Själva synken. |
| `requirements.txt` | Python-paket som behövs. |
| `browser-profile/` | Skapas vid första körningen och innehåller din inloggning. **Dela den aldrig.** Den finns i `.gitignore`. |
| `val.json` | Dina sparade val från `-i`. Den finns i `.gitignore`. |

## Felsökning

- **Google säger att webbläsaren kanske inte är säker:** Google nekar ibland inloggning i webbläsare som styrs av ett skript. Prova igen senare.
- **Logga in på ett annat konto:** ta bort mappen `browser-profile/` och kör skriptet igen.
- **Spellistan uppdateras inte fast du har ändrat den för hand:** skriptet hoppar över listor som redan är synkade. Kör med `-f` så uppdateras spellistan ändå.
- **Något är trasigt med installationen:** ta bort mappen `.venv/` så installeras allt på nytt vid nästa körning.

## Licens

[GPL-3.0](LICENSE)
