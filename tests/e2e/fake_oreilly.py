"""Local fake of the O'Reilly Learning API, serving a pinned fictitious book.

Only the endpoints the CLI uses are implemented. Behaviour the E2E scenario
relies on:

- every request must carry the cookie `orm-jwt=e2e-valid-token`; any other
  session gets 401, as an expired O'Reilly session does;
- the chapter listing is paginated (two pages, linked by `next`);
- `images/fig2-1.png` is listed in the file listing but answers 404;
- `images/fig3-1.png` and the chapter `text/ch01.html` answer 500 on their
  first request and 200 afterwards;
- `images/fig3-2.png` is listed but always answers 503;
- `styles/book.css` answers 429 with `Retry-After: 1` on its first request
  and 200 afterwards;
- `images/fig1-2.png` and `images/fig1-3.png` are not in the file listing and
  are only reachable through the per-file endpoint.

`{BASE}` in the fixtures is replaced by the server's own base URL.
"""

import json
import threading
from collections import Counter
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

BOOK_ID = "9781234567897"
URN = f"urn:orm:book:{BOOK_ID}"
VALID_TOKEN = "e2e-valid-token"
FIXTURES = Path(__file__).parent / "fixtures" / "book"
FLAKY_ONCE = {"images/fig3-1.png", "text/ch01.html"}
RATE_LIMITED_ONCE = {"styles/book.css"}
ALWAYS_UNAVAILABLE = {"images/fig3-2.png"}
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".png": "image/png",
}


class FakeOreilly:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.statuses: dict[str, list[int]] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOreilly":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _render(self, data: bytes) -> bytes:
        return data.replace(b"{BASE}", self.base_url.encode())

    def _route(self, path: str, query: dict) -> tuple[int, str, bytes]:
        api = f"/api/v2/epubs/{URN}/"
        if path == api:
            return (
                200,
                "application/json",
                self._render((FIXTURES / "api/epub.json").read_bytes()),
            )
        if path == "/api/v2/search/" and query.get("query") == [BOOK_ID]:
            return (
                200,
                "application/json",
                self._render((FIXTURES / "api/search.json").read_bytes()),
            )
        if path == "/api/v2/epub-chapters/":
            if query.get("page") == ["2"]:
                name = "chapters-page2.json"
            elif query.get("epub_identifier") == [URN]:
                name = "chapters-page1.json"
            else:
                return (
                    400,
                    "application/json",
                    b'{"detail": "epub_identifier required"}',
                )
            return (
                200,
                "application/json",
                self._render((FIXTURES / "api" / name).read_bytes()),
            )
        if path == f"{api}table-of-contents/":
            return (
                200,
                "application/json",
                self._render((FIXTURES / "api/toc.json").read_bytes()),
            )
        if path == f"{api}files/":
            return (
                200,
                "application/json",
                self._render((FIXTURES / "api/files.json").read_bytes()),
            )
        if path.startswith(f"{api}files/"):
            rel = path[len(f"{api}files/") :]
            if rel in FLAKY_ONCE and self.hits[rel] == 1:
                return 500, "text/plain", b"upstream hiccup"
            if rel in ALWAYS_UNAVAILABLE:
                return 503, "text/plain", b"service unavailable"
            if rel in RATE_LIMITED_ONCE and self.hits[rel] == 1:
                return 429, "text/plain", b"slow down"
            return self._file(FIXTURES / "content", rel)
        if path.startswith("/covers/"):
            return self._file(FIXTURES / "covers", path[len("/covers/") :])
        return 404, "text/plain", b"not found"

    @staticmethod
    def _file(root: Path, rel: str) -> tuple[int, str, bytes]:
        target = (root / rel).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            return 404, "text/plain", b"not found"
        return (
            200,
            MEDIA_TYPES.get(target.suffix, "application/octet-stream"),
            target.read_bytes(),
        )

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                path = unquote(parts.path)
                key = path.split("/files/", 1)[1] if "/files/" in path else path
                fake.hits[key] += 1
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                token = cookie["orm-jwt"].value if "orm-jwt" in cookie else ""
                if token != VALID_TOKEN:
                    status, ctype, body = (
                        401,
                        "application/json",
                        json.dumps(
                            {"detail": "Authentication credentials were not provided."}
                        ).encode(),
                    )
                else:
                    status, ctype, body = fake._route(path, parse_qs(parts.query))
                    if ctype.startswith("text/html") or ctype == "text/css":
                        body = fake._render(body)
                fake.statuses.setdefault(key, []).append(status)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if status == 429:
                    self.send_header("Retry-After", "1")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        return Handler
