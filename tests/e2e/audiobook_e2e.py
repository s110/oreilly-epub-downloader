"""E2E scenario 2: the real CLI downloads a fictitious audiobook from local fakes.

Called by `run_e2e.py`. The fake serves O'Reilly's audiobook API, Kaltura and
Kaltura's CDN (see `fake_oreilly.py`). The audio is synthetic HLS made by
`fixtures/audiobook/make_media.py`, and `fixtures/audiobook/expected.json`
holds the number of AAC frames and the sha256 of the raw frames of every
chapter as FFmpeg's demuxer reads them: the checks compare the M4B and M4A
files, which this module takes apart itself, with those figures.

Runs:
1. `oreilly-dl <id>`: one M4B, playlist from the API;
2. the same with the playlist API answering 404: the client reads the player
   page instead, and the M4B must be byte-identical to run 1;
3. `--feed-url` with a /library/view/ URL: one M4A per chapter, playlist, cover and podcast feed;
4. an expired cookie: 401, nothing written.

Artifacts: artifacts/e2e/fictitious_audiobook.m4b and .json. The M4B has no
timestamps, so its plain sha256 is the repeatable fingerprint.
"""

import hashlib
import json
import shutil
import struct
import tempfile
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

from fake_oreilly import AUDIO, AUDIO_ID, FakeOreilly

SCENARIO = "fictitious_audiobook"
AUDIO_URL = f"https://learning.oreilly.com/videos/suenan-los-grafos/{AUDIO_ID}/{AUDIO_ID}-aud_ch1/"
TITLE = "¿Suenan los Grafos? Relatos para Ñandúes"
M4B_NAME = "¿Suenan los Grafos Relatos para Ñandúes.m4b"
FOLDER = "¿Suenan los Grafos Relatos para Ñandúes"
# The split run uses the /library/view/ form with a lowercase suffix: the id
# must still be read as an audiobook id, in upper case.
LIBRARY_URL = f"https://learning.oreilly.com/library/view/suenan-los-grafos/{AUDIO_ID.lower()}/"
FEED_URL = "https://podcast.example/mis audios"
FEED_BASE = "https://podcast.example/mis%20audios/"
SAMPLE_RATE = 22050
# AAC LC (2), 22050 Hz (index 7), mono: what make_media.py encodes.
AUDIO_SPECIFIC_CONFIG = "1388"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

# (title, entry, flavor the client must pick, file name in split mode)
# fmt: off
CHAPTERS = [
    ("Capítulo 1. Cómo suena un árbol", "1_capit001", "1_hiflav01",
     "01 - Capítulo 1. Cómo suena un árbol.m4a"),
    ("Capítulo 2. Ñandúes: ida/vuelta", "1_capit002", "1_hiflav02",
     "02 - Capítulo 2. Ñandúes idavuelta.m4a"),
    ("Capítulo 3. El eco del grafo", "1_capit003", "1_hiflav03",
     "03 - Capítulo 3. El eco del grafo.m4a"),
    ("Apéndice A. Créditos", "1_capit004", "1_hiflav04",
     "04 - Apéndice A. Créditos.m4a"),
]
# fmt: on

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"tref", b"ilst", b"dinf"}


