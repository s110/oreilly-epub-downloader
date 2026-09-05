# O'Reilly EPUB Downloader

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)

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

## Finding Book IDs

The book ID is the number in the O'Reilly URL:
- URL: `https://learning.oreilly.com/library/view/ai-engineering/9781098166298/`
- Book ID: `9781098166298`

## Refreshing Cookies

Cookies expire periodically. When downloads fail, re-export cookies from your browser.

## Requirements

- Python 3.11+
- Active O'Reilly Learning subscription
