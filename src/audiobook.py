"""Audiobook download: O'Reilly metadata and clips, Kaltura playback, HLS audio.

The web player does this (seen in a HAR of learning.oreilly.com):

1. `/api/v1/videoplaylists/<id>/` (or the player page, which embeds the same
   record) gives the title, contributors and `spine`, the clips in order;
2. `/api/v1/videoclips/<clip>/` gives each clip's title and Kaltura entry id;
3. `/api/v1/player/kaltura_config/` and `/api/v1/player/kaltura_session/` give
   the Kaltura partner id and a session token (KS) valid for a few hours;
4. Kaltura's `getPlaybackContext` names the HLS flavors of an entry, and
   `playManifest` returns the HLS playlist; its segments are MPEG-TS with AAC.

Kaltura and its CDN are third-party hosts: they get their own HTTP client,
which never carries the O'Reilly cookies.
"""

import base64
import json
import math
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from rich.markup import escape

from .client import SITE, OreillyClient, _collapse, _html_to_text, console, human_delay
from .hls import AdtsReader, HlsError, aac_elementary_stream, parse_master, parse_media
from .models import Asset, Audiobook, AudioChapter, AudioTrack, BookMetadata

# OREILLY_DL_KALTURA_URL points at another Kaltura server (the E2E suite's
# local fake), like OREILLY_DL_BASE_URL does for O'Reilly. Unset in normal use.
KALTURA = os.environ.get("OREILLY_DL_KALTURA_URL", "").rstrip("/") + "/"
if KALTURA == "/":
    KALTURA = "https://cdnapisec.kaltura.com/"
CLIENT_TAG = "html5:v3.17.101"
# Renew the Kaltura session when it has less than this many seconds left.
KS_MARGIN = 600
COVER_TYPES = ("image/jpeg", "image/png")


class DownloadError(Exception):
    """A part of the audiobook could not be downloaded; no file is written."""


def is_audiobook_id(value: str) -> bool:
    """O'Reilly audiobook ids are an ISBN followed by "AU" (9781633437166AU)."""
    return bool(re.fullmatch(r"\d{9,13}X?AU", value, re.I))


