"""Data models for O'Reilly book content."""

import re
from array import array
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import BinaryIO

# AAC frames (MP4 samples) always hold 1024 PCM samples per channel here.
SAMPLES_PER_FRAME = 1024


def sanitize_filename(name: str) -> str:
    """Create a safe filename from a title."""
    safe = re.sub(r'[<>:"/\\|?*]', "", name)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:100]


def _xml_id(prefix: str, path: str) -> str:
    """Build a manifest id that is valid XML and unique per path."""
    return f"{prefix}-{re.sub(r'[^A-Za-z0-9._-]', '_', path)}"


@dataclass
class BookMetadata:
    """Metadata for an O'Reilly book."""

    id: str
    title: str
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)  # audiobooks only
    subtitle: str = ""
    publisher: str = ""
    description: str = ""
    isbn: str = ""
    language: str = "en"
    published: str = ""  # ISO date (YYYY-MM-DD)
    subjects: list[str] = field(default_factory=list)
    rights: str = ""
    cover_url: str = ""  # catalogue thumbnail, used only if the book has no cover page

    def __str__(self) -> str:
        authors = ", ".join(self.authors) or "Unknown"
        details = ", ".join(d for d in (self.publisher, self.published[:4]) if d)
        return f"{self.title} by {authors}" + (f" ({details})" if details else "")


@dataclass
class Chapter:
    """A content document of the book, in reading order."""

    path: str  # path inside the original EPUB, e.g. "ch01.html"
    title: str
    content_url: str
    order: int
    html: str = ""

    @property
    def filename(self) -> str:
        """Path of the document inside the generated EPUB."""
        return str(PurePosixPath(self.path).with_suffix(".xhtml"))

    @property
    def uid(self) -> str:
        return _xml_id("c", self.path)

    def __str__(self) -> str:
        return f"Chapter({self.order}: {self.title})"


@dataclass
class Asset:
    """A non-HTML file of the book (image, stylesheet, font)."""

    path: str
    media_type: str
    data: bytes = b""

    @property
    def uid(self) -> str:
        return _xml_id("a", self.path)


@dataclass
class TocEntry:
    """A node of the nested table of contents."""

    title: str
    path: str
    fragment: str = ""
    children: list["TocEntry"] = field(default_factory=list)


@dataclass
class Book:
    """Complete book with metadata, content and assets."""

    metadata: BookMetadata
    chapters: list[Chapter] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    toc: list[TocEntry] = field(default_factory=list)
    cover: Asset | None = None

    def __str__(self) -> str:
        return f"Book({self.metadata.title}, {len(self.chapters)} chapters)"


@dataclass
class AudioChapter:
    """A chapter of an audiobook: one O'Reilly audio clip."""

    title: str
    reference_id: str  # e.g. "9781633437166AU-bll_ch1"
    ourn: str = ""
    first_sample: int = 0  # index of its first AAC frame in the book's track
    samples: int = 0  # number of AAC frames

    def seconds(self, sample_rate: int) -> float:
        return self.samples * SAMPLES_PER_FRAME / sample_rate if sample_rate else 0.0


@dataclass
class AudioTrack:
    """Raw AAC frames of the whole book, spooled to a temporary file."""

    file: BinaryIO
    audio_specific_config: bytes = b""
    sample_rate: int = 0
    channels: int = 0
    sizes: array = field(default_factory=lambda: array("I"))

    @property
    def samples(self) -> int:
        return len(self.sizes)

    def byte_offset(self, sample: int) -> int:
        """Position of frame `sample` inside the spool file."""
        return sum(self.sizes[:sample])


@dataclass
class Audiobook:
    """An audiobook with its metadata, chapters, audio and cover."""

    metadata: BookMetadata
    chapters: list[AudioChapter]
    track: AudioTrack
    cover: Asset | None = None

    @property
    def seconds(self) -> float:
        rate = self.track.sample_rate
        return self.track.samples * SAMPLES_PER_FRAME / rate if rate else 0.0
