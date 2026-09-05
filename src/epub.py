"""EPUB generation from O'Reilly book content."""

import itertools
import posixpath
import re
from pathlib import Path, PurePosixPath

from ebooklib import epub
from rich.console import Console

from .models import Book, Chapter, TocEntry

console = Console()


def create_epub(book: Book, output_path: Path) -> Path:
    """Create an EPUB file from book content.

    Args:
        book: Complete book with metadata, chapters and assets
        output_path: Path to save the EPUB file

    Returns:
        Path to the created EPUB file
    """
    console.print(f"[bold]Creating EPUB:[/] {book.metadata.title}")

    epub_book = epub.EpubBook()
    _add_metadata(epub_book, book)

    if book.cover:
        epub_book.set_cover(book.cover.path, book.cover.data, create_page=False)

    stylesheets = _add_assets(epub_book, book)
    documents = _add_chapters(epub_book, book, stylesheets)
    if not documents:
        raise RuntimeError(
            "No chapters with content were found. "
            "This usually means authentication failed or book access is restricted."
        )

    epub_book.toc = _build_toc(book.toc, documents, book.chapters)
    epub_book.spine = list(documents.values())

    cover_page = next((c for c in book.chapters if c.path in documents and _is_cover_page(c)), None)
    if cover_page:
        epub_book.guide.append({"type": "cover", "href": cover_page.filename, "title": "Cover"})
    body_pages = [c for c in book.chapters if c.path in documents and c is not cover_page]
    start_page = next(
        (c for c in body_pages if "titlepage" in PurePosixPath(c.path).stem.lower()),
        body_pages[0] if body_pages else None,
    )
    if start_page:
        epub_book.guide.append(
            {"type": "text", "href": start_page.filename, "title": "Start of Text"}
        )

    epub_book.add_item(epub.EpubNcx())
    epub_book.add_item(epub.EpubNav())

    output_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(output_path), epub_book, {"epub3_pages": False})
    return output_path


def _add_metadata(epub_book: epub.EpubBook, book: Book) -> None:
    m = book.metadata

    epub_book.set_identifier(f"urn:isbn:{m.isbn}" if m.isbn else f"urn:orm:book:{m.id}")
    if m.isbn:
        epub_book.add_metadata("DC", "identifier", f"urn:orm:book:{m.id}", {"id": "oreilly-id"})

    epub_book.title = m.title
    epub_book.add_metadata("DC", "title", m.title, {"id": "title"})
    epub_book.add_metadata(None, "meta", "main", {"refines": "#title", "property": "title-type"})
    if m.subtitle:
        epub_book.add_metadata("DC", "title", m.subtitle, {"id": "subtitle"})
        epub_book.add_metadata(
            None, "meta", "subtitle", {"refines": "#subtitle", "property": "title-type"}
        )

    epub_book.set_language(m.language)
    for i, author in enumerate(m.authors, start=1):
        uid = f"creator{i}"
        epub_book.add_metadata("DC", "creator", author, {"id": uid})
        epub_book.add_metadata(
            None, "meta", _file_as(author), {"refines": f"#{uid}", "property": "file-as"}
        )
        epub_book.add_metadata(
            None,
            "meta",
            "aut",
            {"refines": f"#{uid}", "property": "role", "scheme": "marc:relators"},
        )

    if m.publisher:
        epub_book.add_metadata("DC", "publisher", m.publisher)
    if m.published:
        epub_book.add_metadata("DC", "date", m.published)
    if m.description:
        epub_book.add_metadata("DC", "description", m.description)
    if m.rights:
        epub_book.add_metadata("DC", "rights", m.rights)
    for subject in m.subjects:
        epub_book.add_metadata("DC", "subject", subject)

    if any(a.media_type.startswith("font/") for a in book.assets):
        # Tells Apple Books to honour the embedded fonts instead of its own.
        epub_book.add_prefix(
            "ibooks", "http://vocabulary.itunes.apple.com/rdf/ibooks/vocabulary-extensions-1.0/"
        )
        epub_book.add_metadata(None, "meta", "true", {"property": "ibooks:specified-fonts"})


