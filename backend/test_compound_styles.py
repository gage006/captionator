"""
Verify _build_compound_events produces inline ASS override tags.

Run inside the worker container:
  docker exec captionator-worker python /app/test_compound_styles.py
"""
import sys
sys.path.insert(0, "/app")

from app.tasks.ass_generator import _build_compound_events, build

# Synthetic Whisper output: 6 words with timestamps
SEGMENTS = [
    {
        "start": 0.0, "end": 3.0,
        "text": " The key is to stay active",
        "words": [
            {"word": " The",    "start": 0.0, "end": 0.4},
            {"word": " key",    "start": 0.4, "end": 0.8},
            {"word": " is",     "start": 0.8, "end": 1.0},
            {"word": " to",     "start": 1.0, "end": 1.4},
            {"word": " stay",   "start": 1.4, "end": 2.0},
            {"word": " active", "start": 2.0, "end": 3.0},
        ],
    }
]


def check(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        sys.exit(1)


print("=== duo_tone ===")
from app.styles.definitions import STYLES
duo = STYLES["duo_tone"]
events = _build_compound_events(SEGMENTS, duo, "DuoTone")
print("\n".join(events))
check(len(events) >= 1, "at least one event produced")
check(any("\\c" in e for e in events), "color override tag present")
check(any("\\N" in e for e in events), "hard line-break present")
check(any("{\\r}" in e for e in events), "reset tag present")

print()
print("=== mixed_weight ===")
mw = STYLES["mixed_weight"]
events = _build_compound_events(SEGMENTS, mw, "MixedWeight")
print("\n".join(events))
check(len(events) >= 1, "at least one event produced")
check(any("\\1a" in e for e in events), "hollow (alpha) tag present")
check(any("\\bord" in e for e in events), "border-width override present")
check(any("{\\r}" in e for e in events), "reset tag present")

print()
print("=== full build() round-trip ===")
ass = build(SEGMENTS, "duo_tone", 1920, 1080)
check("[Script Info]" in ass, "ASS header present")
check("DuoTone" in ass, "style name in output")
check("\\c&H2222FF&" in ass, "accent color in output")
print()
print("All checks passed.")
