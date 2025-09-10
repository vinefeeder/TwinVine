from __future__ import annotations

import json
import re
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urljoin

import click
from click import Context

try:
    from devine.core.manifests import DASH  # type: ignore
    from devine.core.search_result import SearchResult  # type: ignore
    from devine.core.service import Service  # type: ignore
    from devine.core.titles import Episode, Movie, Movies, Series  # type: ignore
    from devine.core.tracks import Chapter, Chapters, Tracks  # type: ignore
except ImportError:
    try:
        from unshackle.core.manifests import DASH
        from unshackle.core.search_result import SearchResult
        from unshackle.core.service import Service
        from unshackle.core.titles import Episode, Movie, Movies, Series
        from unshackle.core.tracks import Chapter, Chapters, Tracks
    except ImportError:
        raise ImportError("CWTV service requires devine or unshackle to be installed")

from lxml import etree
from requests import Request


class CWTV(Service):
    """
    \b
    Service code for CWTV streaming service (https://www.cwtv.com/).

    \b
    Version: 1.0.0
    Author: stabbedbybrick
    Authorization: None
    Geofence: US (API and downloads)
    Robustness:
      L3: 1080p, AAC2.0

    \b
    Tips:
        - Input should be complete URL:
          SHOW: https://www.cwtv.com/shows/sullivans-crossing
          EPISODE: https://www.cwtv.com/series/sullivans-crossing/new-beginnings/?play=7778f443-c7cc-4843-8e3c-d97d53b813d2
          MOVIE: https://www.cwtv.com/movies/burnt/
    """

    GEOFENCE = ("us",)
    ALIASES = ("cw",)

    @staticmethod
    @click.command(name="CWTV", short_help="https://www.cwtv.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> CWTV:
        return CWTV(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update(self.config["headers"])

    def search(self) -> Generator[SearchResult, None, None]:
        results = self._request(
            "GET", "https://www.cwtv.com/search/",
            params={
                "q": quote(self.title),
                "format": "json2",
                "service": "t",
                "cwuid": "8195356001251527455",
            },
        )

        for result in results["items"]:
            if result.get("type") not in ("shows", "series", "movies"):
                continue

            video_type = "shows" if result.get("type") in ("series", "shows") else "movies"

            yield SearchResult(
                id_=f"https://www.cwtv.com/{video_type}/{result.get('show_slug')}",
                title=result.get("title"),
                description=result.get("description_long"),
                label=result.get("type").capitalize(),
                url=f"https://www.cwtv.com/{video_type}/{result.get('show_slug')}",
            )

    def get_titles(self) -> Movies | Series:
        url_pattern = re.compile(
            r"^https:\/\/www\.cwtv\.com\/"
            r"(?P<type>series|shows|movies)\/"
            r"(?P<id>[\w-]+(?:\/[\w-]+)?)"
            r"(?:\/?\?play=(?P<play_id>[\w-]+))?"
        )

        match = url_pattern.match(self.title)
        if not match:
            raise ValueError(f"Could not parse ID from title: {self.title}")

        kind, guid, play_id = (match.group(i) for i in ("type", "id", "play_id"))

        if kind in ("series", "shows") and not play_id:
            episodes = self._series(guid)
            return Series(episodes)

        elif kind == "movies" and not play_id:
            movie = self._movie(guid)
            return Movies(movie)

        elif kind in ("series", "shows") and play_id:
            episode = self._episode(play_id)
            return Series(episode)

        else:
            raise ValueError(f"Could not parse conent type from title: {self.title}")

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        data = self._request(
            "GET", self.config["endpoints"]["playback"].format(title.id),
            headers={"accept": f'application/json;pk={self.config["policy_key"]}'},
        )
        has_drm = data.get("custom_fields", {}).get("is_drm") == "1"

        title.data["chapters"] = data.get("cue_points")

        source_manifest = next(
            (source.get("src") for source in data["sources"] if source.get("type") == "application/dash+xml"),
            None,
        )
        if not source_manifest:
            raise ValueError("Could not find DASH manifest")
        
        license_url = next((
            source.get("key_systems", {}).get("com.widevine.alpha", {}).get("license_url")
            for source in data["sources"] if source.get("src") == source_manifest),
            None,
        )
        if has_drm and not license_url:
            raise ValueError("Could not find license URL")
        
        title.data["license_url"] = license_url

        manifest = self.trim_duration(source_manifest)
        tracks = DASH.from_text(manifest, source_manifest).to_tracks(language="en")

        for track in tracks.audio:
            role = track.data["dash"]["representation"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        if not title.data.get("chapters"):
            return Chapters()

        chapters = []
        for cue in title.data["chapters"]:
            if cue["time"] > 0:
                chapters.append(Chapter(timestamp=cue["time"]))

        return Chapters(chapters)
    
    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Movie | Episode, track: Any) -> bytes | str | None:
        if license_url := title.data.get("license_url"):
            r = self.session.post(url=license_url, data=challenge)
            if r.status_code != 200:
                raise ConnectionError(r.text)
            return r.content

        return None

    # Service specific

    def _series(self, guid: str) -> list[Episode]:
        series = self._request("GET", f"/feed/app-2/videos/show_{guid}/type_episodes/apiversion_24/device_androidtv")
        if not series.get("items"):
            raise ValueError(f"Could not find any episodes with ID {guid}")
        
        episodes = [
            Episode(
                id_=episode.get("bc_video_id"),
                service=self.__class__,
                name=episode.get("title"),
                season=int(episode.get("season") or 0),
                number=int(episode.get("episode_in_season") or 0),
                title=episode.get("series_name") or episode.get("show_title"),
                year=episode.get("release_year"),
                data=episode,
            )
            for episode in series.get("items")
            if episode.get("fullep", 0) == 1
        ]
        
        return episodes

    def _movie(self, guid: str) -> Movie:
        data = self._request("GET", f"/feed/app-2/videos/show_{guid}/type_episodes/apiversion_24/device_androidtv")
        if not data.get("items"):
            raise ValueError(f"Could not find any data for ID {guid}")

        movies = [
            Movie(
                id_=movie.get("bc_video_id"),
                service=self.__class__,
                name=movie.get("series_name") or movie.get("show_title"),
                year=movie.get("release_year"),
                data=movie,
            )
            for movie in data.get("items")
            if movie.get("fullep", 0) == 1
        ]

        return movies

    def _episode(self, guid: str) -> Episode:
        data = self._request("GET", f"/feed/app-2/video-meta/guid_{guid}/apiversion_24/device_androidtv")
        if not data.get("video"):
            raise ValueError(f"Could not find any data for ID {guid}")

        episodes = [
            Episode(
                id_=data.get("video", {}).get("bc_video_id"),
                service=self.__class__,
                name=data.get("video", {}).get("title"),
                season=int(data.get("video", {}).get("season") or 0),
                number=int(data.get("video", {}).get("episode_in_season") or 0),
                title=data.get("video", {}).get("series_name") or data.get("video", {}).get("show_title"),
                year=data.get("video", {}).get("release_year"),
                data=data.get("video"),
            )
        ]

        return episodes

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any[dict | str]:
        url = urljoin(self.config["endpoints"]["base_url"], endpoint)

        prep = self.session.prepare_request(Request(method, url, **kwargs))

        response = self.session.send(prep)
        if response.status_code != 200:
            raise ConnectionError(f"{response.text}")

        try:
            return json.loads(response.content)

        except json.JSONDecodeError:
            return response.text
        
    @staticmethod
    def trim_duration(source_manifest: str) -> str:
        """
        The last segment on all tracks return a 404 for some reason, causing a failed download.
        So we trim the duration by exactly one segment to account for that.

        TODO: Calculate the segment duration instead of assuming length.
        """
        manifest = DASH.from_url(source_manifest).manifest
        period_duration = manifest.get("mediaPresentationDuration")
        period_duration = DASH.pt_to_sec(period_duration)

        hours, minutes, seconds = str(timedelta(seconds=period_duration - 6)).split(":")
        new_duration = f"PT{hours}H{minutes}M{seconds}S"
        manifest.set("mediaPresentationDuration", new_duration)

        return etree.tostring(manifest, encoding="unicode")

