"""Command-line interface for O'Reilly book downloader."""

import re
import sys
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .audio_output import FEED_NAME, write_chapters, write_m4b
from .audiobook import AudiobookFetcher, is_audiobook_id
from .client import AuthError, OreillyClient
from .cookie_auth import load_cookies
from .epub import create_epub
from .models import Audiobook, Book, sanitize_filename

console = Console()


def extract_book_id(book_input: str) -> str:
    """Extract book ID from URL or direct input."""
    # Audiobook ids (ISBN + "AU") keep their suffix, in upper case as the API
    # expects, wherever they appear: /library/view/, /videos/, a URN or alone.
    match = re.search(r"(?<![0-9A-Za-z])(\d{9,13}X?AU)(?![0-9A-Za-z])", book_input, re.I)
    if match:
        return match.group(1).upper()

    url_pattern = r"learning\.oreilly\.com/library/view/[^/]+/(\d+)"
    match = re.search(url_pattern, book_input)
    if match:
        return match.group(1)

    # Other audiobook URLs: .../videos/<slug>/<id>/[<clip>/], urn:orm:audiobook:<id>
    match = re.search(r"/videos/[^/]+/([^/?#]+)|urn:orm:audiobook:([^:/?#]+)", book_input)
    if match:
        return match.group(1) or match.group(2)

    if re.match(r"^\d+$", book_input):
        return book_input

    isbn_match = re.search(r"(\d{10,13})", book_input)
    if isbn_match:
        return isbn_match.group(1)

    return book_input


def resolve_output(output: Path | None, book: Book) -> Path:
    """Where to write the EPUB: explicit file, a directory, or ./downloads/<title>.epub."""
    default_name = f"{sanitize_filename(book.metadata.title)}.epub"
    if output is None:
        return Path("downloads") / default_name
    if output.is_dir():
        return output / default_name
    return output if output.suffix == ".epub" else output.with_suffix(".epub")


AUDIO_SUFFIXES = (".m4b", ".m4a")


def check_audio_output(output: Path | None, split: bool) -> None:
    """Refuse, before the download, an -o the audiobook cannot be written to."""
    if output is None or output.is_dir():
        return
    if split and output.exists():
        raise click.BadParameter(
            f"{output} is a file; --split needs a folder", param_hint="-o/--output"
        )


def resolve_audio_output(output: Path | None, book: Audiobook, split: bool) -> Path:
    """Where to write the audiobook, with the same rules as resolve_output.

    An existing directory gets <title>.m4b (or the <title>/ folder with
    --split) inside it. Any other path is the target itself: the file, whose
    suffix becomes .m4b unless it is .m4b or .m4a, or with --split the folder.
    Default: ./downloads/<title>.m4b or ./downloads/<title>/.
    """
    name = sanitize_filename(book.metadata.title) or book.metadata.id
    if output is None:
        return Path("downloads") / (name if split else f"{name}.m4b")
    if output.is_dir():
        return output / name if split else output / f"{name}.m4b"
    if split or output.suffix.lower() in AUDIO_SUFFIXES:
        return output
    return output.with_suffix(".m4b")


def _duration(seconds: float) -> str:
    total = round(seconds)
    return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"


def print_audio_summary(book: Audiobook, output_path: Path, files: list[Path]) -> None:
    m = book.metadata
    track = book.track
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Title", escape(m.title))
    table.add_row("Authors", escape(", ".join(m.authors)) or "[yellow]unknown[/]")
    table.add_row("Narrators", escape(", ".join(m.narrators)) or "[dim]not listed[/]")
    table.add_row("Publisher", escape(m.publisher) or "[yellow]unknown[/]")
    table.add_row("Published", escape(m.published) or "[yellow]unknown[/]")
    table.add_row("Cover", "yes" if book.cover else "[yellow]none[/]")
    table.add_row("Chapters", str(len(book.chapters)))
    table.add_row("Length", _duration(book.seconds))
    table.add_row("Audio", f"AAC {track.sample_rate} Hz, {track.channels} ch")
    size = sum(p.stat().st_size for p in files)
    table.add_row("Size", f"{size / 1_000_000:.1f} MB in {len(files)} files" if len(files) > 1
                  else f"{size / 1_000_000:.1f} MB")
    console.print(table)
    console.print(f"[bold green]Done:[/] {escape(str(output_path))}")


