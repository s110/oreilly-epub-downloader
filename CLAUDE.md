# oreilly-epub-downloader

CLI (`oreilly-dl`, `src/cli.py`) that downloads an O'Reilly book with cookie
auth (`src/client.py`) and writes an EPUB (`src/epub.py`). Never read or commit
`cookies.json`, and never commit real book content.

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

Takes about 30 s (the client's human-like delays are real). Offline: the CLI
runs as a subprocess with `OREILLY_DL_BASE_URL` pointing at
`tests/e2e/fake_oreilly.py` on 127.0.0.1 and a dead proxy for everything else,
so it never reaches oreilly.com.

**Scenario.** A fictitious Spanish book (`tests/e2e/fixtures/`) with non-ASCII
title and authors: parts > chapters > sections three levels deep, a paginated
chapter list, a TOC fragment that does not exist, images by relative path,
by files-API URL and by reader URL (the last two missing from the file listing),
an image that 404s, an image that 500s once, cross-chapter links and footnotes,
reader markup to strip, and a second run with an expired cookie (401).

**Artifact.** `artifacts/e2e/fictitious_book.epub` and
`artifacts/e2e/fictitious_book.json` (gitignored): per-check status, the EPUB's
normalized sha256 (sorted extracted entries; `dcterms:modified` blanked) and the
epubcheck result (`not run` when epubcheck is not installed; set `EPUBCHECK_JAR`
with Java available to enable it).

**Verify.** Exit 0 and `"result": "pass"` in the JSON. Re-running must give a
byte-identical JSON (same hash). Checks that fail on a known bug are listed in
`KNOWN_FAILURES` in `run_e2e.py` with the reason and show as `known_failure`;
when a fix makes one pass the run fails with `unexpected_pass` until the entry
is removed. Do not weaken a check to make it pass.
