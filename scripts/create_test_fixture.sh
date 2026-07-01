#!/usr/bin/env bash
# Generates a ~5.1s 320x240 MP4 alternating tone/silence segments (no real
# speech, so transcript-related assertions stay empty/near-empty as before).
# The silence gaps (0.8s, 1.0s, 0.6s) exercise the remove-silences feature's
# silencedetect pass while staying comfortably above its default 0.5s minimum.
# Requires ffmpeg to be installed on the host.
set -euo pipefail

FIXTURE_DIR="$(dirname "$0")/../backend/tests/fixtures"
mkdir -p "$FIXTURE_DIR"
OUT="$FIXTURE_DIR/sample.mp4"

if [ -f "$OUT" ]; then
    echo "Fixture already exists: $OUT"
    exit 0
fi

if ! command -v ffmpeg &>/dev/null; then
    echo "ERROR: ffmpeg not found. Install it with: apt-get install ffmpeg" >&2
    exit 1
fi

echo "Generating test fixture: $OUT"
ffmpeg -y \
    -f lavfi -i "aevalsrc=0:d=0.8" \
    -f lavfi -i "sine=frequency=440:duration=1.2" \
    -f lavfi -i "aevalsrc=0:d=1.0" \
    -f lavfi -i "sine=frequency=440:duration=1.5" \
    -f lavfi -i "aevalsrc=0:d=0.6" \
    -f lavfi -i "color=c=black:size=320x240:rate=24:duration=5.1" \
    -filter_complex "[0][1][2][3][4]concat=n=5:v=0:a=1[outa]" \
    -map 5:v -map "[outa]" \
    -shortest \
    -c:v libx264 -preset ultrafast -crf 40 \
    -c:a aac -b:a 64k \
    -loglevel error \
    "$OUT"

echo "Created: $OUT ($(du -sh "$OUT" | cut -f1))"
