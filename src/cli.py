"""Command-line interface for O'Reilly book downloader."""

import re
import sys
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from .client import OreillyClient
from .cookie_auth import load_cookies
from .epub import create_epub
from .models import Book

console = Console()


def extract_book_id(book_input: str) -> str:
    """Extract book ID from URL or direct input."""
    url_pattern = r"learning\.oreilly\.com/library/view/[^/]+/(\d+)"
    match = re.search(url_pattern, book_input)
    if match:
        return match.group(1)

    if re.match(r"^\d+$", book_input):
        return book_input

    isbn_match = re.search(r"(\d{10,13})", book_input)
    if isbn_match:
        return isbn_match.group(1)

    return book_input


def sanitize_filename(name: str) -> str:
    """Create a safe filename from book title."""
    safe = re.sub(r'[<>:"/\\|?*]', "", name)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:100]


def resolve_output(output: Path | None, book: Book) -> Path:
    """Where to write the EPUB: explicit file, a directory, or ./downloads/<title>.epub."""
    default_name = f"{sanitize_filename(book.metadata.title)}.epub"
    if output is None:
        return Path("downloads") / default_name
    if output.is_dir():
        return output / default_name
    return output if output.suffix == ".epub" else output.with_suffix(".epub")


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
    help="Output file or directory (defaults to ./downloads/<title>.epub)",
)
def main(book: str, cookies: Path, output: Path | None) -> None:
    """Download O'Reilly books as EPUB.

    BOOK can be a book ID or full O'Reilly URL.

    \b
    Examples:
        oreilly-dl 9781098166298 -c cookies.json
        oreilly-dl "https://learning.oreilly.com/library/view/book/9781098166298/" -c cookies.json
    """
    book_id = extract_book_id(book)
    console.print(f"[bold]Downloading book:[/] {book_id}")

    try:
        session = load_cookies(cookies)

        with OreillyClient(session) as client:
            book_data = client.get_book(book_id)

        output_path = resolve_output(output, book_data)
        create_epub(book_data, output_path)
        print_summary(book_data, output_path)

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
