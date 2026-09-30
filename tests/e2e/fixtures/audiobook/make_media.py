"""Regenerate the fictitious audiobook's audio, cover and expected frames.

Needs ffmpeg on PATH (to encode) and PyAV (`pip install av`, to demux); the
E2E run itself needs neither. The audio is synthetic (sine tones), so no real
content is committed. Run from the repository root:

    python tests/e2e/fixtures/audiobook/make_media.py

It writes, under tests/e2e/fixtures/audiobook/:

- media/<entry>/<flavor>/index.m3u8 and seg-N.ts: the HLS stream of each
  chapter as Kaltura's CDN serves it (absolute, signed segment URLs);
- cover.jpg;
- expected.json: per chapter and flavor, the number of AAC frames and the
  sha256 of the raw frames concatenated, as FFmpeg's libraries (PyAV) demux
  them. The E2E checks compare the downloader's output with these figures.
"""

import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path

import av

HERE = Path(__file__).resolve().parent
MEDIA = HERE / "media"
SAMPLE_RATE = 22050

# entry id -> (tone Hz, seconds, {flavor id: bitrate})
CHAPTERS = {
    "1_capit001": (440, 5.2, {"1_hiflav01": "48k"}),
    # Two flavors: the client must choose the higher bandwidth one.
    "1_capit002": (523, 7.0, {"1_loflav02": "16k", "1_hiflav02": "48k"}),
    "1_capit003": (659, 6.1, {"1_hiflav03": "48k"}),
    "1_capit004": (784, 1.5, {"1_hiflav04": "48k"}),
}


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def raw_frames(stream: bytes) -> list[bytes]:
    """AAC frames without ADTS headers, as FFmpeg's mpegts demuxer reads them."""
    container = av.open(io.BytesIO(stream), format="mpegts")
    audio = container.streams.audio[0]
    strip = av.BitStreamFilterContext("aac_adtstoasc", audio)
    frames = [bytes(p) for packet in container.demux(audio) for p in strip.filter(packet)]
    frames += [bytes(p) for p in strip.filter(None)]
    return [f for f in frames if f]


def main() -> None:
    shutil.rmtree(MEDIA, ignore_errors=True)
    expected: dict[str, dict] = {}
    for entry, (tone, seconds, flavors) in CHAPTERS.items():
        for flavor, bitrate in flavors.items():
            out = MEDIA / entry / flavor
            out.mkdir(parents=True)
            ffmpeg(
                "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}:sample_rate={SAMPLE_RATE}",
                "-ac", "1", "-c:a", "aac", "-b:a", bitrate,
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "0", "-start_number", "1",
                "-hls_segment_filename", str(out / "seg-%d.ts"), str(out / "index.m3u8"),
            )
            playlist = (out / "index.m3u8").read_text()
            segments = [line for line in playlist.splitlines() if line.endswith(".ts")]
            for name in segments:
                playlist = playlist.replace(
                    f"\n{name}\n",
                    f"\n{{BASE}}cdn/{entry}/{flavor}/{name}?Policy=e2e-signed&Signature=e2e\n",
                )
            (out / "index.m3u8").write_text(playlist)
            stream = b"".join((out / name).read_bytes() for name in segments)
            frames = raw_frames(stream)
            expected.setdefault(entry, {})[flavor] = {
                "segments": len(segments),
                "frames": len(frames),
                "sha256": hashlib.sha256(b"".join(frames)).hexdigest(),
            }
    ffmpeg("-f", "lavfi", "-i", "color=c=0x2a6f4e:s=96x96", "-frames:v", "1", str(HERE / "cover.jpg"))
    (HERE / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")


if __name__ == "__main__":
    main()
