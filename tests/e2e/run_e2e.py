"""End-to-end test: the real CLI downloads a fictitious book from a local fake API.

Run from the repository root with the project installed (`pip install -e .`):

    python tests/e2e/run_e2e.py

Nothing leaves 127.0.0.1: the CLI is pointed at `fake_oreilly.py` through
OREILLY_DL_BASE_URL and authenticates with the fake cookies in `fixtures/`.

Artifacts (same inputs, same artifact):
    artifacts/e2e/fictitious_book.epub   the EPUB the CLI produced
    artifacts/e2e/fictitious_book.json   per-check results, normalized hash, epubcheck

Exit status is 0 when every check passes or fails only as a listed known bug
(KNOWN_FAILURES). A known bug that starts passing is reported as
`unexpected_pass` and fails the run, so the list is kept honest.
"""

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup
from lxml import etree

sys.path.insert(0, str(Path(__file__).parent))
from fake_oreilly import BOOK_ID, FIXTURES, FakeOreilly  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
E2E = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts" / "e2e"
SCENARIO = "fictitious_book"
BOOK_URL = f"https://learning.oreilly.com/library/view/que-es-un-grafo/{BOOK_ID}/"
EXPECTED_FILENAME = "¿Qué es un Grafo Árboles y Ñandúes.epub"

# Checks that fail because of a real bug in the downloader, with the reason.
# Remove an entry when the bug is fixed; the run fails until you do.
KNOWN_FAILURES: dict[str, str] = {
    "no_dangling_references": (
        "an image that failed to download (the 404) keeps its "
        "<img src>, pointing at a file the EPUB does not contain (epubcheck RSC-007)"
    ),
    "expired_cookie_message_says_refresh_cookies": (
        "a 401 surfaces as the raw httpx error; the 'cookies may have expired' hint "
        "is only printed when the chapter list comes back empty"
    ),
}

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "cn": "urn:oasis:names:tc:opendocument:xmlns:container",
}

SPINE = [
    "text/cover.xhtml",
    "text/titlepage.xhtml",
    "text/copyright.xhtml",
    "text/part01.xhtml",
    "text/ch01.xhtml",
    "text/ch02.xhtml",
    "text/part02.xhtml",
    "text/ch03.xhtml",
    "text/appa.xhtml",
]

# (title, href relative to the OPF directory, children)
# fmt: off
EXPECTED_TOC = [
    ("Parte I. Fundamentos", "text/part01.xhtml#part01", [
        ("Capítulo 1. Árboles", "text/ch01.xhtml#ch01", [
            ("1.1 Raíces", "text/ch01.xhtml#sec-1-1", []),
            ("1.2 Ramas", "text/ch01.xhtml#sec-1-2", [
                ("1.2.1 Hojas", "text/ch01.xhtml#sec-1-2-1", []),
            ]),
        ]),
        ("Capítulo 2. Grafos", "text/ch02.xhtml", [
            ("2.1 Caminos", "text/ch02.xhtml#sec-2-1", []),
            # the API's fragment does not exist in the chapter: link the file
            ("2.2 Ciclos", "text/ch02.xhtml", []),
        ]),
    ]),
    ("Parte II. Aplicaciones", "text/part02.xhtml", [
        ("Capítulo 3. Ñandúes", "text/ch03.xhtml#ch03", [
            ("3.1 Rutas migratorias", "text/ch03.xhtml#sec-3-1", []),
        ]),
    ]),
    ("Apéndice A. Glosario", "text/appa.xhtml", []),
    # "Índice" points at a document the book does not have and is dropped;
    # the untitled entry is dropped too.
]
# fmt: on


