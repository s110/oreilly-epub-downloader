# O'Reilly EPUB Downloader

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)

Sebastián López's ([s110](https://github.com/s110)) version of the O'Reilly EPUB downloader, maintained independently from the [original by tctibbs](https://github.com/tctibbs/oreilly-epub-downloader) (MIT), with a rewritten EPUB pipeline.

A CLI to download O'Reilly books as EPUB for offline reading. Uses cookie-based authentication to access your subscription content and generates clean EPUBs that mirror the original book:

- Full metadata for your library (Calibre, Apple Books, Kobo…): title and subtitle, authors with sort names, publisher, publication date, ISBN, description, subjects and rights.
- Full-resolution cover, declared as the EPUB cover image.
- Nested table of contents (parts → chapters → sections), taken from O'Reilly's own TOC.
- Original file names, so footnotes, cross-references and index links keep working.
- The book's own stylesheet, images and fonts, so it looks like the publisher's EPUB.

## Installation

```bash
pip install -e .
```

## Usage

### 1. Export cookies from O'Reilly

1. Log into https://learning.oreilly.com in your browser
2. Open Developer Tools (Cmd+Option+I)
3. Go to Console and run:
   ```javascript
   JSON.stringify(Object.fromEntries(document.cookie.split('; ').map(c => c.split('='))))
   ```
4. Save the output to `cookies.json`

### 2. Download books

```bash
# By book ID
oreilly-dl 9781098166298 -c cookies.json

# By URL
oreilly-dl "https://learning.oreilly.com/library/view/ai-engineering/9781098166298/" -c cookies.json

# Custom output file or directory
oreilly-dl 9781098166298 -c cookies.json -o "My Book.epub"
oreilly-dl 9781098166298 -c cookies.json -o ~/Books/
```

Example output:

![Example output](assets/example.png)

Books are saved to `./downloads/` by default.

### 3. Download audiobooks

Audiobook IDs end in `AU`. You can also paste the player URL
(`https://learning.oreilly.com/videos/<slug>/<id>/...`).

```bash
# One .m4b with chapters, cover and tags (Apple Books, BookPlayer, VLC...)
oreilly-dl 9781633437166AU -c cookies.json

# One .m4a per chapter in a folder, with an .m3u8 playlist and the cover
oreilly-dl 9781633437166AU -c cookies.json --split

# Per-chapter files plus a podcast feed (feed.xml). Put the folder on a web
# server at that URL and subscribe to <URL>/feed.xml in your podcast app.
oreilly-dl 9781633437166AU -c cookies.json --feed-url https://example.org/llm/
```

The `.m4b` carries the chapters twice (a QuickTime chapter track for Apple
players and a Nero `chpl` list for the others), the authors, the narrator (as
composer), the description, the cover and the media kind "Audiobook". The
audio is copied bit for bit from O'Reilly's stream (AAC): nothing is
re-encoded and ffmpeg is not needed. If one part of the audio cannot be
downloaded after the retries, the command stops and writes nothing, so run
it again.

Only audiobooks are supported, not video courses. Protected (DRM) or
encrypted streams stop with an error.

## Finding Book IDs

The book ID is the number in the O'Reilly URL:
- URL: `https://learning.oreilly.com/library/view/ai-engineering/9781098166298/`
- Book ID: `9781098166298`

## Failed Downloads

Timeouts, dropped connections and server errors (HTTP 5xx, 408, 429) are
retried up to three times with a short backoff, honouring `Retry-After` up to
30 seconds. A file that still cannot be downloaded is left out with a warning,
and the EPUB never points at it: a missing image shows as the text
`[Image not available: <alt text>]` (a `span.missing-image` you can style),
and a link to a missing file keeps its text but stops being a link.

## Refreshing Cookies

Cookies expire periodically. When they do, O'Reilly answers 401 Unauthorized
and the download stops with a message asking you to renew them: log in again
and repeat step 1 to overwrite `cookies.json`.

## Requirements

- Python 3.11+
- Active O'Reilly Learning subscription

## Credits

Started from [tctibbs/oreilly-epub-downloader](https://github.com/tctibbs/oreilly-epub-downloader), MIT licensed. The metadata, TOC, styling and link handling were rewritten in this version; it is not kept in sync with the original.
