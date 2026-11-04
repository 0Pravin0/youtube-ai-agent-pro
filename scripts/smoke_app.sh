#!/bin/bash
# Launch the built .app like a user would (the real binary), wait for its engine, check health, quit.
set -euo pipefail
APP="$1"
BIN="$APP/Contents/MacOS/$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$APP/Contents/Info.plist")"
"$BIN" > /tmp/yta-app.log 2>&1 &
PID=$!
for i in $(seq 1 120); do
  PORT=$(lsof -nP -a -c yta-engin -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1{split($9,a,":"); print a[2]; exit}')
  if [ -n "${PORT:-}" ] && curl -fsS "http://127.0.0.1:$PORT/api/health" -o /tmp/health.json; then break; fi
  sleep 1
done
cat /tmp/health.json; echo
grep -q '"status":"ok"' /tmp/health.json
kill -TERM "$PID"; sleep 5
if pgrep -f "Contents/Resources/engine/yta-engine" >/dev/null; then echo "engine still running after app quit"; exit 1; fi
echo "APP SMOKE OK"
