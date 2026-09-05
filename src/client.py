"""O'Reilly API client for fetching book content."""

import posixpath
import random
import re
import time
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)

from .cookie_auth import Session
from .models import Asset, Book, BookMetadata, Chapter, TocEntry

console = Console()

SITE = "https://learning.oreilly.com/"
API_BASE = f"{SITE}api/v2/"
OREILLY_HOSTS = {"learning.oreilly.com", "www.oreilly.com", "oreilly.com"}

# Files of the original EPUB that are not copied as assets: chapters are fetched
# through the chapters API and the package/NCX files are regenerated on write.
HTML_TYPES = {"text/html", "application/xhtml+xml"}
PACKAGE_TYPES = {"application/oebps-package+xml", "application/x-dtbncx+xml"}


def human_delay(min_ms: int = 300, max_ms: int = 1500) -> None:
    """Add a random human-like delay between requests."""
    time.sleep(random.randint(min_ms, max_ms) / 1000)


def _book_urn(book_id: str) -> str:
    return f"urn:orm:book:{book_id}"


def _item_path(item: dict) -> str:
    """Path of a chapter/TOC item inside the original EPUB (e.g. "ch01.html")."""
    reference_id = item.get("reference_id", "")
    if "-/" in reference_id:
        return reference_id.split("-/", 1)[1]
    ourn = item.get("ourn", "")
    if ":chapter:" in ourn:
        return ourn.split(":chapter:", 1)[1]
    return item.get("full_path", "")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _html_to_text(html: str) -> str:
    """Flatten the catalogue's HTML description into plain paragraphs."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.find_all(["p", "li"])
    paragraphs = [_collapse(b.get_text(" ")) for b in blocks] or [_collapse(soup.get_text(" "))]
    return "\n\n".join(p for p in paragraphs if p)


def _localize_css(css: str) -> str:
    """Undo the web reader's rewrite of the publisher stylesheet.

    The reader scopes every rule under `#sbo-rt-content` and replaces `body`
    with `div` (which also mangles `tbody`); it drops the `@font-face` rules,
    which are regenerated in `_font_faces`.
    """
    css = re.sub(r"#sbo-rt-content(?=\s*[{,])", "body", css)
    css = re.sub(r"#sbo-rt-content\s*>?\s*", "", css)
    return re.sub(r"(?<![\w.#-])tdiv(?![\w-])", "tbody", css)


# Family names O'Reilly's stylesheet uses for the fonts it ships.
FONT_FAMILIES = {
    "UbuntuMono-Regular": ("Ubuntu Mono", "normal", "normal"),
    "UbuntuMono-Bold": ("Ubuntu Mono Bold", "bold", "normal"),
    "UbuntuMono-Italic": ("Ubuntu Mono Ital", "normal", "italic"),
    "UbuntuMono-BoldItalic": ("Ubuntu Mono BoldItal", "bold", "italic"),
    "DejaVuSerif": ("DejaVu Serif", "normal", "normal"),
    "DejaVuSans-Bold": ("DejaVu Sans", "bold", "normal"),
}
FONT_EXTENSIONS = (".otf", ".ttf", ".woff", ".woff2")


def _is_font(asset: Asset) -> bool:
    return asset.media_type.startswith("font/") or asset.path.lower().endswith(FONT_EXTENSIONS)


def _font_faces(fonts: list[Asset], css_path: str) -> str:
    """`@font-face` rules for the shipped fonts, relative to the stylesheet."""
    rules = []
    for font in fonts:
        stem = PurePosixPath(font.path).stem
        family, weight, style = FONT_FAMILIES.get(stem) or (
            re.sub(r"[-_]", " ", stem),
            "bold" if "bold" in stem.lower() else "normal",
            "italic" if re.search(r"ital|oblique", stem, re.I) else "normal",
        )
        url = posixpath.relpath(font.path, posixpath.dirname(css_path) or ".")
        rules.append(
            f'@font-face{{font-family:"{family}";font-weight:{weight};'
            f"font-style:{style};src:url({url})}}"
        )
    return "\n" + "\n".join(rules) + "\n"


def _walk(entries: list[TocEntry]) -> Iterator[TocEntry]:
    for entry in entries:
        yield entry
        yield from _walk(entry.children)


class OreillyClient:
    """Client for interacting with O'Reilly Learning API."""

    def __init__(self, session: Session):
        self.session = session
        self.http = httpx.Client(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": session.get_cookie_header(),
                "Referer": SITE,
            },
            follow_redirects=True,
            timeout=30.0,
        )

    def get_book(self, book_id: str) -> Book:
        """Fetch a complete book: metadata, nested TOC, chapters and assets."""
        console.print(f"[bold]Fetching book:[/] {book_id}")

        metadata = self._get_metadata(book_id)
        console.print(f"[green]Found:[/] {metadata}")

        chapters = self._get_chapters(book_id)
        toc = self._get_toc(book_id, chapters)
        console.print(
            f"[green]Found[/] {len(chapters)} documents, "
            f"{sum(1 for _ in _walk(toc))} table-of-contents entries"
        )

        assets = self._fetch_assets(book_id, chapters)
        console.print(f"[green]Downloaded[/] {len(assets)} assets")

        missing = self._fetch_chapters(book_id, chapters, assets)
        if missing:
            self._fetch_missing_assets(book_id, missing, assets)
        self._read_front_matter(metadata, chapters)
        cover = self._pick_cover(chapters, assets, metadata)

        return Book(metadata=metadata, chapters=chapters, assets=assets, toc=toc, cover=cover)

    # ------------------------------------------------------------------ HTTP

    def _get_json(self, url: str, params: dict | None = None) -> Any:
        response = self.http.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def _paginate(self, url: str, params: dict | None = None) -> Iterator[dict]:
        """Yield the results of a paginated API listing."""
        while url:
            data = self._get_json(url, params=params)
            params = None
            if isinstance(data, list):
                yield from data
                return
            yield from data.get("results", [])
            url = data.get("next")
            if url:
                human_delay(200, 500)

    @staticmethod
    def _progress() -> Progress:
        return Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=console,
            transient=True,
        )

    # -------------------------------------------------------------- metadata

    def _get_metadata(self, book_id: str) -> BookMetadata:
        """Combine the EPUB record with the catalogue entry (authors, publisher, topics)."""
        response = self.http.get(f"{API_BASE}epubs/{_book_urn(book_id)}/")
        if response.status_code == 404:
            raise ValueError(f"Book not found: {book_id}")
        response.raise_for_status()
        info = response.json()

        catalog = self._get_catalog_entry(book_id)
        authors = list(catalog.get("authors") or [])
        publisher = next(iter(catalog.get("publishers") or []), "")
        if not authors or not publisher:
            page_authors, page_publisher = self._scrape_book_page(book_id)
            authors = authors or page_authors
            publisher = publisher or page_publisher

        description = catalog.get("description") or info.get("descriptions", {}).get(
            "text/html", ""
        )
        published = info.get("publication_date") or catalog.get("issued") or ""

        return BookMetadata(
            id=book_id,
            title=_collapse(info.get("title") or catalog.get("title") or f"Book {book_id}"),
            authors=authors,
            publisher=publisher,
            description=_html_to_text(description),
            isbn=info.get("isbn") or catalog.get("isbn") or "",
            language=info.get("language") or catalog.get("language") or "en",
            published=published[:10],
            subjects=[t["name"] for t in catalog.get("topics_payload", []) if t.get("name")],
            cover_url=catalog.get("cover_url", ""),
        )

    def _get_catalog_entry(self, book_id: str) -> dict:
        """Search result for the book; it carries what the EPUB record lacks."""
        try:
            data = self._get_json(f"{API_BASE}search/", params={"query": book_id, "limit": 5})
        except (httpx.HTTPError, ValueError):
            return {}
        for result in data.get("results", []):
            if result.get("archive_id") == book_id:
                return result
        return {}

    def _scrape_book_page(self, book_id: str) -> tuple[list[str], str]:
        """Fallback: authors and publisher from the book page's Open Graph tags."""
        try:
            response = self.http.get(f"{SITE}library/view/-/{book_id}/")
            response.raise_for_status()
        except httpx.HTTPError:
            return [], ""
        soup = BeautifulSoup(response.text, "lxml")
        authors = [
            m["content"] for m in soup.find_all("meta", property="og:book:author") if m.get("content")
        ]
        publisher_tag = soup.find("meta", attrs={"name": "publisher"})
        publisher = publisher_tag["content"] if publisher_tag and publisher_tag.get("content") else ""
        return authors, publisher

    def _read_front_matter(self, metadata: BookMetadata, chapters: list[Chapter]) -> None:
        """Fill subtitle, rights and publisher from the title and copyright pages."""
        for chapter in chapters[:6]:
            if not chapter.html:
                continue
            soup = BeautifulSoup(chapter.html, "lxml")
            if not metadata.subtitle and (tag := soup.select_one("p.subtitle")):
                metadata.subtitle = _collapse(tag.get_text(" "))
            if not metadata.rights and (tag := soup.select_one("p.copyright")):
                metadata.rights = _collapse(tag.get_text(" "))
            if not metadata.publisher and (tag := soup.select_one("span.publishername")):
                metadata.publisher = _collapse(tag.get_text(" "))
            if not metadata.authors and (tag := soup.select_one("p.author")):
                byline = re.sub(r"^by\s+", "", _collapse(tag.get_text(" ")), flags=re.I)
                metadata.authors = [a for a in re.split(r",\s*|\s+and\s+", byline) if a]

    # --------------------------------------------------------------- content

    def _get_chapters(self, book_id: str) -> list[Chapter]:
        """Reading-order list of content documents."""
        console.print("[dim]Fetching table of contents...[/]")
        items = self._paginate(
            f"{API_BASE}epub-chapters/",
            params={"epub_identifier": _book_urn(book_id), "limit": 100},
        )

        chapters: list[Chapter] = []
        for item in items:
            path = _item_path(item)
            if not path or not item.get("content_url"):
                continue
            title = _collapse(item.get("title") or "") or PurePosixPath(path).stem
            chapters.append(
                Chapter(
                    path=path,
                    title=title,
                    content_url=item["content_url"],
                    order=len(chapters),
                )
            )

        if not chapters:
            raise ValueError(
                f"No chapters found for {book_id}. "
                "Your cookies may have expired or the book is not in your subscription."
            )
        return chapters

    def _get_toc(self, book_id: str, chapters: list[Chapter]) -> list[TocEntry]:
        """Nested table of contents; falls back to one entry per document."""
        try:
            data = self._get_json(f"{API_BASE}epubs/{_book_urn(book_id)}/table-of-contents/")
        except (httpx.HTTPError, ValueError):
            data = []
        nodes = data if isinstance(data, list) else data.get("results", [])

        def build(node: dict) -> TocEntry:
            return TocEntry(
                title=_collapse(node.get("title") or ""),
                path=_item_path(node),
                fragment=node.get("fragment") or "",
                children=[build(child) for child in node.get("children", [])],
            )

        known = {c.path for c in chapters}
        entries = [build(n) for n in nodes]
        entries = [e for e in entries if e.path in known and e.title]
        if not entries:
            entries = [TocEntry(title=c.title, path=c.path) for c in chapters]
        return entries

    def _fetch_assets(self, book_id: str, chapters: list[Chapter]) -> list[Asset]:
        """Download every non-HTML file of the book (images, CSS, fonts) at its original path."""
        files = self._paginate(
            f"{API_BASE}epubs/{_book_urn(book_id)}/files/", params={"limit": 500}
        )
        chapter_paths = {c.path for c in chapters}
        wanted: dict[str, dict] = {}
        for file in files:
            path = file.get("full_path")
            if (
                path
                and path not in chapter_paths
                and path not in wanted
                and file.get("media_type") not in HTML_TYPES | PACKAGE_TYPES
            ):
                wanted[path] = file
        wanted = list(wanted.values())

        assets: list[Asset] = []
        with self._progress() as progress:
            task = progress.add_task("Assets", total=len(wanted))
            for file in wanted:
                name = file.get("filename") or file["full_path"]
                progress.update(task, description=f"Assets: {name[:40]}")
                human_delay(100, 300)
                try:
                    response = self.http.get(file["url"])
                    response.raise_for_status()
                except httpx.HTTPError as e:
                    console.print(f"[yellow]Warning: failed to fetch {name}: {e}[/]")
                    progress.advance(task)
                    continue

                media_type = file.get("media_type") or response.headers.get(
                    "content-type", "application/octet-stream"
                ).split(";")[0]
                data = response.content
                if media_type == "text/css":
                    data = _localize_css(response.text).encode("utf-8")

                assets.append(Asset(path=file["full_path"], media_type=media_type, data=data))
                progress.advance(task)

        fonts = [a for a in assets if _is_font(a)]
        for stylesheet in (a for a in assets if a.media_type == "text/css"):
            if fonts and b"@font-face" not in stylesheet.data:
                stylesheet.data += _font_faces(fonts, stylesheet.path).encode("utf-8")
        return assets

    def _fetch_chapters(
        self, book_id: str, chapters: list[Chapter], assets: list[Asset]
    ) -> set[str]:
        """Fetch and clean the HTML of every document with human-like pacing.

        Returns the in-book file paths referenced by the chapters that are not
        among the downloaded assets (the file listing occasionally omits some).
        """
        chapter_paths = {c.path for c in chapters}
        asset_paths = {a.path for a in assets}
        missing: set[str] = set()

        with self._progress() as progress:
            task = progress.add_task("Chapters", total=len(chapters))
            for i, chapter in enumerate(chapters):
                progress.update(task, description=f"Chapters: {chapter.title[:40]}")
                # Vary the delay more for early chapters, then settle into a rhythm.
                human_delay(1000, 2500) if i < 3 else human_delay(500, 1500)

                try:
                    response = self.http.get(chapter.content_url)
                    response.raise_for_status()
                except httpx.HTTPError as e:
                    console.print(f"[yellow]Warning: failed to fetch {chapter.title}: {e}[/]")
                    human_delay(2000, 4000)
                    progress.advance(task)
                    continue

                if "json" in response.headers.get("content-type", ""):
                    html = response.json().get("content", "")
                else:
                    html = response.text
                chapter.html = self._clean_html(
                    html, chapter, chapter_paths, asset_paths, book_id, missing
                )
                progress.advance(task)
        return missing

    def _fetch_missing_assets(self, book_id: str, paths: set[str], assets: list[Asset]) -> None:
        """Download files the chapters reference but the listing did not include."""
        for path in sorted(paths):
            human_delay(100, 300)
            try:
                response = self.http.get(f"{API_BASE}epubs/{_book_urn(book_id)}/files/{path}")
                response.raise_for_status()
            except httpx.HTTPError as e:
                console.print(f"[yellow]Warning: failed to fetch {path}: {e}[/]")
                continue
            media_type = response.headers.get("content-type", "application/octet-stream")
            assets.append(Asset(path=path, media_type=media_type.split(";")[0], data=response.content))
        console.print(f"[green]Recovered[/] {len(paths)} assets missing from the file listing")

    def _clean_html(
        self,
        html: str,
        chapter: Chapter,
        chapter_paths: set[str],
        asset_paths: set[str],
        book_id: str,
        missing: set[str] | None = None,
    ) -> str:
        """Unwrap the reader container and point every reference at the local files."""
        if not html:
            return ""
        soup = BeautifulSoup(html, "lxml")
        root = soup.find(id="sbo-rt-content") or soup.body or soup

        for tag in root.find_all("script"):
            tag.decompose()
        for tag in root.find_all(attrs={"contenteditable": True}):
            del tag["contenteditable"]
        # The reader adds pixel dimensions the publisher's EPUB does not carry;
        # combined with the stylesheet's max-width they distort figures.
        for tag in root.find_all("img"):
            del tag["width"], tag["height"]

        chapter_dir = posixpath.dirname(chapter.path)
        for tag in root.find_all(True):
            for attr in ("src", "href", "poster", "xlink:href", "data"):
                if tag.has_attr(attr) and isinstance(tag[attr], str):
                    tag[attr] = self._localize_ref(
                        tag[attr], chapter_dir, chapter_paths, asset_paths, book_id, missing
                    )

        return "".join(str(child) for child in root.children)

    @staticmethod
    def _localize_ref(
        ref: str,
        chapter_dir: str,
        chapter_paths: set[str],
        asset_paths: set[str],
        book_id: str,
        missing: set[str] | None = None,
    ) -> str:
        """Map an O'Reilly URL or in-book path to a relative path inside the EPUB.

        Returns the reference unchanged when it points outside the book. In-book
        files that are not among the assets are recorded in `missing`.
        """
        if not ref or ref.startswith(("#", "mailto:", "data:", "javascript:", "tel:")):
            return ref
        parts = urlsplit(ref)
        if parts.scheme and parts.netloc not in OREILLY_HOSTS:
            return ref

        path = unquote(parts.path)
        files_prefix = f"/api/v2/epubs/{_book_urn(book_id)}/files/"
        view_match = re.match(rf"^/library/view/[^/]+/{re.escape(book_id)}/(.+)$", path)
        in_book = True
        if path.startswith(files_prefix):
            target = path[len(files_prefix):]
        elif view_match:
            target = view_match.group(1)
        elif parts.netloc or path.startswith("/") or not path:
            return ref
        else:
            target = posixpath.normpath(posixpath.join(chapter_dir, path))
            in_book = False

        if target in chapter_paths:
            target = str(PurePosixPath(target).with_suffix(".xhtml"))
        elif target not in asset_paths:
            if not in_book or missing is None or target.endswith(".html"):
                return ref
            missing.add(target)

        local = posixpath.relpath(target, chapter_dir or ".")
        return f"{local}#{parts.fragment}" if parts.fragment else local

    def _pick_cover(
        self, chapters: list[Chapter], assets: list[Asset], metadata: BookMetadata
    ) -> Asset | None:
        """The cover page's image is the full-resolution cover; the catalogue one is a thumbnail."""
        for chapter in chapters[:5]:
            is_cover = "cover" in PurePosixPath(chapter.path).stem.lower() or (
                'data-type="cover"' in chapter.html
            )
            if not is_cover or not chapter.html:
                continue
            img = BeautifulSoup(chapter.html, "lxml").find("img")
            if not img or not img.get("src"):
                continue
            src = img["src"].split("#")[0]
            target = posixpath.normpath(posixpath.join(posixpath.dirname(chapter.path), src))
            for asset in assets:
                if asset.path == target and asset.media_type.startswith("image/"):
                    return asset

        if metadata.cover_url:
            try:
                response = self.http.get(metadata.cover_url)
                response.raise_for_status()
            except httpx.HTTPError as e:
                console.print(f"[yellow]Warning: failed to fetch cover: {e}[/]")
                return None
            media_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
            ext = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(media_type, "jpg")
            return Asset(path=f"images/cover.{ext}", media_type=media_type, data=response.content)

        return None

    def close(self) -> None:
        """Close the HTTP client."""
        self.http.close()

    def __enter__(self) -> "OreillyClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()