def _file_as(name: str) -> str:
    """Sort key for an author name: "Grootendorst, Maarten"."""
    parts = name.split()
    if len(parts) < 2:
        return name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _add_assets(epub_book: epub.EpubBook, book: Book) -> list[epub.EpubItem]:
    """Add images, fonts and stylesheets; return the stylesheet items."""
    stylesheets: list[epub.EpubItem] = []
    for asset in book.assets:
        if asset is book.cover:
            continue  # already added by set_cover
        item = epub.EpubItem(
            uid=asset.uid,
            file_name=asset.path,
            media_type=asset.media_type,
            content=asset.data,
        )
        epub_book.add_item(item)
        if asset.media_type == "text/css":
            stylesheets.append(item)

    if not stylesheets:
        item = epub.EpubItem(
            uid="style-default",
            file_name="style/default.css",
            media_type="text/css",
            content=DEFAULT_CSS.encode("utf-8"),
        )
        epub_book.add_item(item)
        stylesheets.append(item)
    return stylesheets


def _add_chapters(
    epub_book: epub.EpubBook, book: Book, stylesheets: list[epub.EpubItem]
) -> dict[str, epub.EpubHtml]:
    """Add one XHTML document per chapter, keyed by original path, in reading order."""
    documents: dict[str, epub.EpubHtml] = {}
    for chapter in book.chapters:
        if len(chapter.html.strip()) < 50:
            console.print(f"[dim]Skipping empty document: {chapter.title}[/]")
            continue

        document = epub.EpubHtml(
            uid=chapter.uid,
            file_name=chapter.filename,
            title=chapter.title,
            lang=book.metadata.language,
        )
        document.content = chapter.html.encode("utf-8")
        if "<math" in chapter.html:
            document.properties.append("mathml")
        if "<svg" in chapter.html:
            document.properties.append("svg")
        chapter_dir = posixpath.dirname(chapter.filename) or "."
        for stylesheet in stylesheets:
            document.add_link(
                href=posixpath.relpath(stylesheet.file_name, chapter_dir),
                rel="stylesheet",
                type="text/css",
            )
        epub_book.add_item(document)
        documents[chapter.path] = document
    return documents


def _build_toc(
    entries: list[TocEntry], documents: dict[str, epub.EpubHtml], chapters: list[Chapter]
) -> list:
    """Convert the nested TOC into ebooklib Sections/Links, dropping dangling anchors."""
    ids = {c.path: set(re.findall(r'\bid="([^"]+)"', c.html)) for c in chapters}
    counter = itertools.count(1)

    def convert(entry: TocEntry) -> list:
        children = [node for child in entry.children for node in convert(child)]
        document = documents.get(entry.path)
        if document is None:
            return children  # document was skipped: hoist its children
        href = document.file_name
        if entry.fragment and entry.fragment in ids.get(entry.path, ()):
            href += f"#{entry.fragment}"
        if children:
            return [(epub.Section(entry.title, href), children)]
        return [epub.Link(href, entry.title, uid=f"toc{next(counter)}")]

    toc = [node for entry in entries for node in convert(entry)]
    if not toc:
        toc = [
            epub.Link(d.file_name, d.title, uid=f"toc{next(counter)}") for d in documents.values()
        ]
    return toc


def _is_cover_page(chapter: Chapter) -> bool:
    return "cover" in PurePosixPath(chapter.path).stem.lower() or 'data-type="cover"' in chapter.html


# Used only when the book does not ship its own stylesheet.
DEFAULT_CSS = """\
body {
    font-family: Georgia, "Times New Roman", serif;
    line-height: 1.6;
    margin: 1em;
}

h1, h2, h3, h4, h5, h6 {
    font-family: Helvetica, Arial, sans-serif;
    line-height: 1.2;
    margin: 1.5em 0 0.5em;
}

h1 { font-size: 1.8em; }
h2 { font-size: 1.5em; }
h3 { font-size: 1.3em; }

p { margin: 0.8em 0; }

pre, code {
    font-family: "Courier New", Courier, monospace;
    font-size: 0.9em;
}

pre {
    background-color: #f4f4f4;
    border: 1px solid #ddd;
    padding: 1em;
    white-space: pre-wrap;
    word-wrap: break-word;
}

img {
    max-width: 100%;
    height: auto;
}

figure {
    margin: 1em 0;
    text-align: center;
}

figcaption, div.figure h6 {
    font-size: 0.9em;
    font-style: italic;
    font-weight: normal;
}

table {
    border-collapse: collapse;
    margin: 1em 0;
    width: 100%;
}

th, td {
    border: 1px solid #ddd;
    padding: 0.5em;
    text-align: left;
}

blockquote {
    border-left: 4px solid #ddd;
    color: #555;
    margin: 1em 0;
    padding: 0.5em 1em;
}

div[data-type="note"], div[data-type="tip"], div[data-type="warning"], div[data-type="caution"] {
    border-left: 4px solid #999;
    margin: 1em 0;
    padding: 0.5em 1em;
}
"""
