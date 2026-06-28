#!/usr/bin/env bash
# Run inside the worker container after rebuilding (requires espeak-ng):
#   docker exec captionator-worker-1 bash /app/gen_test_video.sh
#
# Outputs /storage/uploads/test_speech.mp4 — upload this via the UI
# and choose duo_tone or mixed_weight to verify compound tags in captions.ass.

set -e

PHRASE="The quick brown fox jumps over the lazy dog near the stream"
OUT_WAV="/tmp/test_speech.wav"
OUT_MP4="/storage/uploads/test_speech.mp4"

echo "Synthesizing speech with espeak-ng..."
espeak-ng -s 140 -a 150 -w "$OUT_WAV" "$PHRASE"

echo "Muxing audio + black video with ffmpeg..."
# 5-second 1280x720 black video; audio drives actual duration
DURATION=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$OUT_WAV")
ffmpeg -y \
  -f lavfi -i "color=c=black:s=1280x720:r=25:d=${DURATION}" \
  -i "$OUT_WAV" \
  -c:v libx264 -crf 23 -preset fast \
  -c:a aac -b:a 128k \
  -shortest "$OUT_MP4"

echo ""
echo "Done: $OUT_MP4"
echo "Upload this file at http://localhost:80 and pick Duo Tone or Mixed Weight."
