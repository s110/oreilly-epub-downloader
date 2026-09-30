# oreilly-epub-downloader

CLI (`oreilly-dl`, `src/cli.py`) that downloads an O'Reilly book with cookie
auth (`src/client.py`) and writes an EPUB (`src/epub.py`). Audiobooks (IDs
ending in `AU`) go through Kaltura HLS (`src/audiobook.py`, `src/hls.py`) and
are written as M4B/M4A with chapters (`src/m4b.py`, `src/audio_output.py`).
Never read or commit `cookies.json`, and never commit real book content.

## Testing

- NEVER write unit tests after you write code.
- Highly prefer E2E tests as the sole testing mechanism. Use them to verify complex features work. At the end of E2E tests, produce a verifiable and repeatable artifact.
- If you must test a system in isolation, FIRST write all the ways it could fail, THEN write the code.
- When writing E2E tests don't pick the simplest possible scenario to prove it works; pick a medium to hard scenario when verifying the work with E2E tests.
- Tautological tests considered harmful.
- Change-detector tests considered harmful.
- Do not create regression tests for bug fixes without a genuine gap in behavior testing.

**Command** (from the repo root, project installed with `pip install -e .`):

```bash
python tests/e2e/run_e2e.py
```

Takes about 75 s (the client's human-like delays are real). Offline: the CLI
runs as a subprocess with `OREILLY_DL_BASE_URL` and `OREILLY_DL_KALTURA_URL`
pointing at `tests/e2e/fake_oreilly.py` on 127.0.0.1 and a dead proxy for
everything else, so it never reaches oreilly.com or kaltura.com.

**Scenario.** A fictitious Spanish book (`tests/e2e/fixtures/`) with non-ASCII
title and authors: parts > chapters > sections three levels deep, a paginated
chapter list, a TOC fragment that does not exist, images by relative path,
by files-API URL and by reader URL (the last two missing from the file listing),
an image that 404s, an image that always 503s, an image and a chapter that
500 once, a stylesheet that answers 429 with Retry-After once, cross-chapter
links and footnotes, reader markup to strip, and a second run with an expired
cookie (401).

**Scenario 2 (`tests/e2e/audiobook_e2e.py`).** A fictitious Spanish audiobook
(`tests/e2e/fixtures/audiobook/`, synthetic tones made by `make_media.py`)
with four chapters over HLS: a chapter with two flavors (the lower bitrate
listed first), a segment that 503s once and one that 429s once, a first
Kaltura session that is rejected (INVALID_KS), a chapter title with `/`, a
run where the playlist API 404s and the player page must be read instead
(the M4B must be byte-identical), a `--feed-url` run (per-chapter M4A and
podcast feed) and an expired cookie. The audio is compared frame by frame
with `expected.json` (FFmpeg's demux of the fixtures).

**Artifact.** `artifacts/e2e/fictitious_book.epub` and
`artifacts/e2e/fictitious_book.json` (gitignored): per-check status, the EPUB's
normalized sha256 (sorted extracted entries; `dcterms:modified` blanked) and the
epubcheck result (`not run` when epubcheck is not installed; set `EPUBCHECK_JAR`
with Java available to enable it). For scenario 2,
`artifacts/e2e/fictitious_audiobook.m4b` and `.json`: per-check status, the
M4B's sha256 (it has no timestamps) and a decode by PyAV (`not run` when
`pip install av` was not done).

**Verify.** Exit 0 and `"result": "pass"` in both JSON files. Re-running must give a
byte-identical JSON (same hash). Checks that fail on a known bug are listed in
`KNOWN_FAILURES` in `run_e2e.py` with the reason and show as `known_failure`;
when a fix makes one pass the run fails with `unexpected_pass` until the entry
is removed. Do not weaken a check to make it pass.