def _snake(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()


def _normalize(record: dict) -> dict:
    """The API answers in snake_case, the player page in camelCase."""
    return {_snake(k): v for k, v in record.items()}


def _clip_id(url_or_ref: str) -> str:
    """"https://.../api/v1/videoclips/9781633437166AU-bll_ch1/" -> the reference id."""
    return PurePosixPath(urlsplit(url_or_ref).path).name or url_or_ref


def _expiry(value: Any) -> float:
    """Epoch seconds of the KS expiry: an ISO date (naive = UTC) or an epoch.

    Anything else counts as one hour from now, so an unexpected format only
    means the session is renewed earlier than needed.
    """
    seconds: float | None = None
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                seconds = (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()
    if seconds is None or not math.isfinite(seconds):
        return time.time() + 3600
    return seconds / 1000 if seconds > 1e11 else seconds  # milliseconds


def _short(url: str) -> str:
    """File name of a URL, for messages: never the query or a token in the path."""
    return PurePosixPath(urlsplit(url).path).name or urlsplit(url).netloc


class AudiobookFetcher:
    """Downloads an audiobook's metadata and audio through an OreillyClient."""

    def __init__(self, client: OreillyClient):
        self.client = client
        self.media = httpx.Client(
            headers={
                "User-Agent": client.http.headers["User-Agent"],
                "Accept": "*/*",
                "Origin": SITE.rstrip("/"),
                "Referer": SITE,
            },
            follow_redirects=True,
            timeout=30.0,
        )
        self._partner = ""
        self._ui_conf = ""
        self._ks = ""
        self._ks_expires = 0.0

    def close(self) -> None:
        self.media.close()

    def __enter__(self) -> "AudiobookFetcher":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # --------------------------------------------------------------- the book

    def get_audiobook(self, work_id: str) -> Audiobook:
        console.print(f"[bold]Fetching audiobook:[/] {escape(work_id)}")
        playlist = self._get_playlist(work_id)
        content_format = playlist.get("content_format") or "audiobook"
        if content_format != "audiobook":
            raise ValueError(
                f"{work_id} is a {content_format}, not an audiobook: only audiobooks "
                "can be downloaded as audio"
            )
        metadata = self._metadata(work_id, playlist)
        console.print(f"[green]Found:[/] {escape(str(metadata))}")

        refs = [_clip_id(r) for r in playlist.get("spine") or playlist.get("video_clips") or []]
        if not refs:
            raise ValueError(f"No chapters found for {work_id}.")
        console.print(f"[green]Found[/] {len(refs)} chapters")

        track = AudioTrack(file=tempfile.TemporaryFile())
        chapters: list[AudioChapter] = []
        try:
            with self.client._progress() as progress:
                task = progress.add_task("Audio", total=len(refs))
                for i, ref in enumerate(refs):
                    human_delay(1000, 2500) if i < 3 else human_delay(500, 1500)
                    clip = self._json(f"{SITE}api/v1/videoclips/{ref}/", f"chapter {ref}")
                    chapter = AudioChapter(
                        title=_collapse(clip.get("title") or "") or f"Chapter {i + 1}",
                        reference_id=ref,
                        ourn=clip.get("ourn") or "",
                        first_sample=track.samples,
                    )
                    progress.update(task, description=f"Audio: {escape(chapter.title[:40])}")
                    entry = clip.get("kaltura_entry_id")
                    if not entry:
                        raise DownloadError(f"{chapter.title}: the clip has no Kaltura entry")
                    self._download_entry(entry, work_id, ref, chapter.title, track)
                    chapter.samples = track.samples - chapter.first_sample
                    chapters.append(chapter)
                    progress.advance(task)
        except BaseException:
            track.file.close()
            raise

        return Audiobook(
            metadata=metadata,
            chapters=chapters,
            track=track,
            cover=self._get_cover(metadata),
        )

    def _json(self, url: str, what: str) -> Any:
        response = self.client._get(url)
        if response.status_code != 200:
            raise DownloadError(f"{what}: HTTP {response.status_code}")
        return response.json()

    def _get_playlist(self, work_id: str) -> dict:
        """The audiobook record; the player page when the API does not answer."""
        response = self.client._get(f"{SITE}api/v1/videoplaylists/{work_id}/")
        if response.status_code == 200:
            try:
                record = _normalize(response.json())
            except ValueError:
                record = {}
            if record.get("spine") or record.get("video_clips"):
                return record
        return self._playlist_from_page(work_id)

    def _playlist_from_page(self, work_id: str) -> dict:
        response = self.client._get(f"{SITE}videos/-/{work_id}/")
        if response.status_code == 404:
            raise ValueError(f"Audiobook not found: {work_id}")
        if response.status_code != 200:
            raise DownloadError(f"audiobook page: HTTP {response.status_code}")
        match = re.search(r"htmlContext\s*=\s*", response.text)
        if match:
            try:
                context, _ = json.JSONDecoder().raw_decode(response.text, match.end())
            except ValueError:
                context = {}
            for query in (context.get("reactQueryState") or {}).get("queries", []):
                key = query.get("queryKey") or []
                data = (query.get("state") or {}).get("data")
                if key[:1] == ["video-playlist"] and isinstance(data, dict):
                    return _normalize(data)
        raise ValueError(f"Could not read the details of {work_id} from its page")

    @staticmethod
    def _metadata(work_id: str, playlist: dict) -> BookMetadata:
        contributors = playlist.get("contributors") or {}
        publisher = playlist.get("publisher") or ""
        if isinstance(publisher, dict):
            publisher = publisher.get("name") or ""
        descriptions = playlist.get("descriptions") or {}
        if isinstance(descriptions, dict):
            description = next((v for v in descriptions.values() if isinstance(v, str) and v), "")
        else:
            description = str(descriptions)
        return BookMetadata(
            id=work_id,
            title=_collapse(playlist.get("title") or f"Audiobook {work_id}"),
            authors=[a for a in contributors.get("authors") or [] if a],
            narrators=[n for n in contributors.get("narrators") or [] if n],
            publisher=publisher,
            description=_html_to_text(description),
            isbn=playlist.get("isbn") or "",
            language=playlist.get("language") or "en",
            published=(playlist.get("publication_date") or "")[:10],
            subjects=[t["name"] for t in playlist.get("topics") or [] if isinstance(t, dict) and t.get("name")],
            cover_url=urljoin(SITE, playlist.get("cover") or f"/covers/{work_id}/"),
        )

    def _get_cover(self, metadata: BookMetadata) -> Asset | None:
        try:
            response = self.client._get(metadata.cover_url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            console.print(f"[yellow]Warning: failed to fetch cover: {escape(str(e))}[/]")
            return None
        media_type = response.headers.get("content-type", "").split(";")[0].strip()
        if media_type not in COVER_TYPES:
            kind = escape(media_type) if media_type else "not an image"
            console.print(f"[yellow]Warning: cover is {kind}, not added[/]")
            return None
        ext = "png" if media_type == "image/png" else "jpg"
        return Asset(path=f"cover.{ext}", media_type=media_type, data=response.content)

    # ---------------------------------------------------------------- Kaltura

    def _session(self, renew: bool = False) -> str:
        """Kaltura session token (KS), renewed shortly before it expires."""
        if not self._partner:
            config = self._json(f"{SITE}api/v1/player/kaltura_config/", "Kaltura config")
            self._partner = str(config.get("partner_id") or "")
            self._ui_conf = str(config.get("web_player_playkit_id") or "")
            if not self._partner:
                raise DownloadError("Kaltura config: no partner id")
        if renew or not self._ks or time.time() > self._ks_expires - KS_MARGIN:
            data = self._json(f"{SITE}api/v1/player/kaltura_session/", "Kaltura session")
            self._ks = data.get("session") or ""
            if not self._ks:
                raise DownloadError("Kaltura session: no token")
            self._ks_expires = _expiry(data.get("expiry"))
        return self._ks

    def _flavors(self, entry: str) -> str:
        """Flavor ids of the entry's HLS source, from `getPlaybackContext`."""
        for attempt in (1, 2):
            ks = self._session(renew=attempt == 2)
            body = {
                "1": {
                    "service": "baseEntry",
                    "action": "getPlaybackContext",
                    "entryId": entry,
                    "ks": ks,
                    "contextDataParams": {
                        "objectType": "KalturaContextDataParams",
                        "flavorTags": "all",
                    },
                },
                "apiVersion": "3.3.0",
                "format": 1,
                "ks": ks,
                "clientTag": CLIENT_TAG,
                "partnerId": self._partner,
            }
            response = self.client._request(
                "POST", f"{KALTURA}api_v3/service/multirequest", json=body, http=self.media
            )
            if response.status_code != 200:
                raise DownloadError(f"Kaltura playback context: HTTP {response.status_code}")
            data = response.json()
            context = data[0] if isinstance(data, list) and data else data
            if not isinstance(context, dict):
                raise DownloadError("Kaltura playback context: unexpected answer")
            if context.get("objectType") == "KalturaAPIException":
                code = str(context.get("code") or "")
                if "KS" in code and attempt == 1:
                    continue
                raise DownloadError(f"Kaltura: {code} {context.get('message') or ''}".strip())
            sources = [s for s in context.get("sources") or [] if s.get("format") == "applehttp"]
            if any(s.get("drm") for s in sources) and not any(not s.get("drm") for s in sources):
                raise HlsError("the audio is protected with DRM")
            for source in sources:
                if not source.get("drm") and source.get("flavorIds"):
                    return source["flavorIds"]
            raise HlsError("Kaltura offers no HLS source for this chapter")
        raise AssertionError("unreachable")

    def _download_entry(
        self, entry: str, work_id: str, ref: str, title: str, track: AudioTrack
    ) -> None:
        """Append the AAC frames of one Kaltura entry to the book's track."""
        flavors = self._flavors(entry)
        ks = self._session()
        page = f"{SITE}videos/-/{work_id}/{ref}/"
        params = {
            "clientTag": CLIENT_TAG,
            "referrer": base64.b64encode(page.encode()).decode(),
        }
        if self._ui_conf:
            params["uiConfId"] = self._ui_conf
        manifest_url = (
            f"{KALTURA}p/{self._partner}/sp/{self._partner}00/playManifest/entryId/{entry}"
            f"/protocol/https/format/applehttp/flavorIds/{flavors}/ks/{ks}/a.m3u8"
        )
        manifest = self._media(manifest_url, f"{title}: playlist", params=params)
        base = str(manifest.url)
        variants = parse_master(manifest.text, base)
        if variants:
            best = max(variants, key=lambda v: v.bandwidth)
            media = self._media(best.url, f"{title}: playlist")
            text, base = media.text, str(media.url)
        else:
            text = manifest.text
        segments = parse_media(text, base)

        reader = AdtsReader()
        for segment in segments:
            data = self._media(segment.url, f"{title}: segment {_short(segment.url)}").content
            for frame in reader.feed(aac_elementary_stream(data)):
                track.file.write(frame)
                track.sizes.append(len(frame))
        left = reader.finish()
        if left:
            console.print(
                f"[yellow]Warning: {escape(title)}: dropped {left} bytes of an incomplete "
                "audio frame at the end[/]"
            )
        config = reader.config
        if config is None:
            raise DownloadError(f"{title}: the chapter has no audio")
        asc = config.audio_specific_config
        if not track.audio_specific_config:
            track.audio_specific_config = asc
            track.sample_rate = config.sample_rate
            track.channels = config.channels
        elif asc != track.audio_specific_config:
            raise HlsError(f"{title} is encoded differently ({config}) from the chapters before it")

    def _media(self, url: str, what: str, params: dict | None = None) -> httpx.Response:
        """GET from Kaltura or its CDN; errors never show the URL (it holds tokens)."""
        try:
            response = self.client._request("GET", url, params=params, http=self.media)
        except httpx.HTTPError as e:
            raise DownloadError(f"{what}: {type(e).__name__}") from None
        if response.status_code != 200:
            raise DownloadError(f"{what}: HTTP {response.status_code}")
        return response
