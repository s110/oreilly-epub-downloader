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

The same server fakes the audiobook flow (`fixtures/audiobook/`), with
O'Reilly's API, Kaltura (`/kaltura/`, OREILLY_DL_KALTURA_URL) and Kaltura's
CDN (`/cdn/`) on one port:

- `/api/v1/videoplaylists/<id>/` answers 404 when `playlist_api` is False, so
  the client has to read the player page (`/videos/-/<id>/`) instead;
- every call to `/api/v1/player/kaltura_session/` issues a new token,
  `ks-e2e-1`, `ks-e2e-2`...; Kaltura rejects `ks-e2e-1` with INVALID_KS;
- the chapter with entry `1_capit002` has two flavors, the first listed being
  the lower bitrate;
- segment 2 of `1_capit003` answers 503 once and segment 3 answers 429 with
  `Retry-After: 1` once;
- Kaltura and the CDN do not need the O'Reilly cookie; requests to them that
  carry it are recorded in `third_party_cookies`;
- CDN URLs must carry the `Policy` signature, playManifest URLs a valid KS.

`{BASE}` in the fixtures is replaced by the server's own base URL.
"""

import json
import threading
from collections import Counter
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

AUDIO_ID = "9781234567011AU"
AUDIO = Path(__file__).parent / "fixtures" / "audiobook"
PARTNER = "1234567"
BAD_KS = "ks-e2e-1"
AUDIO_FLAKY_ONCE = {"/cdn/1_capit003/1_hiflav03/seg-2.ts": 503}
AUDIO_RATE_LIMITED_ONCE = {"/cdn/1_capit003/1_hiflav03/seg-3.ts"}

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


def _json(data) -> tuple[int, str, bytes]:
    return 200, "application/json", json.dumps(data).encode()


class FakeOreilly:
    def __init__(self, playlist_api: bool = True) -> None:
        self.playlist_api = playlist_api
        self.sessions = 0
        self.third_party_cookies: list[str] = []
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
        if path.startswith("/covers/") and AUDIO_ID not in path:
            return self._file(FIXTURES / "covers", path[len("/covers/") :])
        return self._audio_route(path, query)

    # ------------------------------------------------------------- audiobook

    def _audio_route(self, path: str, query: dict) -> tuple[int, str, bytes]:
        if path == f"/api/v1/videoplaylists/{AUDIO_ID}/":
            if not self.playlist_api:
                return 404, "application/json", b'{"detail": "Not found."}'
            return 200, "application/json", self._render((AUDIO / "api/playlist.json").read_bytes())
        if path == f"/videos/-/{AUDIO_ID}/":
            return 200, "text/html; charset=utf-8", (AUDIO / "page.html").read_bytes()
        if path.startswith("/api/v1/videoclips/"):
            ref = path.split("/")[4]
            return self._file(AUDIO / "api/clips", f"{ref}.json", "application/json")
        if path == "/api/v1/player/kaltura_config/":
            return 200, "application/json", (AUDIO / "api/kaltura_config.json").read_bytes()
        if path == "/api/v1/player/kaltura_session/":
            self.sessions += 1
            return _json(
                {
                    "session": f"ks-e2e-{self.sessions}",
                    "expiry": "2099-01-01T00:00:00.000000",
                    "privileges": "sview:*",
                }
            )
        if path == f"/covers/{AUDIO_ID}/":
            return self._file(AUDIO, "cover.jpg", "image/jpeg")
        return 404, "text/plain", b"not found"

    def _valid_ks(self, ks: str) -> bool:
        number = ks.removeprefix("ks-e2e-")
        return ks != BAD_KS and number.isdigit() and 0 < int(number) <= self.sessions

    def _flavors(self) -> dict[str, list[str]]:
        return {
            entry.name: sorted(f.name for f in entry.iterdir())
            for entry in (AUDIO / "media").iterdir()
        }

    def _kaltura(self, path: str, body: bytes) -> tuple[int, str, bytes]:
        if path == "/kaltura/api_v3/service/multirequest":
            request = json.loads(body or b"{}")
            call = request.get("1", {})
            if request.get("partnerId") != PARTNER or call.get("action") != "getPlaybackContext":
                return _json([{"objectType": "KalturaAPIException", "code": "INVALID_REQUEST"}])
            if not self._valid_ks(call.get("ks", "")):
                return _json(
                    [{"objectType": "KalturaAPIException", "code": "INVALID_KS",
                      "message": "Invalid KS"}]
                )
            entry = call.get("entryId")
            flavors = self._flavors().get(entry)
            if not flavors:
                return _json([{"objectType": "KalturaAPIException", "code": "ENTRY_ID_NOT_FOUND"}])
            ids = ",".join(flavors)
            return _json(
                [
                    {
                        "objectType": "KalturaPlaybackContext",
                        "sources": [
                            {"format": "url", "flavorIds": ids, "drm": [],
                             "url": f"{self.base_url}kaltura/{entry}/a.mp4"},
                            {"format": "applehttp", "flavorIds": ids, "drm": [],
                             "url": f"{self.base_url}kaltura/{entry}/a.m3u8"},
                        ],
                        "messages": [],
                    }
                ]
            )
        parts = path.split("/")
        # /kaltura/p/<pid>/sp/<pid>00/playManifest/entryId/<e>/protocol/https/format/
        # applehttp/flavorIds/<ids>/ks/<ks>/a.m3u8
        if len(parts) == 18 and parts[6] == "playManifest" and parts[17] == "a.m3u8":
            pid, sp, entry, ids, ks = parts[3], parts[5], parts[8], parts[14], parts[16]
            if pid != PARTNER or sp != f"{PARTNER}00" or not self._valid_ks(ks):
                return 403, "text/plain", b"forbidden"
            if ids.split(",") != self._flavors().get(entry):
                return 404, "text/plain", b"unknown flavor"
            variants = []
            for flavor in sorted(ids.split(","), key=lambda f: "hi" in f):
                bandwidth = 60000 if "hi" in flavor else 20000
                variants.append(
                    f'#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH={bandwidth},CODECS="mp4a.40.2"\n'
                    f"{self.base_url}cdn/{entry}/{flavor}/index.m3u8?Policy=e2e-signed&Signature=e2e"
                )
            master = "#EXTM3U\n" + "\n".join(variants) + "\n"
            return 200, "application/x-mpegurl", master.encode()
        return 404, "text/plain", b"not found"

    def _cdn(self, path: str, query: dict) -> tuple[int, str, bytes]:
        if query.get("Policy") != ["e2e-signed"]:
            return 403, "text/plain", b"missing signature"
        if AUDIO_FLAKY_ONCE.get(path) and self.hits[path] == 1:
            return AUDIO_FLAKY_ONCE[path], "text/plain", b"try again"
        if path in AUDIO_RATE_LIMITED_ONCE and self.hits[path] == 1:
            return 429, "text/plain", b"slow down"
        rel = path[len("/cdn/") :]
        if rel.endswith(".m3u8"):
            status, _, body = self._file(AUDIO / "media", rel)
            return status, "application/vnd.apple.mpegurl", self._render(body)
        return self._file(AUDIO / "media", rel, "video/mp2t")

    @staticmethod
    def _file(root: Path, rel: str, media_type: str = "") -> tuple[int, str, bytes]:
        target = (root / rel).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            return 404, "text/plain", b"not found"
        return (
            200,
            media_type or MEDIA_TYPES.get(target.suffix, "application/octet-stream"),
            target.read_bytes(),
        )

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self._serve(b"")

            def do_POST(self) -> None:  # noqa: N802
                self._serve(self.rfile.read(int(self.headers.get("Content-Length", 0))))

            def _serve(self, body_in: bytes) -> None:
                parts = urlsplit(self.path)
                path = unquote(parts.path)
                key = path.split("/files/", 1)[1] if "/files/" in path else path
                if path.startswith("/kaltura/p/"):
                    key = "/kaltura/playManifest"
                fake.hits[key] += 1
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                token = cookie["orm-jwt"].value if "orm-jwt" in cookie else ""
                third_party = path.startswith(("/kaltura/", "/cdn/"))
                if third_party:
                    if self.headers.get("Cookie"):
                        fake.third_party_cookies.append(key)
                    if path.startswith("/kaltura/"):
                        status, ctype, body = fake._kaltura(path, body_in)
                    else:
                        status, ctype, body = fake._cdn(path, parse_qs(parts.query))
                elif token != VALID_TOKEN:
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
