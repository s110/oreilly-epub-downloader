"""MPEG-4 audio files (M4B/M4A) written from raw AAC frames.

The file carries what audiobook and podcast players read:

- chapters twice: a QuickTime chapter track (Apple Books, Apple Podcasts,
  iOS/macOS players) and a Nero `chpl` box (VLC, ffmpeg-based players);
- iTunes-style tags (title, author, narrator, description, cover, media kind);
- the `moov` box before the audio (fast start), so a player that streams the
  file over HTTP, like a podcast app, can start before the download ends.

The layout copies what ffmpeg writes for an M4B with chapters, so players that
accept ffmpeg's files accept these. The output only depends on the input: no
creation dates or encoder versions, so the same audiobook gives the same bytes.
"""

import struct
import sys
from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from .hls import SAMPLES_PER_FRAME
from .models import AudioTrack

MOVIE_TIMESCALE = 1000
CHUNK_SAMPLES = 64  # AAC frames per chunk: about 1.5 s at 44.1 kHz
MAX_CHPL_CHAPTERS = 255

# Values ffmpeg writes for a QuickTime chapter (text) track.
ENCD = b"\x00\x00\x00\x0cencd\x00\x00\x01\x00"  # the sample text is UTF-8
TEXT_SAMPLE_ENTRY = bytes.fromhex(
    "000000000000"  # reserved
    "0001"  # data reference index
    "00000001"  # display flags
    "0000"  # justification
    "0000000000000000"  # background colour
    "0000000000000000"  # default text box
    "00000000000000000001ffffffff"  # style record
    "0000000d6674616200010001" "00"  # font table: one font, empty name
)
GMHD = bytes.fromhex(
    "0000004c676d686400000018676d696e000000000040800080008000000000000000002c"
    "74657874000100000000000000000000000000000001000000000000000000000000000040000000"
)
MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)

# ISO 639-1 codes the O'Reilly catalogue uses -> ISO 639-2/T for `mdhd`.
LANGUAGES = {
    "en": "eng", "es": "spa", "pt": "por", "fr": "fra", "de": "deu", "it": "ita",
    "ja": "jpn", "zh": "zho", "ko": "kor", "ru": "rus", "nl": "nld", "pl": "pol",
}

# `data` atom types.
UTF8, JPEG, PNG, INTEGER, BINARY = 1, 13, 14, 21, 0
MEDIA_KIND_AUDIOBOOK = 2


def box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I", 8 + len(body)) + kind + body


def full_box(kind: bytes, version: int, flags: int, *parts: bytes) -> bytes:
    return box(kind, struct.pack(">I", (version << 24) | flags), *parts)


def _be32(values: array) -> bytes:
    data = array("I", values)
    if sys.byteorder == "little":
        data.byteswap()
    return data.tobytes()


