from __future__ import annotations

import json
import re
from collections.abc import Generator
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import click
from requests import Request
from unshackle.core.manifests import DASH
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Series, Title_T, Titles_T
from unshackle.core.tracks import Chapters, Tracks


class VM(Service):
    """
    Service code for Virgin Media Television (https://play.virginmediatelevision.ie).

    \b
    Version: 1.0.0
    Author: billybanana
    Authorization: None
    Geofence: IE
    Robustness:
      Widevine:
        L3: 1080p, AAC2.0

    \b
    Tips:
        - Use complete series, VOD, or replay URLs:
          https://play.virginmediatelevision.ie/shows/<uuid>/<slug>
          https://play.virginmediatelevision.ie/watch/vod/52528176/blood-ep1          
        - Raw numeric video IDs are also accepted. eg: 630bd3d6-80e8-11ef-b3a0-020f80c0527e
    """

    GEOFENCE = ("ie",)
    ALIASES = ("virginmedia", "virgin-media", "virgin",)

    @staticmethod
    @click.command(name="VM", short_help="https://play.virginmediatelevision.ie", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs: Any) -> VM:
        return VM(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        self.title = title
        self.license_url: str | None = None
        super().__init__(ctx)

        self.session.headers.update(self.config["headers"])
        self.session.headers["Userid"] = str(self.config["api"].get("userid") or "123456")

    def search(self) -> Generator[SearchResult, None, None]:
        api = self.config["api"]
        data = self._request(
            "GET",
            self.config["endpoints"]["search"],
            params={
                "key": api["key"],
                "cc": api.get("country", "IE"),
                "lang": "en",
                "platform": api.get("platform", "chrome"),
                "q": self.title.strip(),
            },
        )

        seen = set()
        for section in (data.get("response") or {}).get("sections") or []:
            section_title = section.get("title") or section.get("id") or "Result"
            for item in section.get("tiles") or []:
                result = self._search_result(item, section_title)
                if not result:
                    continue
                seen_key = str(result.id)
                if seen_key in seen:
                    continue
                seen.add(seen_key)
                yield result

    def get_titles(self) -> Titles_T:
        parsed = self._parse_title(self.title)

        if parsed["kind"] == "series":
            episodes = self._series(parsed["series_id"], self.title)
            if not episodes:
                raise ValueError(f"Could not find episodes for series: {self.title}")
            return Series(episodes)

        episode = self._episode(parsed["video_id"], self.title)
        return Series([episode])

    def get_tracks(self, title: Title_T) -> Tracks:
        playback = self._playback(title.id, title.data.get("url") or self.title)
        response = playback.get("response") or {}
        widevine = (response.get("drm") or {}).get("widevine") or {}

        manifest_url = widevine.get("stream")
        self.license_url = widevine.get("licenseAcquisitionUrl")
        if not manifest_url:
            raise ValueError(f"Could not find a Widevine DASH manifest for {title.id}")

        title.data["playback"] = playback
        title.data["license_url"] = self.license_url

        tracks = DASH.from_url(manifest_url, self.session).to_tracks(language=title.language)
        self._mark_descriptive_audio(tracks)
        return tracks

    def get_widevine_service_certificate(self, **_: Any) -> str | None:
        return None

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: Any) -> bytes:
        license_url = title.data.get("license_url") or self.license_url
        if not license_url:
            raise ValueError("Could not find Virgin Media Widevine license URL")

        r = self.session.post(
            url=license_url,
            data=challenge,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": "https://www.virginmediatelevision.ie",
                "Referer": "https://www.virginmediatelevision.ie/",
                "User-Agent": self.config["headers"]["User-Agent"],
            },
        )
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.content

    # Service specific

    def _series(self, series_id: str, series_url: str) -> list[Episode]:
        metadata = self._series_metadata(series_id)
        show_title = (
            ((metadata.get("response") or {}).get("series") or {}).get("title")
            or self._slug_title(series_url)
        )
        if not series_url.startswith("http"):
            series_url = f"https://play.virginmediatelevision.ie/shows/{series_id}/{self._slugify(show_title)}"

        episode_refs = self._episode_refs(metadata, series_url)
        if not episode_refs:
            episode_refs = self._episode_refs_from_html(series_url)

        episodes = []
        seen = set()
        for ref in episode_refs:
            video_id = str(ref.get("id") or "")
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)

            try:
                episodes.append(self._episode(video_id, ref.get("url") or series_url, ref=ref, show_title=show_title))
            except Exception as exc:
                self.log.warning("Skipping VM episode %s: %s", video_id, exc)

        return sorted(
            episodes,
            key=lambda item: (int(item.season or 0), int(item.number or 0), item.id),
            reverse=True,
        )

    def _episode(
        self,
        video_id: str,
        url: str,
        *,
        ref: dict[str, Any] | None = None,
        show_title: str | None = None,
    ) -> Episode:
        playback = self._playback(video_id, url)
        metadata = self._stream_metadata(playback)
        ref = ref or {}

        title = show_title or self._show_title(metadata, url)
        season = self._to_int(metadata.get("series_season") or ref.get("season") or ref.get("season_number"), 0)
        number = self._to_int(metadata.get("series_episode") or ref.get("episode") or ref.get("episode_number"), 0)
        name = metadata.get("title") or ref.get("title") or (f"Episode {number}" if number else "Episode")

        return Episode(
            id_=str(video_id),
            service=self.__class__,
            title=title,
            season=season,
            number=number,
            name=name,
            year=self._year(metadata.get("created_at")),
            language="en",
            data={
                "url": self._video_url(video_id, url),
                "metadata": metadata,
                "playback": playback,
                "ref": ref,
            },
        )

    def _playback(self, video_id: str, url: str) -> dict:
        token_data = self._request(
            "GET",
            self.config["endpoints"]["token"].format(id=video_id),
        )
        token = token_data.get("token")
        expiry = token_data.get("expiry")
        if not token or not expiry:
            raise ValueError(f"Could not get Virgin Media token for {video_id}")

        api = self.config["api"]
        return self._request(
            "POST",
            self.config["endpoints"]["stream"].format(company_id=api["company_id"], id=video_id),
            headers={
                "Token": token,
                "Token-Expiry": str(expiry),
                "Userid": str(api.get("userid") or "123456"),
                "Uvid": str(video_id),
            },
            params={
                "key": api["key"],
                "cc": api.get("country", "IE"),
                "platform": api.get("platform", "chrome"),
                "url": url,
                "gdpr": api.get("gdpr", "1"),
                "gdpr_consent": api.get("gdpr_consent", "undefined"),
            },
        )

    def _series_metadata(self, series_id: str) -> dict:
        api = self.config["api"]
        params = {
            "key": api["key"],
            "cc": api.get("country", "IE"),
            "lang": "en",
            "platform": api.get("platform", "chrome"),
        }

        last_error = None
        for endpoint in self.config["endpoints"]["series_metadata"]:
            try:
                return self._request("GET", endpoint.format(id=series_id), params=params)
            except Exception as exc:
                last_error = exc

        raise ValueError(f"Could not fetch Virgin Media series metadata: {last_error}")

    def _episode_refs(self, metadata: dict, series_url: str) -> list[dict[str, Any]]:
        series = ((metadata.get("response") or {}).get("series") or {})
        refs = []
        for season in series.get("seasons") or []:
            season_number = self._to_int(
                season.get("number") or season.get("season_number") or season.get("title"),
                0,
            )
            for episode in season.get("episodes") or season.get("tiles") or []:
                video_id = (
                    episode.get("uvid")
                    or episode.get("id")
                    or episode.get("video_id")
                    or episode.get("content_id")
                    or episode.get("asset_id")
                )
                if not video_id:
                    continue

                refs.append({
                    "id": str(video_id),
                    "season_number": season_number,
                    "episode_number": self._to_int(
                        episode.get("episode") or episode.get("series_episode") or episode.get("episode_number"),
                        0,
                    ),
                    "title": episode.get("title") or episode.get("name"),
                    "url": episode.get("url") or series_url,
                })

        return refs

    def _episode_refs_from_html(self, series_url: str) -> list[dict[str, Any]]:
        html = self._request(
            "GET",
            series_url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            raw=True,
        )

        ids = set()
        for pattern in (
            r"/replay/(\d{6,})",
            r"/vod/(\d{6,})",
            r'"videoId"\s*:\s*(\d{6,})',
        ):
            ids.update(re.findall(pattern, html))

        refs = []
        for video_id in ids:
            refs.append({"id": video_id, "season_number": 0, "episode_number": 0, "url": series_url})
        return refs

    def _search_result(self, item: dict[str, Any], section_title: str) -> SearchResult | None:
        item_type = (item.get("type") or item.get("label") or section_title or "").lower()
        title = self._clean_title(item.get("title") or item.get("name") or "Virgin Media")
        description = item.get("synopsis") or item.get("description")

        if "series" in item_type or item.get("series_id"):
            series_id = item.get("series_id") or item.get("id")
            if not series_id:
                return None

            seasons = item.get("seasons")
            episodes = item.get("episodes")
            label = "SERIES"
            if seasons or episodes:
                season_word = "season" if str(seasons) == "1" else "seasons"
                episode_word = "episode" if str(episodes) == "1" else "episodes"
                label += f" ({seasons or 0} {season_word}, {episodes or 0} {episode_word})"

            return SearchResult(
                id_=series_id,
                title=title,
                description=description,
                label=label,
                url=f"https://play.virginmediatelevision.ie/shows/{series_id}/{self._slugify(title)}",
            )

        video_id = (
            item.get("uvid")
            or item.get("video_id")
            or item.get("content_id")
            or item.get("asset_id")
            or item.get("id")
        )
        if not video_id or not re.fullmatch(r"\d+", str(video_id)):
            return None

        label = item.get("label") or section_title or "VIDEO"
        return SearchResult(
            id_=str(video_id),
            title=title,
            description=description,
            label=str(label).upper(),
            url=f"https://play.virginmediatelevision.ie/replay/{video_id}/{self._slugify(title)}",
        )

    def _request(self, method: str, endpoint: str, *, raw: bool = False, **kwargs: Any) -> Any:
        url = endpoint if endpoint.startswith("http") else urljoin(self.config["endpoints"]["base_url"], endpoint)
        headers = kwargs.pop("headers", None)

        request = Request(
            method,
            url,
            headers={**self.config["headers"], **(headers or {})},
            **kwargs,
        )
        prep = self.session.prepare_request(request)
        response = self.session.send(prep, timeout=30)

        if response.status_code != 200:
            raise ConnectionError(
                f"Status: {response.status_code} - {response.url}\n"
                "Content may be geo-restricted to IE"
            )

        if raw:
            return response.text

        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            return response.text

    @staticmethod
    def _parse_title(title: str) -> dict[str, str]:
        value = title.strip()
        path = urlparse(value).path if value.startswith("http") else value

        series = re.search(r"/shows/(?P<series_id>[0-9a-f-]{36})", path, re.IGNORECASE)
        if series:
            return {"kind": "series", "series_id": series.group("series_id")}

        if re.fullmatch(r"[0-9a-f-]{36}", value, re.IGNORECASE):
            return {"kind": "series", "series_id": value}

        video = re.search(r"/(?:watch/)?(?:vod|replay)/(?P<video_id>\d+)", path, re.IGNORECASE)
        if video:
            return {"kind": "episode", "video_id": video.group("video_id")}

        if re.fullmatch(r"\d+", value):
            return {"kind": "episode", "video_id": value}

        raise ValueError(f"Could not parse Virgin Media title: {title}")

    @staticmethod
    def _stream_metadata(playback: dict) -> dict:
        metadata = ((playback.get("response") or {}).get("metadata") or {}).get("metadata") or {}
        return metadata if isinstance(metadata, dict) else {}

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            if value is None:
                return default
            if isinstance(value, int):
                return value
            match = re.search(r"(\d+)", str(value))
            return int(match.group(1)) if match else int(value)
        except Exception:
            return default

    @staticmethod
    def _year(value: str | None) -> int | None:
        if value and (match := re.match(r"(\d{4})", value)):
            return int(match.group(1))
        return None

    @staticmethod
    def _slug_title(url: str) -> str:
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        return re.sub(r"[-_]+", " ", slug).title() if slug else "Virgin Media"

    @staticmethod
    def _show_title(metadata: dict, url: str) -> str:
        title = metadata.get("title") or VM._slug_title(url)
        return re.sub(r"\s+Ep\.?\s*\d+.*$", "", title, flags=re.IGNORECASE).strip() or title

    @staticmethod
    def _clean_title(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _slugify(value: Any) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", VM._clean_title(value).lower()).strip("-")
        return slug or "title"

    @staticmethod
    def _video_url(video_id: str, fallback: str) -> str:
        if fallback and f"/{video_id}" in fallback:
            return fallback
        return f"https://play.virginmediatelevision.ie/replay/{video_id}"

    @staticmethod
    def _mark_descriptive_audio(tracks: Tracks) -> None:
        for track in tracks.audio:
            adaptation = track.data.get("dash", {}).get("adaptation_set")
            role = adaptation.find("Role") if adaptation is not None else None
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True