class Epub:
    """Read-only view of the produced EPUB, paths relative to the OPF directory."""

    def __init__(self, path: Path):
        self.zip = zipfile.ZipFile(path)
        self.names = set(self.zip.namelist())
        container = etree.fromstring(self.zip.read("META-INF/container.xml"))
        self.opf_path = container.find(".//cn:rootfile", NS).get("full-path")
        self.base = posixpath.dirname(self.opf_path)
        self.opf = etree.fromstring(self.zip.read(self.opf_path))
        self.manifest = {
            item.get("id"): item
            for item in self.opf.findall("opf:manifest/opf:item", NS)
        }

    def zip_path(self, rel: str) -> str:
        return posixpath.normpath(posixpath.join(self.base, rel)) if self.base else rel

    def has(self, rel: str) -> bool:
        return self.zip_path(rel) in self.names

    def read(self, rel: str) -> bytes:
        return self.zip.read(self.zip_path(rel))

    def soup(self, rel: str) -> BeautifulSoup:
        return BeautifulSoup(self.read(rel), "lxml-xml")

    def resolve(self, doc: str, href: str) -> str:
        """Target of `href` found in `doc`, relative to the OPF directory."""
        path, _, fragment = href.partition("#")
        target = (
            posixpath.normpath(posixpath.join(posixpath.dirname(doc), path))
            if path
            else doc
        )
        return f"{target}#{fragment}" if fragment else target

    def ids(self, rel: str) -> set[str]:
        return {tag["id"] for tag in self.soup(rel).find_all(id=True)}

    def meta(self, prop: str, refines: str) -> list[str]:
        return [
            m.text
            for m in self.opf.findall("opf:metadata/opf:meta", NS)
            if m.get("property") == prop and m.get("refines") == refines
        ]

    def dc(self, name: str) -> list[etree._Element]:
        return self.opf.findall(f"opf:metadata/dc:{name}", NS)

    @property
    def content_docs(self) -> list[str]:
        items = {i.get("id"): i.get("href") for i in self.manifest.values()}
        return [
            items[ref.get("idref")]
            for ref in self.opf.findall("opf:spine/opf:itemref", NS)
        ]


def fixture(rel: str) -> bytes:
    return (FIXTURES / rel).read_bytes()