def _utf8(text: str, limit: int) -> bytes:
    """`text` in UTF-8, cut to `limit` bytes without splitting a character."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore").encode("utf-8")


def _language(code: str) -> int:
    code = LANGUAGES.get(code.lower()[:2], "und") if len(code) < 3 else code.lower()[:3]
    if not code.isascii() or not code.isalpha():
        code = "und"
    return sum((ord(c) - 0x60) << (10 - 5 * i) for i, c in enumerate(code))


@dataclass
class Tags:
    """iTunes-style metadata of one file."""

    title: str
    album: str = ""
    artist: str = ""  # authors
    composer: str = ""  # narrators: Apple Books shows the composer as narrator
    date: str = ""  # YYYY or YYYY-MM-DD
    genre: str = ""
    description: str = ""
    copyright: str = ""
    language: str = "und"
    track: tuple[int, int] | None = None  # (number, total)
    audiobook: bool = False  # media kind "Audiobook" instead of "Music"
    cover: bytes = b""
    cover_type: str = ""  # image/jpeg or image/png


def _data(kind: bytes, data_type: int, value: bytes) -> bytes:
    return box(kind, box(b"data", struct.pack(">II", data_type, 0), value))


def _ilst(tags: Tags) -> bytes:
    items = []
    for kind, text in (
        (b"\xa9nam", tags.title),
        (b"\xa9alb", tags.album),
        (b"\xa9ART", tags.artist),
        (b"aART", tags.artist),
        (b"\xa9wrt", tags.composer),
        (b"\xa9day", tags.date),
        (b"\xa9gen", tags.genre),
        (b"cprt", tags.copyright),
        (b"desc", tags.description[:255]),
        (b"ldes", tags.description),
        (b"\xa9too", "oreilly-dl"),
    ):
        if text:
            items.append(_data(kind, UTF8, text.encode("utf-8")))
    if tags.track:
        items.append(_data(b"trkn", BINARY, struct.pack(">HHHH", 0, *tags.track, 0)))
    if tags.audiobook:
        items.append(_data(b"stik", INTEGER, bytes([MEDIA_KIND_AUDIOBOOK])))
    if tags.cover and tags.cover_type in ("image/jpeg", "image/png"):
        items.append(_data(b"covr", JPEG if tags.cover_type == "image/jpeg" else PNG, tags.cover))
    return box(b"ilst", *items)


def _chpl(chapters: list[tuple[str, int]], sample_rate: int) -> bytes:
    """Nero chapter list; start times in units of 100 ns."""
    entries = []
    start = 0
    for title, samples in chapters[:MAX_CHPL_CHAPTERS]:
        name = _utf8(title, 255)
        ticks = start * SAMPLES_PER_FRAME * 10_000_000 // sample_rate
        entries.append(struct.pack(">QB", ticks, len(name)) + name)
        start += samples
    return full_box(b"chpl", 1, 0, struct.pack(">IB", 0, len(entries)), *entries)


def _udta(tags: Tags, chapters: list[tuple[str, int]], sample_rate: int) -> bytes:
    hdlr = full_box(b"hdlr", 0, 0, b"\0\0\0\0mdirappl" + b"\0" * 9)
    parts = [full_box(b"meta", 0, 0, hdlr, _ilst(tags))]
    if chapters:
        parts.append(_chpl(chapters, sample_rate))
    return box(b"udta", *parts)


def _mdhd(timescale: int, duration: int, language: int) -> bytes:
    if duration > 0xFFFFFFFF:
        return full_box(b"mdhd", 1, 0, struct.pack(">QQIQHH", 0, 0, timescale, duration, language, 0))
    return full_box(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, timescale, duration, language, 0))


def _tkhd(track_id: int, duration_ms: int, flags: int, group: int, volume: int) -> bytes:
    return full_box(
        b"tkhd", 0, flags,
        struct.pack(">IIIII", 0, 0, track_id, 0, duration_ms),
        b"\0" * 8, struct.pack(">hhhH", 0, group, volume, 0), MATRIX, struct.pack(">II", 0, 0),
    )


def _hdlr(handler: bytes, name: str) -> bytes:
    return full_box(b"hdlr", 0, 0, b"\0\0\0\0", handler, b"\0" * 12, name.encode() + b"\0")


def _dinf() -> bytes:
    return box(b"dinf", full_box(b"dref", 0, 0, struct.pack(">I", 1), full_box(b"url ", 0, 1)))


def _offsets(offsets: list[int], wide: bool) -> bytes:
    if wide:
        return full_box(b"co64", 0, 0, struct.pack(f">I{len(offsets)}Q", len(offsets), *offsets))
    return full_box(b"stco", 0, 0, struct.pack(f">I{len(offsets)}I", len(offsets), *offsets))


def _esds(asc: bytes, avg_bitrate: int, max_frame: int) -> bytes:
    decoder_specific = b"\x05" + bytes([len(asc)]) + asc
    decoder_config = (
        b"\x04" + bytes([13 + len(decoder_specific)])
        + struct.pack(">BB", 0x40, 0x15)  # MPEG-4 audio, audio stream
        + max_frame.to_bytes(3, "big") + struct.pack(">II", avg_bitrate, avg_bitrate)
        + decoder_specific
    )
    sl_config = b"\x06\x01\x02"
    es = struct.pack(">HB", 1, 0) + decoder_config + sl_config
    return full_box(b"esds", 0, 0, b"\x03" + bytes([len(es)]) + es)


def _chunk_runs(count: int) -> Iterator[tuple[int, int]]:
    """(first sample, samples) of each audio chunk."""
    for start in range(0, count, CHUNK_SAMPLES):
        yield start, min(CHUNK_SAMPLES, count - start)


def _audio_trak(track: AudioTrack, sizes: array, audio_start: int, wide: bool,
                tags: Tags, duration_ms: int, has_chapters: bool) -> bytes:
    count = len(sizes)
    total_bytes = sum(sizes)
    seconds = count * SAMPLES_PER_FRAME / track.sample_rate
    avg_bitrate = int(total_bytes * 8 / seconds) if seconds else 0

    mp4a = box(
        b"mp4a",
        b"\0" * 6, struct.pack(">H", 1), b"\0" * 8,
        struct.pack(">HHHH", track.channels, 16, 0, 0),
        struct.pack(">I", track.sample_rate << 16),
        _esds(track.audio_specific_config, avg_bitrate, max(sizes, default=0)),
    )
    offsets = []
    position = audio_start
    stsc_entries = []
    for index, (start, n) in enumerate(_chunk_runs(count), 1):
        offsets.append(position)
        position += sum(sizes[start : start + n])
        if not stsc_entries or stsc_entries[-1][1] != n:
            stsc_entries.append((index, n))
    stbl = box(
        b"stbl",
        full_box(b"stsd", 0, 0, struct.pack(">I", 1), mp4a),
        full_box(b"stts", 0, 0, struct.pack(">III", 1, count, SAMPLES_PER_FRAME)),
        full_box(b"stsc", 0, 0, struct.pack(">I", len(stsc_entries)),
                 *(struct.pack(">III", first, n, 1) for first, n in stsc_entries)),
        full_box(b"stsz", 0, 0, struct.pack(">II", 0, count), _be32(sizes)),
        _offsets(offsets, wide),
    )
    minf = box(b"minf", full_box(b"smhd", 0, 0, b"\0" * 4), _dinf(), stbl)
    mdia = box(
        b"mdia",
        _mdhd(track.sample_rate, count * SAMPLES_PER_FRAME, _language(tags.language)),
        _hdlr(b"soun", "SoundHandler"),
        minf,
    )
    parts = [_tkhd(1, duration_ms, 3, 1, 0x0100)]
    if has_chapters:
        parts.append(box(b"tref", box(b"chap", struct.pack(">I", 2))))
    parts.append(mdia)
    return box(b"trak", *parts)


def _chapter_trak(chapters: list[tuple[str, int]], samples: list[bytes], start: int,
                  wide: bool, sample_rate: int, duration_ms: int, language: str) -> bytes:
    offsets = []
    for sample in samples:
        offsets.append(start)
        start += len(sample)
    deltas = [n * SAMPLES_PER_FRAME for _, n in chapters]
    stbl = box(
        b"stbl",
        full_box(b"stsd", 0, 0, struct.pack(">I", 1), box(b"text", TEXT_SAMPLE_ENTRY)),
        full_box(b"stts", 0, 0, struct.pack(">I", len(deltas)),
                 *(struct.pack(">II", 1, d) for d in deltas)),
        full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, 1, 1)),
        full_box(b"stsz", 0, 0, struct.pack(f">II{len(samples)}I", 0, len(samples),
                                            *(len(s) for s in samples))),
        _offsets(offsets, wide),
    )
    minf = box(b"minf", GMHD, _dinf(), stbl)
    mdia = box(
        b"mdia",
        _mdhd(sample_rate, sum(deltas), _language(language)),
        _hdlr(b"text", "ChapterHandler"),
        minf,
    )
    # Disabled track: players use it for chapter names and do not render it.
    return box(b"trak", _tkhd(2, duration_ms, 0, 0, 0), mdia)


def write_mp4(
    out: BinaryIO,
    track: AudioTrack,
    tags: Tags,
    first: int = 0,
    count: int | None = None,
    chapters: list[tuple[str, int]] | None = None,
    brand: bytes = b"M4B ",
) -> None:
    """Write frames [first, first + count) of `track` as an MPEG-4 audio file.

    `chapters` lists (title, number of frames) in order and must add up to
    `count`; an empty list writes no chapter data.
    """
    count = track.samples - first if count is None else count
    chapters = chapters or []
    if chapters and sum(n for _, n in chapters) != count:
        raise ValueError("chapter lengths do not add up to the audio length")
    sizes = track.sizes[first : first + count]
    text_samples = [
        struct.pack(">H", len(name)) + name + ENCD
        for name in (_utf8(title, 0xFFFF) for title, _ in chapters)
    ]
    text_bytes = sum(len(s) for s in text_samples)
    payload = text_bytes + sum(sizes)
    duration_ms = round(count * SAMPLES_PER_FRAME * MOVIE_TIMESCALE / track.sample_rate)

    ftyp = box(b"ftyp", brand, struct.pack(">I", 0x200), brand, b"M4A isomiso2mp42")
    mdat_header = (
        struct.pack(">I4sQ", 1, b"mdat", payload + 16)
        if payload + 8 > 0xFFFFFFFF
        else struct.pack(">I4s", payload + 8, b"mdat")
    )

    def moov(data_start: int, wide: bool) -> bytes:
        traks = [
            _audio_trak(track, sizes, data_start + text_bytes, wide, tags, duration_ms, bool(chapters))
        ]
        if chapters:
            traks.append(
                _chapter_trak(chapters, text_samples, data_start, wide, track.sample_rate,
                              duration_ms, tags.language)
            )
        mvhd = full_box(
            b"mvhd", 0, 0,
            struct.pack(">IIII", 0, 0, MOVIE_TIMESCALE, duration_ms),
            struct.pack(">IH", 0x10000, 0x100), b"\0" * 10, MATRIX, b"\0" * 24,
            struct.pack(">I", len(traks) + 1),
        )
        return box(b"moov", mvhd, *traks, _udta(tags, chapters, track.sample_rate))

    # Offsets depend on the size of `moov`, which does not depend on their values.
    wide = False
    head = len(ftyp) + len(moov(0, wide)) + len(mdat_header)
    if head + payload > 0xFFFFFFFF:
        wide = True
        head = len(ftyp) + len(moov(0, wide)) + len(mdat_header)
    out.write(ftyp)
    out.write(moov(head, wide))
    out.write(mdat_header)
    for sample in text_samples:
        out.write(sample)
    track.file.seek(track.byte_offset(first))
    _copy(track.file, out, payload - text_bytes)


def _copy(src: BinaryIO, dst: BinaryIO, size: int) -> None:
    left = size
    while left:
        block = src.read(min(left, 1 << 20))
        if not block:
            raise OSError("the audio spool file ended early")
        dst.write(block)
        left -= len(block)
