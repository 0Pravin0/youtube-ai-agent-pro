#!/bin/bash
# Copy Homebrew's FFmpeg (arm64) into the engine bundle with its libraries, rewrite library paths so it is
# self-contained, verify Tamil text shaping support (libass + HarfBuzz + FreeType), and ad-hoc sign every file.
set -euo pipefail
OUT="$1"                       # e.g. build/engine/bin
mkdir -p "$OUT/libs"
# ffmpeg-full is Homebrew's build with libass/HarfBuzz/FreeType (needed for Tamil subtitles and titles)
FF="$(brew --prefix ffmpeg-full 2>/dev/null || true)"
[ -x "$FF/bin/ffmpeg" ] || FF="$(brew --prefix ffmpeg)"
echo "using FFmpeg from $FF"
cp "$FF/bin/ffmpeg" "$FF/bin/ffprobe" "$OUT/"
chmod u+w "$OUT/ffmpeg" "$OUT/ffprobe"
dylibbundler -of -b -x "$OUT/ffmpeg" -x "$OUT/ffprobe" -d "$OUT/libs" -p @executable_path/libs/ >/dev/null
for f in "$OUT"/libs/*.dylib "$OUT/ffmpeg" "$OUT/ffprobe"; do codesign --force --sign - "$f"; done
CONF=$("$OUT/ffmpeg" -hide_banner -buildconf)
for need in libass libharfbuzz libfreetype libx264; do
  echo "$CONF" | grep -q -- "--enable-$need" || { echo "bundled FFmpeg lacks $need"; exit 1; }
done
# must run without Homebrew present: no absolute /opt/homebrew references may remain
if otool -L "$OUT/ffmpeg" "$OUT"/libs/*.dylib | grep -q "/opt/homebrew"; then echo "unbundled library path left"; exit 1; fi
"$OUT/ffmpeg" -hide_banner -version | head -1
