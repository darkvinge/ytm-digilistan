#!/usr/bin/env bash
# Synkar senaste Digilistan och/eller Svensktoppen från Sveriges Radio till
# spellistorna "Digilistan" och "Svensktoppen" på YouTube Music.
# Det här är det enda skriptet du behöver köra.
#
# Första gången installeras allt som behövs automatiskt.
#
# Flaggor:
#   --svensktoppen  synka Svensktoppen i stället för Digilistan
#   --alla        synka både Digilistan och Svensktoppen
#   --dry-run     visa låtar och sökträffar utan att ändra spellistan
#   -i, --valj    välj själv bland sökträffarna för låtar under 100 % matchning

set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

VENV=.venv

if ! "$VENV/bin/python" -c "import ytmusicapi, requests, playwright" 2>/dev/null; then
    echo "Installerar det som behövs (bara första gången)..."
    rm -rf "$VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r requirements.txt
fi

# Gör ingenting om webbläsaren redan är nedladdad.
"$VENV/bin/playwright" install chromium

exec "$VENV/bin/python" digilistan.py "$@"