def print_summary(book: Book, output_path: Path) -> None:
    m = book.metadata
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Title", m.title + (f" — {m.subtitle}" if m.subtitle else ""))
    table.add_row("Authors", ", ".join(m.authors) or "[yellow]unknown[/]")
    table.add_row("Publisher", m.publisher or "[yellow]unknown[/]")
    table.add_row("Published", m.published or "[yellow]unknown[/]")
    table.add_row("ISBN", m.isbn or "[yellow]unknown[/]")
    table.add_row("Subjects", ", ".join(m.subjects) or "[dim]none[/]")
    table.add_row("Cover", f"{book.cover.path}" if book.cover else "[yellow]none[/]")
    table.add_row("Content", f"{len(book.chapters)} documents, {len(book.assets)} assets")
    table.add_row("Size", f"{output_path.stat().st_size / 1_000_000:.1f} MB")
    console.print(table)
    console.print(f"[bold green]Done:[/] {output_path}")


@click.command()
@click.argument("book", required=True)
@click.option(
    "-c",
    "--cookies",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="Path to cookies.json file",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file or directory (defaults to ./downloads/<title>.epub or .m4b)",
)
@click.option(
    "--split",
    is_flag=True,
    help="Audiobooks: one .m4a per chapter in a folder, instead of one .m4b.",
)
@click.option(
    "--feed-url",
    metavar="URL",
    help=f"Audiobooks: also write a podcast feed ({FEED_NAME}) for the folder served "
    "at URL. Implies --split.",
)
def main(
    book: str, cookies: Path, output: Path | None, split: bool, feed_url: str | None
) -> None:
    """Download O'Reilly books as EPUB and audiobooks as M4B.

    BOOK can be a book or audiobook ID, or its full O'Reilly URL.

    \b
    Examples:
        oreilly-dl 9781098166298 -c cookies.json
        oreilly-dl "https://learning.oreilly.com/library/view/book/9781098166298/" -c cookies.json
        oreilly-dl 9781633437166AU -c cookies.json
        oreilly-dl 9781633437166AU -c cookies.json --feed-url https://example.org/llm/
    """
    book_id = extract_book_id(book)
    audiobook = is_audiobook_id(book_id) or "/videos/" in book
    if feed_url and not re.match(r"^https?://", feed_url):
        raise click.BadParameter("must start with http:// or https://", param_hint="--feed-url")
    if (split or feed_url) and not audiobook:
        raise click.UsageError("--split and --feed-url only apply to audiobooks")
    split = split or bool(feed_url)
    if audiobook:
        check_audio_output(output, split)
    kind = "audiobook" if audiobook else "book"
    console.print(f"[bold]Downloading {kind}:[/] {escape(book_id)}")

    try:
        session = load_cookies(cookies)

        if audiobook:
            with OreillyClient(session) as client, AudiobookFetcher(client) as fetcher:
                audio = fetcher.get_audiobook(book_id)
            with audio.track.file:
                output_path = resolve_audio_output(output, audio, split)
                if split:
                    files = write_chapters(audio, output_path, feed_url)
                else:
                    files = [write_m4b(audio, output_path)]
                print_audio_summary(audio, output_path, files)
            return

        with OreillyClient(session) as client:
            book_data = client.get_book(book_id)

        output_path = resolve_output(output, book_data)
        create_epub(book_data, output_path)
        print_summary(book_data, output_path)

    except AuthError as e:
        console.print(f"\n[bold red]Error:[/] {escape(str(e))}")
        console.print(
            "Log in to learning.oreilly.com in your browser, export fresh cookies "
            f"to {escape(str(cookies))} (see the README) and run the command again."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/] {escape(str(e))}")
        sys.exit(1)


if __name__ == "__main__":
    main()
