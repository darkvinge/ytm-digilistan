# ytm-digilistan

Synkar senaste [Digilistan i P3](https://www.sverigesradio.se/listprogramip3) från Sveriges Radio till en spellista som heter **Digilistan** på ditt YouTube Music-konto.

Varje körning ersätter spellistans innehåll med veckans lista, med ettan först.

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

Vill du bara se veckans lista utan att ändra något på YouTube Music:

```sh
./synka-digilistan.sh --dry-run
```

## Så fungerar det

1. Hämtar låtlistan för senaste avsnittet av Digilistan från [Sveriges Radios öppna API](https://api.sr.se/api/documentation/v2/).
2. Startar Chromium med en egen profil (`browser-profile/`) och läser ut inloggningscookies för YouTube Music. Om du inte är inloggad visas fönstret så att du kan logga in.
3. Söker upp varje låt på YouTube Music med [ytmusicapi](https://github.com/sigma67/ytmusicapi).
4. Skapar spellistan **Digilistan** (privat) om den inte finns och fyller den med veckans låtar.

Låtar som inte hittas på YouTube Music skrivs ut i slutet. Sökningen väljer den bästa träffen, så ibland kan det bli fel version av en låt.

## Filer

| Fil | Beskrivning |
| --- | --- |
| `synka-digilistan.sh` | Det enda du behöver köra. Installerar beroenden vid behov och startar synken. |
| `digilistan.py` | Själva synken. |
| `requirements.txt` | Python-paket som behövs. |
| `browser-profile/` | Skapas vid första körningen och innehåller din inloggning. **Dela den aldrig.** Den finns i `.gitignore`. |

## Felsökning

- **Google säger att webbläsaren kanske inte är säker:** Google nekar ibland inloggning i webbläsare som styrs av ett skript. Prova igen senare.
- **Logga in på ett annat konto:** ta bort mappen `browser-profile/` och kör skriptet igen.
- **Något är trasigt med installationen:** ta bort mappen `.venv/` så installeras allt på nytt vid nästa körning.