def run_cli(base_url: str, cookies: Path, out_dir: Path) -> subprocess.CompletedProcess:
    # Any request that is not for the fake server goes to a dead proxy and fails.
    dead_proxy = "http://127.0.0.1:9"
    env = dict(
        os.environ,
        OREILLY_DL_BASE_URL=base_url,
        HTTP_PROXY=dead_proxy,
        HTTPS_PROXY=dead_proxy,
        ALL_PROXY=dead_proxy,
        NO_PROXY="127.0.0.1",
        COLUMNS="250",
        NO_COLOR="1",
        TERM="dumb",
    )
    script = Path(sys.executable).with_name("oreilly-dl")
    cli = [str(script)] if script.exists() else [sys.executable, "-m", "src.cli"]
    return subprocess.run(
        [*cli, BOOK_URL, "-c", str(cookies), "-o", str(out_dir)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def normalized_hash(epub_path: Path, base_url: str) -> tuple[str, int]:
    """sha256 over the extracted entries, sorted by name.

    Zip timestamps are ignored by construction; `dcterms:modified` (the build
    time ebooklib stamps into the OPF) and the fake server's per-run base URL
    are blanked. Everything else counts.
    """
    digest = hashlib.sha256()
    with zipfile.ZipFile(epub_path) as zf:
        names = sorted(zf.namelist())
        for name in names:
            data = zf.read(name)
            data = re.sub(
                rb'(<meta property="dcterms:modified">)[^<]*(</meta>)', rb"\1\2", data
            )
            data = data.replace(base_url.encode(), b"{BASE}")
            digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest(), len(names)


def run_epubcheck(epub_path: Path) -> dict:
    cmd = None
    if shutil.which("epubcheck"):
        cmd = ["epubcheck"]
    elif os.environ.get("EPUBCHECK_JAR") and shutil.which("java"):
        cmd = ["java", "-jar", os.environ["EPUBCHECK_JAR"]]
    if cmd is None:
        return {
            "available": False,
            "result": "not run",
            "reason": "epubcheck not on PATH and EPUBCHECK_JAR/java not set",
        }
    proc = subprocess.run([*cmd, "-q", str(epub_path)], capture_output=True, text=True)
    messages = sorted(
        {
            re.sub(r"\(\d+,\d+\)", "", line.replace(str(epub_path), "<epub>")).strip()
            for line in (proc.stdout + proc.stderr).splitlines()
            if line.startswith(("ERROR", "FATAL", "WARNING"))
        }
    )
    return {
        "available": True,
        "result": "valid" if proc.returncode == 0 else "invalid",
        "messages": messages,
    }


class Checks:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        known = KNOWN_FAILURES.get(name)
        if ok:
            status = "unexpected_pass" if known else "pass"
        else:
            status = "known_failure" if known else "fail"
        entry = {"name": name, "status": status, "detail": detail}
        if known:
            entry["known_bug"] = known
        self.results.append(entry)

    def guard(self, name: str, fn) -> None:
        """Run a check function; an exception while inspecting counts as a failure."""
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.add(name, ok, detail)


# ---------------------------------------------------------------- the checks


def toc_from_nav(epub: Epub) -> list:
    nav_item = next(
        i for i in epub.manifest.values() if "nav" in (i.get("properties") or "")
    )
    nav_href = nav_item.get("href")
    soup = epub.soup(nav_href)
    nav = next(n for n in soup.find_all("nav") if n.get("epub:type") == "toc")

    def walk(ol) -> list:
        out = []
        for li in ol.find_all("li", recursive=False):
            head = li.find(["a", "span"], recursive=False)
            sub = li.find("ol", recursive=False)
            href = epub.resolve(nav_href, head["href"]) if head.get("href") else ""
            out.append(
                (" ".join(head.get_text().split()), href, walk(sub) if sub else [])
            )
        return out

    return walk(nav.find("ol"))


def toc_from_ncx(epub: Epub) -> list:
    ncx_item = next(
        i
        for i in epub.manifest.values()
        if i.get("media-type") == "application/x-dtbncx+xml"
    )
    ncx_href = ncx_item.get("href")
    root = etree.fromstring(epub.read(ncx_href))

    def walk(node) -> list:
        return [
            (
                " ".join(p.find("ncx:navLabel/ncx:text", NS).text.split()),
                epub.resolve(ncx_href, p.find("ncx:content", NS).get("src")),
                walk(p),
            )
            for p in node.findall("ncx:navPoint", NS)
        ]

    return walk(root.find("ncx:navMap", NS))


def check_book(
    checks: Checks,
    epub: Epub,
    stdout: str,
    base_url: str,
    statuses: dict[str, list[int]],
) -> None:
    def metadata_title():
        titles = {t.get("id"): t.text for t in epub.dc("title")}
        types = (
            epub.meta("title-type", "#title"),
            epub.meta("title-type", "#subtitle"),
        )
        want = {
            "title": "¿Qué es un Grafo? Árboles y Ñandúes",
            "subtitle": "Una guía práctica para curiosos",
        }
        return titles == want and types == (["main"], ["subtitle"]), f"titles={titles}"

    def metadata_authors():
        creators = [(c.text, c.get("id")) for c in epub.dc("creator")]
        file_as = [epub.meta("file-as", f"#{uid}") for _, uid in creators]
        roles = [epub.meta("role", f"#{uid}") for _, uid in creators]
        ok = (
            [name for name, _ in creators] == ["José Ñúñez", "Zoë Østergaard"]
            and file_as == [["Ñúñez, José"], ["Østergaard, Zoë"]]
            and roles == [["aut"], ["aut"]]
        )
        return ok, f"creators={[n for n, _ in creators]} file_as={file_as}"

    def metadata_identity():
        uid = epub.opf.get("unique-identifier")
        idents = {i.get("id"): i.text for i in epub.dc("identifier")}
        values = {
            "unique": idents.get(uid),
            "identifiers": sorted(idents.values()),
            "language": [e.text for e in epub.dc("language")],
            "date": [e.text for e in epub.dc("date")],
            # the catalogue's publisher wins over the copyright page's
            "publisher": [e.text for e in epub.dc("publisher")],
        }
        want = {
            "unique": "urn:isbn:9781234567897",
            "identifiers": ["urn:isbn:9781234567897", "urn:orm:book:9781234567897"],
            "language": ["es"],
            "date": ["2026-03-15"],
            "publisher": ["Editorial Ficticia"],
        }
        return values == want, json.dumps(values, ensure_ascii=False)

    def metadata_subjects_rights():
        subjects = [e.text for e in epub.dc("subject")]
        rights = [e.text for e in epub.dc("rights")]
        want_rights = [
            "Copyright © 2026 José Ñúñez y Zoë Østergaard. "
            "Todos los derechos reservados."
        ]
        ok = (
            subjects == ["Algoritmos", "Estructuras de datos"] and rights == want_rights
        )
        return ok, f"subjects={subjects} rights={rights}"

    def metadata_description():
        got = [e.text for e in epub.dc("description")]
        want = ["Un libro ficticio sobre árboles.\n\nCubre grafos.\n\nIncluye ñandúes."]
        return got == want, f"description={got!r}"

    def cover():
        items = [
            i
            for i in epub.manifest.values()
            if "cover-image" in (i.get("properties") or "")
        ]
        if len(items) != 1:
            return False, f"{len(items)} cover-image items"
        item = items[0]
        meta = [
            m.get("content")
            for m in epub.opf.findall("opf:metadata/opf:meta", NS)
            if m.get("name") == "cover"
        ]
        same = epub.read(item.get("href")) == fixture("content/images/cover-full.png")
        thumb_packed = any(
            epub.read(i.get("href")) == fixture("covers/thumb.png")
            for i in epub.manifest.values()
            if i.get("media-type", "").startswith("image/")
        )
        ok = (
            item.get("href") == "images/cover-full.png"
            and same
            and meta == [item.get("id")]
            and not thumb_packed
        )
        return (
            ok,
            f"href={item.get('href')} full_res={same} thumbnail_packed={thumb_packed}",
        )

    def guide():
        refs = {
            r.get("type"): r.get("href")
            for r in epub.opf.findall("opf:guide/opf:reference", NS)
        }
        want = {"cover": "text/cover.xhtml", "text": "text/titlepage.xhtml"}
        return refs == want, f"guide={refs}"

    def spine():
        docs = epub.content_docs
        return docs == SPINE, f"spine={docs}"

    def toc_nav():
        got = toc_from_nav(epub)
        return got == EXPECTED_TOC, json.dumps(got, ensure_ascii=False)

    def toc_ncx():
        got = toc_from_ncx(epub)
        return got == EXPECTED_TOC, json.dumps(got, ensure_ascii=False)

    def img(doc: str, figure_id: str) -> tuple[str, str]:
        tag = epub.soup(doc).find(id=figure_id).find("img")
        return tag["src"], epub.resolve(doc, tag["src"])

    def images_relative():
        src, target = img("text/ch01.xhtml", "fig-1-1")
        ok = (
            src == "../images/fig1-1.png"
            and epub.has(target)
            and epub.read(target) == fixture("content/images/fig1-1.png")
        )
        return ok, f"src={src}"

    def images_absolute():
        found = {}
        for fig, name in (("fig-1-2", "fig1-2.png"), ("fig-1-3", "fig1-3.png")):
            src, target = img("text/ch01.xhtml", fig)
            found[fig] = src
            if src != f"../images/{name}" or not epub.has(target):
                return False, f"srcs={found}"
            if epub.read(target) != fixture(f"content/images/{name}"):
                return False, f"{name} bytes differ"
        return True, f"srcs={found}"

    def missing_image_continues():
        warned = "fig2-1.png" in stdout and "Warning" in stdout
        ch02 = epub.soup("text/ch02.xhtml")
        intact = (
            ch02.find(id="sec-2-1") is not None and ch02.find(id="sec-2-2") is not None
        )
        return warned and intact, f"warned={warned} chapter_intact={intact}"

    def flaky_image():
        src, target = img("text/ch03.xhtml", "fig-3-1")
        present = epub.has(target) and epub.read(target) == fixture(
            "content/images/fig3-1.png"
        )
        return present, f"src={src} packaged={epub.has(target)}"

    def flaky_chapter():
        # ch01 answers 500 once; the retry must bring the whole chapter back.
        seen = statuses.get("text/ch01.html")
        ids = epub.ids("text/ch01.xhtml") if epub.has("text/ch01.xhtml") else set()
        ok = (
            seen == [500, 200]
            and "text/ch01.xhtml" in epub.content_docs
            and {"ch01", "sec-1-1", "sec-1-2-1", "fig-1-1", "fn1"} <= ids
        )
        return ok, f"statuses={seen} in_spine={'text/ch01.xhtml' in epub.content_docs}"

    def retry_policy():
        # Transient errors are retried once each; a 404 is final and not repeated.
        want = {
            "images/fig3-1.png": [500, 200],
            "text/ch01.html": [500, 200],
            "styles/book.css": [429, 200],
            "images/fig2-1.png": [404],
        }
        got = {key: statuses.get(key) for key in want}
        return got == want, json.dumps(got)

    def no_dangling():
        dangling = []
        for doc in epub.content_docs:
            soup = epub.soup(doc)
            for tag in soup.find_all(["img", "a", "link"]):
                ref = tag.get("src") or tag.get("href")
                if not ref or re.match(r"^[a-z]+:", ref):
                    continue
                target = epub.resolve(doc, ref)
                path, _, frag = target.partition("#")
                if not epub.has(path) or (frag and frag not in epub.ids(path)):
                    dangling.append(f"{doc} -> {ref}")
        return not dangling, f"dangling={dangling}"

    def cross_links():
        want = {
            ("text/ch01.xhtml", "xref-ch03"): "ch03.xhtml#sec-3-1",
            ("text/ch01.xhtml", "xref-ch02"): "ch02.xhtml#sec-2-1",
            ("text/ch01.xhtml", "xref-prod"): "ch03.xhtml",
            ("text/ch02.xhtml", "xref-ch01"): "ch01.xhtml#sec-1-2-1",
            ("text/ch03.xhtml", "xref-back"): "ch01.xhtml",
            ("text/ch01.xhtml", "fnref1"): "#fn1",
        }
        got = {}
        for (doc, anchor_id), href in want.items():
            got[anchor_id] = epub.soup(doc).find(id=anchor_id)["href"]
        backref = epub.soup("text/ch01.xhtml").find(id="fn1").find("a")["href"]
        got["fn1-back"] = backref
        targets_ok = True
        for doc, anchor_id in want:
            path, _, frag = epub.resolve(doc, got[anchor_id]).partition("#")
            targets_ok &= epub.has(path) and (not frag or frag in epub.ids(path))
        ok = (
            all(got[a] == h for (_, a), h in want.items())
            and backref == "#fnref1"
            and targets_ok
        )
        return ok, json.dumps(got, ensure_ascii=False)

    def external_links():
        soup = epub.soup("text/ch01.xhtml")
        web, mail = soup.find(id="ext-web")["href"], soup.find(id="ext-mail")["href"]
        ok = (
            web == "https://example.org/%C3%B1and%C3%BA?x=1&y=2"
            and mail == "mailto:autores@example.org"
        )
        return ok, f"web={web} mail={mail}"

    def reader_artifacts():
        problems = []
        for doc in epub.content_docs:
            soup = epub.soup(doc)
            text = epub.read(doc).decode("utf-8")
            if soup.find("script"):
                problems.append(f"{doc}: script")
            if any(
                i.has_attr("width") or i.has_attr("height")
                for i in soup.find_all("img")
            ):
                problems.append(f"{doc}: img dimensions")
            if (
                "contenteditable" in text
                or "sbo-rt-content" in text
                or "Menú del lector" in text
            ):
                problems.append(f"{doc}: reader markup")
            links = [
                link.get("href") for link in soup.find_all("link", rel="stylesheet")
            ]
            if links != ["../styles/book.css"]:
                problems.append(f"{doc}: stylesheets {links}")
        return not problems, f"problems={problems}"

    def css_localized():
        css = epub.read("styles/book.css").decode("utf-8")
        want = ["body {", "h1, h2 {", "figure img {", "table tbody tr td {"]
        ok = (
            "sbo-rt-content" not in css
            and "tdiv" not in css
            and all(w in css for w in want)
        )
        return ok, " | ".join(
            line.split("{")[0].strip() for line in css.splitlines() if "{" in line
        )

    def no_server_leak():
        leaks = sorted(
            n
            for n in epub.names
            if base_url.encode() in epub.zip.read(n) or b"127.0.0.1" in epub.zip.read(n)
        )
        return not leaks, f"entries_with_server_url={leaks}"

    for name, fn in [
        ("metadata_title_subtitle", metadata_title),
        ("metadata_authors_file_as", metadata_authors),
        ("metadata_identity_publisher_date", metadata_identity),
        ("metadata_subjects_rights", metadata_subjects_rights),
        ("metadata_description_plain_text", metadata_description),
        ("cover_full_resolution", cover),
        ("guide_cover_and_start", guide),
        ("spine_reading_order", spine),
        ("toc_nav_nested", toc_nav),
        ("toc_ncx_nested", toc_ncx),
        ("images_relative_rewritten", images_relative),
        ("images_absolute_urls_recovered", images_absolute),
        ("missing_image_404_warns_and_continues", missing_image_continues),
        ("flaky_image_500_once_recovered", flaky_image),
        ("flaky_chapter_500_once_recovered", flaky_chapter),
        ("retries_transient_errors_only", retry_policy),
        ("no_dangling_references", no_dangling),
        ("cross_chapter_links_and_footnotes", cross_links),
        ("external_links_untouched", external_links),
        ("reader_markup_removed", reader_artifacts),
        ("stylesheet_localized", css_localized),
        ("no_fake_server_url_in_epub", no_server_leak),
    ]:
        checks.guard(name, fn)


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    checks = Checks()
    epub_artifact = ARTIFACTS / f"{SCENARIO}.epub"
    hashes = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # Run 1 and 2: valid session, fresh server each time (the flaky image
        # fails once per server). Run 2 only proves the output is repeatable.
        for attempt in (1, 2):
            out = tmp / f"ok{attempt}"
            out.mkdir()
            with FakeOreilly() as server:
                proc = run_cli(
                    server.base_url, E2E / "fixtures/session-valid.json", out
                )
                base_url = server.base_url
                statuses = dict(server.statuses)
            produced = sorted(p.name for p in out.glob("*.epub"))
            if attempt == 1:
                output = proc.stdout + proc.stderr
                checks.add(
                    "cli_exit_zero", proc.returncode == 0, f"exit={proc.returncode}"
                )
                checks.add(
                    "output_named_after_title",
                    produced == [EXPECTED_FILENAME],
                    f"files={produced}",
                )
                if not produced:
                    print(output)
                    break
                epub_path = out / produced[0]
                shutil.copyfile(epub_path, epub_artifact)
                check_book(
                    checks,
                    Epub(epub_path),
                    output.replace(base_url, "{BASE}"),
                    base_url,
                    statuses,
                )
            if produced:
                hashes.append(normalized_hash(out / produced[0], base_url))
        if len(hashes) == 2:
            checks.add(
                "repeatable_output",
                hashes[0] == hashes[1],
                "normalized hashes of two runs match"
                if hashes[0] == hashes[1]
                else "normalized hashes of two runs differ",
            )

        # Run 3: expired session.
        out = tmp / "expired"
        out.mkdir()
        with FakeOreilly() as server:
            proc = run_cli(server.base_url, E2E / "fixtures/session-expired.json", out)
            text = (proc.stdout + proc.stderr).replace(server.base_url, "{BASE}")
        produced = sorted(p.name for p in out.iterdir())
        checks.add(
            "expired_cookie_fails_without_output",
            proc.returncode != 0 and not produced and "401" in text,
            f"exit={proc.returncode} files={produced} mentions_401={'401' in text}",
        )
        checks.add(
            "expired_cookie_message_says_refresh_cookies",
            "cookie" in text.lower(),
            "error: "
            + " ".join(
                line.strip()
                for line in text.splitlines()
                if "Error" in line or "error" in line
            )[:300],
        )

    counts = {
        s: sum(r["status"] == s for r in checks.results)
        for s in ("pass", "known_failure", "fail", "unexpected_pass")
    }
    ok = counts["fail"] == 0 and counts["unexpected_pass"] == 0
    epub_hash, entries = hashes[0] if hashes else (None, 0)
    report = {
        "scenario": SCENARIO,
        "result": "pass" if ok else "fail",
        "summary": counts,
        "command": f"OREILLY_DL_BASE_URL=<fake> oreilly-dl {BOOK_URL} "
        "-c tests/e2e/fixtures/session-valid.json -o <dir>",
        "epub": {
            "file": str(epub_artifact.relative_to(ROOT)) if hashes else None,
            "normalized_sha256": epub_hash,
            "entries": entries,
            "normalization": "sha256 of sorted (name, sha256(content)); zip mtimes "
            "ignored, dcterms:modified and fake server URL blanked",
        },
        "epubcheck": run_epubcheck(epub_artifact)
        if hashes
        else {"available": None, "result": "not run", "reason": "no EPUB produced"},
        "checks": checks.results,
    }
    out_json = ARTIFACTS / f"{SCENARIO}.json"
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    for r in checks.results:
        print(
            f"{r['status']:>16}  {r['name']}"
            + (f"  ({r['detail']})" if r["status"] != "pass" else "")
        )
    print(f"\n{counts} -> {report['result'].upper()}  [{out_json.relative_to(ROOT)}]")
    print(f"normalized sha256: {epub_hash}  epubcheck: {report['epubcheck']['result']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
