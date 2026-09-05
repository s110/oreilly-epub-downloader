"""Data models for O'Reilly book content."""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath


def _xml_id(prefix: str, path: str) -> str:
    """Build a manifest id that is valid XML and unique per path."""
    return f"{prefix}-{re.sub(r'[^A-Za-z0-9._-]', '_', path)}"


@dataclass
class BookMetadata:
    """Metadata for an O'Reilly book."""

    id: str
    title: str
    authors: list[str] = field(default_factory=list)
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
