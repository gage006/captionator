#!/usr/bin/env bash
# Generates two REAL-speech fixtures for backend/tests/test_real_speech_silence_removal.py
# (the main sample.mp4 fixture used elsewhere is a tone with no speech content,
# which can't validate Whisper transcription or natural-pause silence removal).
#
#   real_speech_synthetic.mp4 - TTS-narrated sentences (espeak-ng) with designed
#       silence gaps. Deterministic, fast to regenerate, no network dependency.
#   real_speech_gehrig.mp4 - Lou Gehrig's 1939 "Farewell to Baseball" speech,
#       public domain audio from archive.org, muxed onto a plain video track.
#       Authentic human speech cadence and a long noisy trailing pause exercise
#       cases the synthetic clip doesn't.
#
# Requires Docker (used to run espeak-ng/ffmpeg without installing them on the
# host) and, for the Gehrig clip, network access to archive.org.
set -euo pipefail

FIXTURE_DIR="$(dirname "$0")/../backend/tests/fixtures"
mkdir -p "$FIXTURE_DIR"

SYNTHETIC_OUT="$FIXTURE_DIR/real_speech_synthetic.mp4"
GEHRIG_OUT="$FIXTURE_DIR/real_speech_gehrig.mp4"

if ! command -v docker &>/dev/null; then
    echo "ERROR: docker not found; it's used to run espeak-ng/ffmpeg without host installs." >&2
    exit 1
fi

if [ -f "$SYNTHETIC_OUT" ]; then
    echo "Fixture already exists: $SYNTHETIC_OUT"
else
    echo "Generating $SYNTHETIC_OUT (espeak-ng TTS + designed silence gaps)..."
    WORKDIR=$(mktemp -d)
    docker run --rm -v "$WORKDIR:/work" debian:bookworm-slim bash -c '
        set -e
        apt-get update -qq && apt-get install -y -qq espeak-ng ffmpeg >/dev/null 2>&1
        cd /work
        espeak-ng -v en-us -s 150 -w s1.wav "Hello, and welcome to this test recording."
        espeak-ng -v en-us -s 150 -w s2.wav "We are checking whether the silence removal feature works correctly."
        espeak-ng -v en-us -s 150 -w s3.wav "Captions should stay perfectly synced after the silent gaps are cut out."
        espeak-ng -v en-us -s 150 -w s4.wav "This is the final sentence on the test clip. Thank you for listening."
        ffmpeg -y \
            -f lavfi -i "aevalsrc=0:d=0.5" -i s1.wav \
            -f lavfi -i "aevalsrc=0:d=1.2" -i s2.wav \
            -f lavfi -i "aevalsrc=0:d=1.5" -i s3.wav \
            -f lavfi -i "aevalsrc=0:d=1.0" -i s4.wav \
            -f lavfi -i "aevalsrc=0:d=0.5" \
            -filter_complex "[0][1][2][3][4][5][6][7][8]concat=n=9:v=0:a=1[outa]" \
            -map "[outa]" -ar 44100 -ac 1 speech.wav
        DUR=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 speech.wav)
        ffmpeg -y \
            -f lavfi -i "color=c=0x1a1a2e:size=640x360:rate=24:duration=$DUR" \
            -i speech.wav \
            -vf "drawtext=text=Silence Removal Test Clip:fontcolor=white:fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2" \
            -shortest \
            -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
            -c:a aac -b:a 128k \
            -loglevel error \
            out.mp4
    '
    cp "$WORKDIR/out.mp4" "$SYNTHETIC_OUT"
    rm -rf "$WORKDIR"
    echo "Created: $SYNTHETIC_OUT"
fi

if [ -f "$GEHRIG_OUT" ]; then
    echo "Fixture already exists: $GEHRIG_OUT"
else
    echo "Generating $GEHRIG_OUT (downloading public domain audio from archive.org)..."
    WORKDIR=$(mktemp -d)
    curl -sL -o "$WORKDIR/gehrig.mp3" \
        "https://archive.org/download/GreatestSpeechesBabbleLabs/Farewell%20to%20Baseball%20-%20Lou%20Gehrig%20%281939%29.mp3" \
        -A "Mozilla/5.0"
    if [ ! -s "$WORKDIR/gehrig.mp3" ]; then
        echo "ERROR: download failed or produced an empty file." >&2
        rm -rf "$WORKDIR"
        exit 1
    fi
    docker run --rm -v "$WORKDIR:/work" debian:bookworm-slim bash -c '
        set -e
        apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null 2>&1
        cd /work
        DUR=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 gehrig.mp3)
        ffmpeg -y \
            -f lavfi -i "color=c=0x1a1a2e:size=640x360:rate=24:duration=$DUR" \
            -i gehrig.mp3 \
            -vf "drawtext=text=Lou Gehrig 1939 Farewell Speech:fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2" \
            -shortest \
            -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
            -c:a aac -b:a 128k \
            -loglevel error \
            out.mp4
    '
    cp "$WORKDIR/out.mp4" "$GEHRIG_OUT"
    rm -rf "$WORKDIR"
    echo "Created: $GEHRIG_OUT"
fi
