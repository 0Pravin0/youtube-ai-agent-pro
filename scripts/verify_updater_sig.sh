#!/bin/bash
# Verify the Tauri updater signature of a release archive with minisign and the public key built into the app.
set -euo pipefail
ARCHIVE="$1"; SIG="$1.sig"; CONF="$2"
PUB=$(python3 -c "import json,sys;print(json.load(open('$CONF'))['plugins']['updater']['pubkey'])" | base64 -d | tail -1)
base64 -d < "$SIG" > /tmp/update.minisig
minisign -Vm "$ARCHIVE" -P "$PUB" -x /tmp/update.minisig
echo "UPDATER SIGNATURE OK"
