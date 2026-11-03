"""Makes mprice_decoder/assets/alert.wav: the chime MaplePrice plays with a new price alert (alerts.py). Two soft
bell notes going up (A5 then E6), each a sine with a little of its overtones, struck and fading out.
    python3 tools/alert_sound.py
"""
import math
import struct
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parents[1] / "mprice_decoder" / "assets" / "alert.wav"
NOTES = ((880.0, 0.0), (1318.5, 0.11))  # (Hz, starts at s)
LENGTH = 0.75     # s
FADE = 0.16       # s for a note to fall to about a third
PEAK = 0.45       # of full scale: a chime, not an alarm


def sample(t):
    total = 0.0
    for freq, start in NOTES:
        s = t - start
        if s < 0:
            continue
        envelope = min(1.0, s / 0.004) * math.exp(-s / FADE)
        total += envelope * sum(level * math.sin(2 * math.pi * freq * n * s) for n, level in ((1, 1.0), (2, 0.28), (3, 0.08)))
    tail = min(1.0, (LENGTH - t) / 0.05)  # no click at the end
    return total * tail


def main():
    raw = [sample(i / RATE) for i in range(int(RATE * LENGTH))]
    scale = PEAK / max(abs(x) for x in raw)
    with wave.open(str(OUT), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"".join(struct.pack("<h", round(x * scale * 32767)) for x in raw))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
