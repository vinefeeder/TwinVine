from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Generator
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import click
from click import Context
from requests import Request
from envied.core.manifests import DASH, HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Subtitle, Tracks


class NINE(Service):
    """
    Service code for 9Now streaming service (https://www.9now.com.au/).

    \b
    Version: 1.0.0
    Authored: billybanana
    Authorization: None
    Geofence: AU (API and downloads)
    Robustness:
      Widevine: L3 720p, some 1080p clips
      Clear HLS: up to source quality

    \b
    Tips:
        - Search by show name:
          envied.dl NINE "Travel Guides"
        - Use complete URLs:
          SERIES: https://www.9now.com.au/travel-guides
          EPISODE: https://www.9now.com.au/travel-guides/season-9/episode-2
          CLIP: https://www.9now.com.au/premier-league-epl-football/season-20252026/clip-cmeop4x67000m0hmmc1822v1i
    """

    GEOFENCE = ("au",)
    ALIASES = ("9now", "nine",)

    @staticmethod
    @click.command(name="NINE", short_help="https://www.9now.com.au/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> NINE:
        return NINE(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update(self.config["headers"])

    def search(self) -> Generator[SearchResult, None, None]:
        results = self._request(
            "GET",
            self.config["endpoints"]["search"],
            params={"q": self.title.strip(), "device": "web"},
        )

        seen = set()
        for group in results.get("results", []):
            if group.get("title") != "Search results":
                continue

            for result in group.get("items", []):
                link = result.get("link") or {}
                web_url = link.get("webUrl")
                if result.get("type") != "tv-series" or not web_url:
                    continue

                url = urljoin("https://www.9now.com.au", web_url)
                if url in seen:
                    continue
                seen.add(url)

                yield SearchResult(
                    id_=url,
                    title=result.get("name") or result.get("displayName"),
                    description=result.get("description"),
                    label="SERIES",
                    url=url,
                )

    def get_titles(self) -> Movies | Series:
        parsed = self._parse_title(self.title)
        series_slug = parsed["series"]

        if parsed["kind"] == "episode":
            if parsed.get("year"):
                episode = self._episode_from_year(series_slug, parsed["year"], parsed["episode"])
            else:
                episode = self._episode(series_slug, parsed["season"], parsed["episode"])
            return Series([episode])

        if parsed["kind"] == "clip":
            clip = self._clip(series_slug, parsed["season"], parsed["clip"])
            return Series([clip])

        episodes = self._series(series_slug)
        return Series(episodes)

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        brightcove_id = title.id
        if not brightcove_id:
            raise ValueError("Could not find Brightcove ID for this title")

        data = self._request(
            "GET",
            self.config["endpoints"]["playback"].format(
                account=self.config["brightcove"]["account"],
                id=brightcove_id,
            ),
            headers={
                "BCOV-POLICY": self.config["brightcove"]["policy_key"],
                **self.config["headers"],
            },
        )

        title.data["chapters"] = data.get("cue_points") or title.data.get("video", {}).get("cuePoints")
        title.data["duration"] = data.get("duration") or title.data.get("video", {}).get("duration")

        source = self._best_source(data.get("sources", []))
        if not source:
            raise ValueError("Could not find a playable Brightcove source")

        title.data["license_url"] = (
            (source.get("key_systems") or {})
            .get("com.widevine.alpha", {})
            .get("license_url")
        )

        source_url = source.get("src")
        if source.get("type") == "application/dash+xml":
            tracks = DASH.from_url(source_url, self.session).to_tracks(title.language)
        else:
            tracks = HLS.from_url(source_url, self.session).to_tracks(title.language)

        self._add_subtitles(tracks, data.get("text_tracks", []))
        self._mark_descriptive_audio(tracks)

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        cue_points = title.data.get("chapters") or []
        duration = title.data.get("duration")
        chapters = []
        for cue in cue_points:
            timestamp = cue.get("time")
            if timestamp and timestamp > 0:
                chapters.append(Chapter(timestamp=self._chapter_timestamp_ms(timestamp, duration)))

        return Chapters(chapters)

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Movie | Episode, track: Any) -> bytes | str | None:
        if license_url := title.data.get("license_url"):
            r = self.session.post(
                url=license_url,
                data=challenge,
                headers={
                    "Content-Type": "application/octet-stream",
                    **self.config["headers"],
                },
            )
            if r.status_code != 200:
                raise ConnectionError(r.text)
            return r.content

        return None

    # Service specific

    def _series(self, series_slug: str) -> list[Episode]:
        metadata = self._request(
            "GET",
            self.config["endpoints"]["series"].format(series=series_slug),
            params={"device": "web"},
        )

        seasons = self._extract_seasons(metadata)
        if not seasons:
            raise ValueError(f"Could not find seasons for title: {series_slug}")

        episodes = []
        for season_slug in seasons:
            episodes.extend(self._season_episodes(series_slug, season_slug))
            episodes.extend(self._season_clips(series_slug, season_slug, metadata))

        return sorted(episodes, key=lambda item: item.data.get("_sort", (0, 0)), reverse=True)

    def _season_episodes(self, series_slug: str, season_slug: str) -> list[Episode]:
        data = self._request(
            "GET",
            self.config["endpoints"]["season_episodes"].format(series=series_slug, season=season_slug),
            params={"device": "web"},
        )

        return [
            self._build_episode(episode, series_slug=series_slug, season_slug=season_slug)
            for episode in data.get("episodes", {}).get("items", [])
            if (episode.get("video") or {}).get("brightcoveId")
        ]

    def _episode(self, series_slug: str, season_slug: str, episode_slug: str) -> Episode:
        data = self._request(
            "GET",
            self.config["endpoints"]["episode"].format(
                series=series_slug,
                season=season_slug,
                episode=episode_slug,
            ),
            params={"device": "web"},
        )
        episode = data.get("episode")
        if not episode:
            raise ValueError(f"Could not find episode: {self.title}")

        return self._build_episode(episode, series_slug=series_slug, season_slug=season_slug)

    def _episode_from_year(self, series_slug: str, year: str, episode_slug: str) -> Episode:
        episodes = self._series(series_slug)
        episode_number = self._slug_number(episode_slug)
        episode = next(
            (
                item for item in episodes
                if item.season == int(year) and item.number == episode_number
            ),
            None,
        )
        if not episode:
            episode = next((item for item in episodes if item.number == episode_number), None)
        if not episode:
            raise ValueError(f"Could not find episode: {self.title}")

        return episode

    def _season_clips(self, series_slug: str, season_slug: str, series_metadata: dict) -> list[Episode]:
        data = self._request(
            "GET",
            self.config["endpoints"]["season"].format(series=series_slug, season=season_slug),
            params={"device": "web"},
        )
        clips = self._extract_clips(data)

        if not clips:
            clips = [
                clip for clip in self._extract_clips(series_metadata)
                if (clip.get("partOfSeason") or {}).get("slug") == season_slug
            ]

        clips = sorted(clips, key=self._date_sort_key, reverse=True)
        return [
            self._build_clip(clip, series_slug=series_slug, season_slug=season_slug, clip_number=index)
            for index, clip in enumerate(clips, start=1)
            if (clip.get("video") or {}).get("brightcoveId")
        ]

    def _clip(self, series_slug: str, season_slug: str, clip_slug: str) -> Episode:
        clips = self._season_clips(series_slug, season_slug, {})
        target = f"/{series_slug}/{season_slug}/{clip_slug}".lower()
        clip = next(
            (
                item for item in clips
                if NINE._same_clip_path((item.data.get("link") or {}).get("webUrl"), target)
            ),
            None,
        )
        if not clip:
            raise ValueError(f"Could not find clip: {self.title}")

        return clip

    def _build_episode(self, item: dict, *, series_slug: str, season_slug: str) -> Episode:
        season_number = self._season_number(item, season_slug)
        episode_number = int(item.get("episodeNumber") or self._slug_number(item.get("slug")) or 0)

        item["_sort"] = (season_number, episode_number)
        return Episode(
            id_=(item.get("video") or {}).get("brightcoveId"),
            service=self.__class__,
            title=self._series_name(item, series_slug),
            season=season_number,
            number=episode_number,
            name=item.get("name") or item.get("displayName"),
            year=self._year(item),
            language="en",
            data=item,
        )

    def _build_clip(self, item: dict, *, series_slug: str, season_slug: str, clip_number: int) -> Episode:
        season_number = self._season_number(item, season_slug)
        item["_sort"] = (season_number, 100000 - clip_number)

        return Episode(
            id_=(item.get("video") or {}).get("brightcoveId"),
            service=self.__class__,
            title=self._series_name(item, series_slug),
            season=season_number,
            number=0,
            name=item.get("displayName") or item.get("name"),
            year=self._year(item),
            language="en",
            data=item,
        )

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        url = endpoint if endpoint.startswith("http") else urljoin(self.config["endpoints"]["base_url"], endpoint)

        prep = self.session.prepare_request(Request(method, url, **kwargs))
        response = self.session.send(prep)
        if response.status_code != 200:
            raise ConnectionError(response.text)

        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            return response.text

    @staticmethod
    def _parse_title(title: str) -> dict[str, str]:
        title_re = re.compile(
            r"^(?:https?://(?:www\.)?9now\.com\.au/)?"
            r"(?P<series>[a-z0-9-]+)"
            r"(?:/(?P<season>season-[^/]+|special|\d{4})"
            r"/(?:(?P<episode>episode-\d+)|(?P<clip>clip-[^/?#]+))"
            r"(?:/[^/?#]+)?)?"
            r"/?$",
            re.IGNORECASE,
        )

        match = title_re.match(title.strip())
        if not match:
            raise ValueError(f"Could not parse 9Now title: {title}")

        groups = match.groupdict()
        if groups.get("clip"):
            groups["kind"] = "clip"
        elif groups.get("episode"):
            groups["kind"] = "episode"
            if groups["season"].isdigit():
                groups["year"] = groups["season"]
            else:
                groups["year"] = None
        else:
            groups["kind"] = "series"
            groups["year"] = None

        return groups

    @staticmethod
    def _same_clip_path(web_url: str | None, target: str) -> bool:
        if not web_url:
            return False

        path = web_url.lower().rstrip("/")
        return path == target or path.startswith(f"{target}/")

    @staticmethod
    def _extract_seasons(metadata: dict) -> list[str]:
        seasons = []
        for season in metadata.get("seasons", []):
            slug = season.get("slug")
            if slug and slug not in seasons:
                seasons.append(slug)

        return seasons

    @staticmethod
    def _extract_clips(metadata: dict) -> list[dict]:
        clips = []
        for block in metadata.get("items", []):
            for item in block.get("items", []):
                if item.get("type") == "clip":
                    clips.append(item)

        return clips

    @staticmethod
    def _best_source(sources: list[dict]) -> dict | None:
        dash_drm = next(
            (
                source for source in sources
                if source.get("type") == "application/dash+xml"
                and (source.get("key_systems") or {}).get("com.widevine.alpha")
                and source.get("src")
            ),
            None,
        )
        if dash_drm:
            return dash_drm

        dash_clear = next(
            (
                source for source in sources
                if source.get("type") == "application/dash+xml" and source.get("src")
            ),
            None,
        )
        if dash_clear:
            return dash_clear

        return next(
            (
                source for source in sources
                if source.get("type") == "application/x-mpegURL"
                and source.get("src", "").startswith("https://")
            ),
            None,
        )

    @staticmethod
    def _add_subtitles(tracks: Tracks, text_tracks: list[dict]) -> None:
        for text_track in text_tracks:
            source_url = text_track.get("src")
            if not source_url and text_track.get("sources"):
                source_url = next(
                    (
                        source.get("src") for source in text_track["sources"]
                        if source.get("src", "").startswith("https://")
                    ),
                    text_track["sources"][0].get("src"),
                )
            if not source_url:
                continue

            tracks.add(
                Subtitle(
                    id_=hashlib.md5(source_url.encode()).hexdigest()[0:6],
                    url=source_url,
                    codec=Subtitle.Codec.from_mime(NINE._subtitle_codec(text_track, source_url)),
                    language=text_track.get("srclang") or "en",
                    sdh=text_track.get("kind") == "captions",
                )
            )

    @staticmethod
    def _subtitle_codec(text_track: dict, source_url: str) -> str:
        mime_type = (text_track.get("mime_type") or "").lower()
        if "webvtt" in mime_type or source_url.lower().split("?")[0].endswith(".vtt"):
            return "vtt"
        if "ttml" in mime_type or "xml" in mime_type:
            return "ttml"
        if "srt" in mime_type or source_url.lower().split("?")[0].endswith(".srt"):
            return "srt"

        return source_url.lower().split("?")[0].rsplit(".", 1)[-1] or "vtt"

    @staticmethod
    def _chapter_timestamp_ms(timestamp: int | float, duration: int | float | None) -> int:
        if duration and timestamp <= duration / 1000:
            return int(round(timestamp * 1000))

        return int(round(timestamp))

    @staticmethod
    def _mark_descriptive_audio(tracks: Tracks) -> None:
        for track in tracks.audio:
            role = track.data.get("dash", {}).get("representation")
            role = role.find("Role") if role is not None else None
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

    @staticmethod
    def _date_sort_key(item: dict) -> datetime:
        date = item.get("availability") or item.get("updatedAt") or item.get("airDate") or ""
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(date, fmt)
            except ValueError:
                pass

        return datetime.min

    @staticmethod
    def _series_name(item: dict, series_slug: str) -> str:
        series = item.get("partOfSeries") or {}
        return series.get("name") or series_slug.replace("-", " ").title()

    @staticmethod
    def _season_number(item: dict, season_slug: str) -> int:
        season = item.get("partOfSeason") or {}
        if season.get("seasonNumber"):
            return int(season["seasonNumber"])

        if match := re.search(r"season-(\d{4})(?:\d{4})?", season_slug):
            return int(match.group(1))

        if match := re.search(r"season-(\d+)", season_slug):
            return int(match.group(1))

        if season_slug == "special":
            return 0

        return 0

    @staticmethod
    def _slug_number(slug: str | None) -> int:
        if not slug:
            return 0
        match = re.search(r"(\d+)", slug)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _year(item: dict) -> int | None:
        date = item.get("airDate") or item.get("availability") or ""
        if match := re.match(r"(\d{4})", date):
            return int(match.group(1))
        return None
