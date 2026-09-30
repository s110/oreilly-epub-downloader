"""HLS playlists and MPEG-TS audio segments, reduced to raw AAC frames.

O'Reilly plays audiobooks through Kaltura as HLS: a master playlist with one
variant per bitrate, a media playlist per variant and MPEG-TS segments that
carry AAC in ADTS framing. The M4B writer needs the raw AAC frames and the
stream configuration, so this module takes the segments apart; no external
tool (ffmpeg) is involved.
"""

import re
from dataclasses import dataclass
from urllib.parse import urljoin

# MPEG-4 sampling frequency index -> Hz (ISO/IEC 14496-3, 1.6.3.4).
SAMPLE_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]
TS_PACKET = 188
# PMT stream types: 0x0F is AAC with ADTS framing; 0x11 (LATM) is not supported.
STREAM_TYPE_ADTS = 0x0F


class HlsError(Exception):
    """The stream uses a feature this downloader does not handle, or is corrupt."""


@dataclass
class Variant:
    url: str
    bandwidth: int


@dataclass
class Segment:
    url: str
    duration: float


def _attributes(line: str) -> dict[str, str]:
    """`#TAG:A=1,B="x,y"` -> {"A": "1", "B": "x,y"}."""
    _, _, attrs = line.partition(":")
    return {
        key: value.strip('"')
        for key, value in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', attrs)
    }


def parse_master(text: str, base_url: str) -> list[Variant]:
    """Variants of a master playlist; empty when `text` is already a media playlist."""
    variants = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = _attributes(line)
        elif line and not line.startswith("#") and pending is not None:
            bandwidth = int(pending.get("BANDWIDTH", "0") or 0)
            variants.append(Variant(url=urljoin(base_url, line), bandwidth=bandwidth))
            pending = None
    return variants


def parse_media(text: str, base_url: str) -> list[Segment]:
    """Segments of a media playlist, in order.

    Encrypted segments, fMP4 (`#EXT-X-MAP`) and byte ranges are refused with
    HlsError instead of producing a file that does not play.
    """
    if not text.lstrip().startswith("#EXTM3U"):
        raise HlsError("not an HLS playlist")
    segments = []
    duration: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-KEY"):
            method = _attributes(line).get("METHOD", "NONE")
            if method != "NONE":
                raise HlsError(f"encrypted HLS ({method}) is not supported")
        elif line.startswith("#EXT-X-MAP"):
            raise HlsError("fragmented MP4 HLS segments are not supported")
        elif line.startswith("#EXT-X-BYTERANGE"):
            raise HlsError("HLS byte-range segments are not supported")
        elif line.startswith("#EXTINF"):
            duration = float(line.partition(":")[2].split(",")[0] or 0)
        elif line and not line.startswith("#"):
            segments.append(Segment(url=urljoin(base_url, line), duration=duration or 0.0))
            duration = None
    if not segments:
        raise HlsError("the HLS playlist has no segments")
    return segments


@dataclass(frozen=True)
class AacConfig:
    """What the MP4 sample description needs, taken from the ADTS headers."""

    object_type: int  # 2 = AAC LC
    frequency_index: int
    channels: int

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATES[self.frequency_index]

    @property
    def audio_specific_config(self) -> bytes:
        value = (self.object_type << 11) | (self.frequency_index << 7) | (self.channels << 3)
        return value.to_bytes(2, "big")

    def __str__(self) -> str:
        layout = {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels} channels")
        return f"AAC {self.sample_rate} Hz {layout}"