class Mp4:
    """Minimal reader of the boxes the checks need."""

    def __init__(self, path: Path):
        self.data = path.read_bytes()
        self.top = [kind for kind, _, _ in self.children(0, len(self.data))]

    def children(self, start: int, end: int):
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack(">I4s", self.data[pos : pos + 8])
            header = 8
            if size == 1:
                size = struct.unpack(">Q", self.data[pos + 8 : pos + 16])[0]
                header = 16
            if size < header or pos + size > end:
                raise ValueError(f"bad box {kind!r} at {pos}")
            yield kind, pos + header, pos + size
            pos += size

    def find(self, path: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
        """Bodies of the boxes at `path` ("moov/trak/tkhd"), meta's header skipped."""
        end = len(self.data) if end is None else end
        head, _, rest = path.partition("/")
        found = []
        for kind, body, stop in self.children(start, end):
            if kind != head.encode("latin-1"):
                continue
            if kind == b"meta":
                body += 4
            found += self.find(rest, body, stop) if rest else [(body, stop)]
        return found

    def body(self, path: str, start: int = 0, end: int | None = None) -> bytes:
        (body, stop), *_ = self.find(path, start, end)
        return self.data[body:stop]

    def tracks(self) -> dict[bytes, tuple[int, int]]:
        """handler type -> trak body."""
        out = {}
        for body, stop in self.find("moov/trak"):
            hdlr = self.body("mdia/hdlr", body, stop)
            out[hdlr[8:12]] = (body, stop)
        return out

    def samples(self, trak: tuple[int, int]) -> list[bytes]:
        stbl = self.find("mdia/minf/stbl", *trak)[0]
        stsz = self.body("stsz", *stbl)
        fixed, count = struct.unpack(">II", stsz[4:12])
        sizes = [fixed] * count if fixed else list(struct.unpack(f">{count}I", stsz[12:]))
        if self.find("stco", *stbl):
            raw = self.body("stco", *stbl)
            n = struct.unpack(">I", raw[4:8])[0]
            offsets = list(struct.unpack(f">{n}I", raw[8:]))
        else:
            raw = self.body("co64", *stbl)
            n = struct.unpack(">I", raw[4:8])[0]
            offsets = list(struct.unpack(f">{n}Q", raw[8:]))
        stsc = self.body("stsc", *stbl)
        runs = [struct.unpack(">III", stsc[8 + 12 * i : 20 + 12 * i]) for i in range(struct.unpack(">I", stsc[4:8])[0])]
        out, sample = [], 0
        for chunk, offset in enumerate(offsets, 1):
            per_chunk = next(n for first, n, _ in reversed(runs) if first <= chunk)
            for _ in range(per_chunk):
                out.append(self.data[offset : offset + sizes[sample]])
                offset += sizes[sample]
                sample += 1
        if sample != count:
            raise ValueError(f"sample table covers {sample} of {count} samples")
        return out

    def stts(self, trak: tuple[int, int]) -> list[tuple[int, int]]:
        raw = self.body("mdia/minf/stbl/stts", *trak)
        n = struct.unpack(">I", raw[4:8])[0]
        return [struct.unpack(">II", raw[8 + 8 * i : 16 + 8 * i]) for i in range(n)]

    def tags(self) -> dict[str, tuple[int, bytes]]:
        out = {}
        for body, stop in self.find("moov/udta/meta/ilst"):
            for kind, item, end in self.children(body, stop):
                data = self.body("data", item, end)
                out[kind.decode("latin-1")] = (struct.unpack(">I", data[:4])[0], data[8:])
        return out

    def chpl(self) -> list[tuple[int, str]]:
        found = self.find("moov/udta/chpl")
        if not found:
            return []
        raw = self.data[found[0][0] : found[0][1]]
        count, pos, out = raw[8], 9, []
        for _ in range(count):
            start, length = struct.unpack(">QB", raw[pos : pos + 9])
            out.append((start, raw[pos + 9 : pos + 9 + length].decode("utf-8")))
            pos += 9 + length
        return out


def expected_frames() -> dict:
    return json.loads((AUDIO / "expected.json").read_text())


def sha(frames: list[bytes]) -> str:
    return hashlib.sha256(b"".join(frames)).hexdigest()


def pyav_oracle(path: Path) -> dict:
    """Decode with FFmpeg's libraries (PyAV) when installed, like epubcheck for the EPUB."""
    try:
        import av
    except ImportError:
        return {"available": False, "result": "not run", "reason": "PyAV (pip install av) not installed"}
    try:
        with av.open(str(path)) as container:
            chapters = [c["metadata"].get("title", "") for c in container.chapters()]
            audio = container.streams.audio[0]
            samples = sum(frame.samples for frame in container.decode(audio))
            tags = {k: container.metadata.get(k) for k in ("title", "artist", "composer", "media_type")}
    except Exception as exc:  # noqa: BLE001
        return {"available": True, "result": "invalid", "error": f"{type(exc).__name__}: {exc}"}
    total = sum(expected_frames()[e][f]["frames"] for _, e, f, _ in CHAPTERS) * 1024
    ok = samples == total and chapters == [c[0] for c in CHAPTERS]
    return {
        "available": True,
        "result": "valid" if ok else "invalid",
        "decoded_samples": samples,
        "chapters": chapters,
        "tags": tags,
    }


def check_m4b(checks, path: Path, stdout: str, server: FakeOreilly) -> None:
    mp4 = Mp4(path)
    want = expected_frames()
    tracks = mp4.tracks()
    audio, text = tracks.get(b"soun"), tracks.get(b"text")

    def layout():
        brand = mp4.data[8:12]
        return brand == b"M4B " and mp4.top[:3] == [b"ftyp", b"moov", b"mdat"], f"brand={brand} top={mp4.top}"

    def frames_bitexact():
        frames = mp4.samples(audio)
        got, pos = {}, 0
        for title, entry, flavor, _ in CHAPTERS:
            n = want[entry][flavor]["frames"]
            got[title] = sha(frames[pos : pos + n]) == want[entry][flavor]["sha256"]
            pos += n
        return all(got.values()) and pos == len(frames), json.dumps({"total": len(frames), "chapters_match": got}, ensure_ascii=False)

    def highest_flavor():
        requested = sorted({k.split("/")[3] for k in server.statuses if k.startswith("/cdn/1_capit002/")})
        return requested == ["1_hiflav02"], f"flavors_requested={requested}"

    def sample_description():
        stbl = mp4.find("mdia/minf/stbl", *audio)[0]
        stsd = mp4.body("stsd", *stbl)
        asc = stsd[stsd.index(b"\x05") + 2 : stsd.index(b"\x05") + 2 + stsd[stsd.index(b"\x05") + 1]].hex()
        mdhd = mp4.body("mdia/mdhd", *audio)
        timescale = struct.unpack(">I", mdhd[12:16])[0]
        stts = mp4.stts(audio)
        total = sum(want[e][f]["frames"] for _, e, f, _ in CHAPTERS)
        ok = asc == AUDIO_SPECIFIC_CONFIG and timescale == SAMPLE_RATE and stts == [(total, 1024)]
        return ok, f"asc={asc} timescale={timescale} stts={stts}"

    def chapter_track():
        tref = mp4.body("tref/chap", *audio)
        text_id = struct.unpack(">I", mp4.body("tkhd", *text)[12:16])[0]
        disabled = mp4.body("tkhd", *text)[1:4] == b"\0\0\0"
        titles = []
        for sample in mp4.samples(text):
            length = struct.unpack(">H", sample[:2])[0]
            titles.append(sample[2 : 2 + length].decode("utf-8"))
        durations = [d for n, d in mp4.stts(text) for _ in range(n)]
        want_durations = [want[e][f]["frames"] * 1024 for _, e, f, _ in CHAPTERS]
        ok = (
            struct.unpack(">I", tref)[0] == text_id
            and disabled
            and titles == [c[0] for c in CHAPTERS]
            and durations == want_durations
        )
        return ok, json.dumps({"titles": titles, "durations": durations, "disabled": disabled}, ensure_ascii=False)

    def nero_chapters():
        got = mp4.chpl()
        start, want_list = 0, []
        for title, entry, flavor, _ in CHAPTERS:
            want_list.append((start * 1024 * 10_000_000 // SAMPLE_RATE, title))
            start += want[entry][flavor]["frames"]
        return got == want_list, json.dumps(got, ensure_ascii=False)

    def tags():
        got = mp4.tags()
        text_tags = {k: v.decode("utf-8") for k, (t, v) in got.items() if t == 1}
        want_text = {
            "©nam": TITLE,
            "©alb": TITLE,
            "©ART": "Ana Ñúñez, Zoë Østergaard",
            "aART": "Ana Ñúñez, Zoë Østergaard",
            "©wrt": "Íñigo Peña",
            "©day": "2025-11-02",
            "©gen": "Audiobook",
            "desc": "Un audiolibro ficticio sobre grafos.\n\nNarrado con tonos puros.",
            "ldes": "Un audiolibro ficticio sobre grafos.\n\nNarrado con tonos puros.",
            "©too": "oreilly-dl",
        }
        cover = got.get("covr")
        ok = (
            text_tags == want_text
            and got.get("stik") == (21, b"\x02")
            and cover == (13, (AUDIO / "cover.jpg").read_bytes())
            and "trkn" not in got
        )
        return ok, json.dumps({**text_tags, "stik": got.get("stik", (0, b""))[1].hex(), "covr_type": cover[0] if cover else None}, ensure_ascii=False)

    def segment_retries():
        keys = ["/cdn/1_capit003/1_hiflav03/seg-2.ts", "/cdn/1_capit003/1_hiflav03/seg-3.ts"]
        got = {k: server.statuses.get(k) for k in keys}
        return list(got.values()) == [[503, 200], [429, 200]], json.dumps(got)

    def session_renewed():
        # The first KS is rejected (INVALID_KS): one more session and one more
        # playback-context call, then the same KS for every chapter.
        sessions = server.statuses.get("/api/v1/player/kaltura_session/")
        contexts = server.statuses.get("/kaltura/api_v3/service/multirequest")
        ok = sessions == [200, 200] and contexts == [200] * (len(CHAPTERS) + 1)
        return ok, f"sessions={sessions} playback_contexts={contexts}"

    def no_cookies_to_kaltura():
        leaked = sorted(set(server.third_party_cookies))
        return not leaked, f"third_party_requests_with_cookie={leaked}"

    def no_tokens_in_console():
        found = [t for t in ("ks-e2e", "Policy=", "Signature=", "e2e-valid-token") if t in stdout]
        return not found, f"secrets_in_output={found}"

    for name, fn in [
        ("m4b_brand_and_fast_start", layout),
        ("m4b_audio_frames_bit_exact", frames_bitexact),
        ("m4b_highest_bitrate_flavor", highest_flavor),
        ("m4b_sample_description", sample_description),
        ("m4b_quicktime_chapter_track", chapter_track),
        ("m4b_nero_chapters", nero_chapters),
        ("m4b_tags_and_cover", tags),
        ("segment_retries_503_and_429", segment_retries),
        ("kaltura_session_renewed_after_invalid_ks", session_renewed),
        ("no_oreilly_cookies_to_kaltura_or_cdn", no_cookies_to_kaltura),
        ("no_tokens_in_console_output", no_tokens_in_console),
    ]:
        checks.guard(name, fn)


def check_split(checks, folder: Path) -> None:
    want = expected_frames()
    names = [c[3] for c in CHAPTERS]

    def files():
        got = sorted(p.name for p in folder.iterdir())
        expected = sorted([*names, "cover.jpg", "feed.xml", f"{FOLDER}.m3u8"])
        return got == expected, json.dumps(got, ensure_ascii=False)

    def chapter_files():
        problems = []
        for i, (title, entry, flavor, name) in enumerate(CHAPTERS, 1):
            mp4 = Mp4(folder / name)
            tags = mp4.tags()
            frames = mp4.samples(mp4.tracks()[b"soun"])
            if sha(frames) != want[entry][flavor]["sha256"]:
                problems.append(f"{name}: audio differs")
            if tags.get("trkn") != (0, struct.pack(">HHHH", 0, i, len(CHAPTERS), 0)):
                problems.append(f"{name}: trkn {tags.get('trkn')}")
            if tags.get("©nam", (0, b""))[1].decode() != title or tags.get("©alb", (0, b""))[1].decode() != TITLE:
                problems.append(f"{name}: title/album")
            if mp4.data[8:12] != b"M4A " or b"text" in mp4.tracks() or mp4.chpl():
                problems.append(f"{name}: brand or chapter data")
        return not problems, f"problems={problems}"

    def playlist():
        lines = (folder / f"{FOLDER}.m3u8").read_text(encoding="utf-8").splitlines()
        want_lines = ["#EXTM3U"]
        for title, entry, flavor, name in CHAPTERS:
            seconds = round(want[entry][flavor]["frames"] * 1024 / SAMPLE_RATE)
            want_lines += [f"#EXTINF:{seconds},{title}", name]
        return lines == want_lines, json.dumps(lines, ensure_ascii=False)

    def feed():
        root = ET.parse(folder / "feed.xml").getroot()
        channel = root.find("channel")
        items = channel.findall("item")
        got = {
            "title": channel.findtext("title"),
            "type": channel.findtext(f"{ITUNES}type"),
            "image": channel.find(f"{ITUNES}image").get("href"),
            "items": [],
        }
        problems = []
        dates = []
        for i, (item, (title, _, _, name)) in enumerate(zip(items, CHAPTERS, strict=False), 1):
            enclosure = item.find("enclosure")
            got["items"].append(item.findtext("title"))
            if enclosure.get("url") != FEED_BASE + quote(name):
                problems.append(f"url {enclosure.get('url')}")
            if enclosure.get("length") != str((folder / name).stat().st_size):
                problems.append(f"length of {name}")
            if enclosure.get("type") != "audio/x-m4a" or item.findtext(f"{ITUNES}episode") != str(i):
                problems.append(f"type/episode of {name}")
            dates.append(parsedate_to_datetime(item.findtext("pubDate")))
        guids = [item.findtext("guid") for item in items]
        ok = (
            got["title"] == TITLE
            and got["type"] == "serial"
            and got["image"] == FEED_BASE + "cover.jpg"
            and got["items"] == [c[0] for c in CHAPTERS]
            and len(set(guids)) == len(CHAPTERS)
            and dates == sorted(dates) and len(set(dates)) == len(dates)
            and b"127.0.0.1" not in (folder / "feed.xml").read_bytes()
            and not problems
        )
        return ok, json.dumps({**got, "problems": problems}, ensure_ascii=False)

    for name, fn in [
        ("split_files", files),
        ("split_chapters_bit_exact_and_tagged", chapter_files),
        ("split_m3u8_playlist", playlist),
        ("split_podcast_feed", feed),
    ]:
        checks.guard(name, fn)


def run(checks, run_cli, e2e: Path, artifacts: Path, root: Path) -> dict:
    """Run the audiobook scenario, add its checks and return its report."""
    valid = e2e / "fixtures/session-valid.json"
    m4b_artifact = artifacts / f"{SCENARIO}.m4b"
    digests = []
    fallback = None
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for attempt, api in ((1, True), (2, False)):
            out = tmp / f"m4b{attempt}"
            out.mkdir()
            with FakeOreilly(playlist_api=api) as server:
                proc = run_cli(server.base_url, valid, out, AUDIO_URL)
                text = (proc.stdout + proc.stderr).replace(server.base_url, "{BASE}")
                produced = sorted(p.name for p in out.iterdir())
                if attempt == 1:
                    checks.add("audio_cli_exit_zero", proc.returncode == 0, f"exit={proc.returncode}")
                    checks.add("audio_output_named_after_title", produced == [M4B_NAME], f"files={produced}")
                    if produced != [M4B_NAME]:
                        print(text)
                        break
                    shutil.copyfile(out / M4B_NAME, m4b_artifact)
                    check_m4b(checks, out / M4B_NAME, text, server)
                else:
                    fallback = {
                        "exit": proc.returncode,
                        "files": produced,
                        "api_404_then_page": server.statuses.get(f"/api/v1/videoplaylists/{AUDIO_ID}/") == [404]
                        and server.statuses.get(f"/videos/-/{AUDIO_ID}/") == [200],
                    }
            if produced == [M4B_NAME]:
                digests.append(hashlib.sha256((out / M4B_NAME).read_bytes()).hexdigest())
        if fallback is not None:
            # Recorded whenever run 2 ran: a fallback run that writes nothing fails here.
            same = len(digests) == 2 and digests[0] == digests[1]
            checks.add(
                "player_page_fallback_gives_identical_m4b",
                fallback["exit"] == 0 and fallback["files"] == [M4B_NAME]
                and fallback["api_404_then_page"] and same,
                json.dumps({**fallback, "same_sha256": same}, ensure_ascii=False),
            )

        out = tmp / "split"
        out.mkdir()
        with FakeOreilly() as server:
            proc = run_cli(server.base_url, valid, out, LIBRARY_URL, ("--feed-url", FEED_URL))
        checks.add("split_cli_exit_zero", proc.returncode == 0, f"exit={proc.returncode}")
        folder = out / FOLDER
        if folder.is_dir():
            check_split(checks, folder)
            feed_sha = hashlib.sha256((folder / "feed.xml").read_bytes()).hexdigest() if (folder / "feed.xml").exists() else None
        else:
            print(proc.stdout + proc.stderr)
            feed_sha = None

        out = tmp / "expired"
        out.mkdir()
        with FakeOreilly() as server:
            proc = run_cli(server.base_url, e2e / "fixtures/session-expired.json", out, AUDIO_URL)
            requests = sum(len(s) for s in server.statuses.values())
        text = proc.stdout + proc.stderr
        leftovers = sorted(str(p.relative_to(out)) for p in out.rglob("*"))
        checks.add(
            "audio_expired_cookie_stops_without_output",
            proc.returncode != 0 and not leftovers and "401" in text and "cookie" in text.lower() and requests == 1,
            f"exit={proc.returncode} files={leftovers} requests={requests}",
        )

    return {
        "scenario": SCENARIO,
        "command": f"OREILLY_DL_BASE_URL=<fake> OREILLY_DL_KALTURA_URL=<fake>/kaltura/ oreilly-dl {AUDIO_URL} "
        "-c tests/e2e/fixtures/session-valid.json -o <dir>",
        "m4b": {
            "file": str(m4b_artifact.relative_to(root)) if digests else None,
            "sha256": digests[0] if digests else None,
        },
        "podcast_feed_sha256": feed_sha,
        "pyav": pyav_oracle(m4b_artifact) if digests else {"available": None, "result": "not run"},
    }

