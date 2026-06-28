#!/usr/bin/env bash
# Generates a minimal 3-second 320x240 MP4 with a 440 Hz sine tone.
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
    -f lavfi -i "sine=frequency=440:duration=3" \
    -f lavfi -i "color=c=black:size=320x240:rate=24:duration=3" \
    -shortest \
    -c:v libx264 -preset ultrafast -crf 40 \
    -c:a aac -b:a 64k \
    -loglevel error \
    "$OUT"

echo "Created: $OUT ($(du -sh "$OUT" | cut -f1))"