class AdtsReader:
    """Splits an ADTS byte stream into raw AAC frames.

    Data is fed in pieces (one PES payload or segment at a time); a frame cut
    between two pieces is kept until the rest arrives.
    """

    def __init__(self) -> None:
        self.config: AacConfig | None = None
        self._pending = b""

    def feed(self, data: bytes) -> list[bytes]:
        buf = self._pending + data
        frames = []
        pos = 0
        while len(buf) - pos >= 7:
            if buf[pos] != 0xFF or buf[pos + 1] & 0xF0 != 0xF0:
                raise HlsError(f"ADTS sync lost at byte {pos}")
            protection_absent = buf[pos + 1] & 0x01
            profile = buf[pos + 2] >> 6
            frequency_index = (buf[pos + 2] >> 2) & 0x0F
            channels = ((buf[pos + 2] & 0x01) << 2) | (buf[pos + 3] >> 6)
            length = ((buf[pos + 3] & 0x03) << 11) | (buf[pos + 4] << 3) | (buf[pos + 5] >> 5)
            raw_blocks = buf[pos + 6] & 0x03
            header = 7 if protection_absent else 9
            if length < header:
                raise HlsError(f"ADTS frame with invalid length {length}")
            if len(buf) - pos < length:
                break
            if raw_blocks:
                raise HlsError("ADTS frames with several raw data blocks are not supported")
            if frequency_index >= len(SAMPLE_RATES) or channels == 0:
                raise HlsError("unsupported AAC configuration in the ADTS header")
            config = AacConfig(profile + 1, frequency_index, channels)
            if self.config is None:
                self.config = config
            elif config != self.config:
                raise HlsError(f"the AAC configuration changes mid-stream ({self.config} -> {config})")
            frames.append(buf[pos + header : pos + length])
            pos += length
        self._pending = buf[pos:]
        return frames

    def finish(self) -> int:
        """Bytes left over that do not form a complete frame (and are dropped)."""
        left, self._pending = len(self._pending), b""
        return left


def _skip_id3(data: bytes) -> bytes:
    """Packed-audio HLS segments may start with an ID3v2 tag (timestamps)."""
    while data[:3] == b"ID3" and len(data) >= 10:
        size = 0
        for byte in data[6:10]:
            size = (size << 7) | (byte & 0x7F)
        footer = 10 if data[5] & 0x10 else 0
        data = data[10 + size + footer :]
    return data


def _section(payload: bytes, pusi: bool) -> bytes:
    """PSI section of a TS payload (skips the pointer field)."""
    if pusi and payload:
        payload = payload[1 + payload[0] :]
    return payload


def _pes_payload(pes: bytes) -> bytes:
    if pes[:3] != b"\x00\x00\x01" or len(pes) < 9:
        raise HlsError("malformed PES packet in the audio stream")
    return pes[9 + pes[8] :]


def aac_elementary_stream(segment: bytes) -> bytes:
    """ADTS bytes carried by one HLS segment (MPEG-TS or packed audio)."""
    data = _skip_id3(segment)
    if data[:2] and data[0] == 0xFF and data[1] & 0xF0 == 0xF0:
        return data
    if not data or data[0] != 0x47 or len(data) % TS_PACKET:
        raise HlsError("segment is neither MPEG-TS nor ADTS audio")

    pmt_pid: int | None = None
    audio_pid: int | None = None
    pes: bytearray | None = None
    out = bytearray()
    for off in range(0, len(data), TS_PACKET):
        packet = data[off : off + TS_PACKET]
        if packet[0] != 0x47:
            raise HlsError(f"MPEG-TS sync lost at byte {off}")
        pusi = bool(packet[1] & 0x40)
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        control = (packet[3] >> 4) & 0x03
        start = 4
        if control & 0x02:
            start += 1 + packet[4]
        if not control & 0x01 or start >= TS_PACKET:
            continue
        payload = packet[start:]

        if pid == 0 and pmt_pid is None:
            section = _section(payload, pusi)
            # table header (8 bytes), then program_number/PID pairs; skip program 0 (NIT).
            section_end = 3 + (((section[1] & 0x0F) << 8) | section[2]) - 4
            for i in range(8, section_end, 4):
                if (section[i] << 8) | section[i + 1]:
                    pmt_pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
                    break
        elif pid == pmt_pid and audio_pid is None:
            section = _section(payload, pusi)
            section_end = 3 + (((section[1] & 0x0F) << 8) | section[2]) - 4
            info_length = ((section[10] & 0x0F) << 8) | section[11]
            i = 12 + info_length
            types = []
            while i + 5 <= section_end:
                stream_type = section[i]
                stream_pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
                types.append(stream_type)
                if stream_type == STREAM_TYPE_ADTS:
                    audio_pid = stream_pid
                    break
                i += 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
            if audio_pid is None:
                raise HlsError(f"no ADTS AAC stream in the segment (stream types {types})")
        elif pid == audio_pid and audio_pid is not None:
            if pusi:
                if pes:
                    out += _pes_payload(bytes(pes))
                pes = bytearray(payload)
            elif pes is not None:
                pes += payload
    if pes:
        out += _pes_payload(bytes(pes))
    if audio_pid is None:
        raise HlsError("no audio stream found in the MPEG-TS segment")
    return bytes(out)
