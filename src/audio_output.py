"""Write a downloaded audiobook to disk.

Two layouts:

- one `.m4b` with chapters, cover and tags, for audiobook apps (Apple Books,
  BookPlayer, Smart AudioBook Player, VLC...);
- one `.m4a` per chapter in a folder, with an `.m3u8` playlist, the cover and,
  when a base URL is given, a podcast feed (`feed.xml`) that a podcast app can
  subscribe to once the folder is served at that URL.
"""

import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote

from .m4b import Tags, write_mp4
from .models import Audiobook, sanitize_filename

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
FEED_NAME = "feed.xml"
# Characters kept as they are when the feed's base URL is quoted: the URL
# syntax and existing %-escapes, so only spaces and non-ASCII get escaped.
URL_SAFE = ":/?#[]@!$&'()*+,;=%~"


@contextmanager
def _atomic(path: Path):
    """Open `path` for writing; it only appears once complete."""
    part = path.with_name(path.name + ".part")
    try:
        with part.open("wb") as out:
            yield out
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _tags(book: Audiobook, title: str, track: tuple[int, int] | None = None) -> Tags:
    m = book.metadata
    return Tags(
        title=title,
        album=m.title,
        artist=", ".join(m.authors),
        composer=", ".join(m.narrators),
        date=m.published,
        genre="Audiobook",
        description=m.description,
        copyright=m.rights,
        language=m.language,
        track=track,
        audiobook=True,
        cover=book.cover.data if book.cover else b"",
        cover_type=book.cover.media_type if book.cover else "",
    )


def write_m4b(book: Audiobook, path: Path) -> Path:
    """The whole audiobook as one file with a chapter per O'Reilly clip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    chapters = [(c.title, c.samples) for c in book.chapters]
    with _atomic(path) as out:
        write_mp4(out, book.track, _tags(book, book.metadata.title), chapters=chapters)
    return path


def chapter_filenames(book: Audiobook) -> list[str]:
    width = max(2, len(str(len(book.chapters))))
    return [
        f"{i:0{width}d} - {sanitize_filename(c.title) or 'Chapter'}.m4a"
        for i, c in enumerate(book.chapters, 1)
    ]


def write_chapters(book: Audiobook, directory: Path, feed_url: str | None = None) -> list[Path]:
    """One M4A per chapter, a playlist, the cover and optionally a podcast feed."""
    directory.mkdir(parents=True, exist_ok=True)
    names = chapter_filenames(book)
    total = len(book.chapters)
    written = []
    for i, (chapter, name) in enumerate(zip(book.chapters, names, strict=True), 1):
        with _atomic(directory / name) as out:
            write_mp4(
                out,
                book.track,
                _tags(book, chapter.title, (i, total)),
                first=chapter.first_sample,
                count=chapter.samples,
                brand=b"M4A ",
            )
        written.append(directory / name)

    cover_name = ""
    if book.cover:
        cover_name = "cover.png" if book.cover.media_type == "image/png" else "cover.jpg"
        with _atomic(directory / cover_name) as out:
            out.write(book.cover.data)
        written.append(directory / cover_name)

    playlist = directory / f"{sanitize_filename(book.metadata.title) or 'playlist'}.m3u8"
    rate = book.track.sample_rate
    lines = ["#EXTM3U"]
    for chapter, name in zip(book.chapters, names, strict=True):
        seconds = round(chapter.samples * 1024 / rate) if rate else -1
        lines += [f"#EXTINF:{seconds},{chapter.title}", name]
    with _atomic(playlist) as out:
        out.write(("\n".join(lines) + "\n").encode("utf-8"))
    written.append(playlist)

    if feed_url:
        with _atomic(directory / FEED_NAME) as out:
            out.write(podcast_feed(book, names, [p.stat().st_size for p in written[:total]],
                                   feed_url, cover_name))
        written.append(directory / FEED_NAME)
    return written


def podcast_feed(
    book: Audiobook, names: list[str], sizes: list[int], base_url: str, cover_name: str
) -> bytes:
    """RSS 2.0 feed with Apple Podcasts tags; one episode per chapter, in order.

    `itunes:type` serial and `itunes:episode` make apps list chapter 1 first;
    publication dates one minute apart keep that order in apps that sort by
    date. Everything is derived from the book, so the feed is reproducible.
    """
    base = quote(base_url if base_url.endswith("/") else base_url + "/", safe=URL_SAFE)
    m = book.metadata
    ET.register_namespace("itunes", ITUNES)
    it = f"{{{ITUNES}}}"

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    def add(parent: ET.Element, tag: str, text: str = "", **attrs: str) -> ET.Element:
        element = ET.SubElement(parent, tag, attrs)
        if text:
            element.text = text
        return element

    add(channel, "title", m.title)
    add(channel, "link", base)
    add(channel, "description", m.description or m.title)
    add(channel, "language", m.language)
    if m.rights:
        add(channel, "copyright", m.rights)
    add(channel, f"{it}author", ", ".join(m.authors) or m.publisher)
    add(channel, f"{it}type", "serial")
    add(channel, f"{it}explicit", "false")
    if cover_name:
        add(channel, f"{it}image", href=base + quote(cover_name))

    try:
        start = datetime.fromisoformat(m.published).replace(tzinfo=UTC)
    except ValueError:
        start = datetime(2000, 1, 1, tzinfo=UTC)
    rate = book.track.sample_rate
    for i, (chapter, name, size) in enumerate(zip(book.chapters, names, sizes, strict=True), 1):
        item = add(channel, "item")
        add(item, "title", chapter.title)
        add(item, f"{it}episode", str(i))
        add(item, f"{it}episodeType", "full")
        add(item, "enclosure", url=base + quote(name), length=str(size), type="audio/x-m4a")
        add(item, "guid", chapter.ourn or f"{m.id}:{chapter.reference_id}", isPermaLink="false")
        add(item, "pubDate", format_datetime(start + timedelta(minutes=i - 1)))
        if rate:
            add(item, f"{it}duration", str(round(chapter.samples * 1024 / rate)))
    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"
