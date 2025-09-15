from __future__ import annotations

import json
import re
import uuid
from collections.abc import Generator
from typing import Any, Optional
from urllib.parse import urlparse, urljoin, quote
from http.cookiejar import MozillaCookieJar
from concurrent.futures import ThreadPoolExecutor

import click
from click import Context

try:
    from devine.core.credential import Credential  # type: ignore
    from devine.core.manifests import DASH, HLS  # type: ignore
    from devine.core.search_result import SearchResult  # type: ignore
    from devine.core.service import Service  # type: ignore
    from devine.core.titles import Episode, Movie, Movies, Series  # type: ignore
    from devine.core.tracks import Chapter, Chapters, Tracks  # type: ignore
except ImportError:
    try:
        from unshackle.core.credential import Credential
        from unshackle.core.manifests import DASH, HLS
        from unshackle.core.search_result import SearchResult
        from unshackle.core.service import Service
        from unshackle.core.titles import Episode, Movie, Movies, Series
        from unshackle.core.tracks import Chapter, Chapters, Tracks
    except ImportError:
        raise ImportError("PLEX service requires devine or unshackle to be installed")

from requests import Request


class PLEX(Service):
    """
    \b
    Service code for Plex's free streaming service (https://watch.plex.tv/).

    \b
    Version: 1.0.3
    Author: stabbedbybrick
    Authorization: None
    Geofence: API and downloads are locked into whatever region the user is in
    Robustness:
      L3: 720p, AAC2.0

    \b
    Tips:
        - Input should be complete URL:
          SHOW: https://watch.plex.tv/show/taboo-2017
          EPISODE: https://watch.plex.tv/show/taboo-2017/season/1/episode/1
          MOVIE: https://watch.plex.tv/movie/the-longest-yard
    """

    ALIASES = ("plextv",)

    @staticmethod
    @click.command(name="PLEX", short_help="https://watch.plex.tv/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> PLEX:
        return PLEX(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)
    
    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        self.session.headers.update(
            {
                "accept": "application/json",
                "x-plex-client-identifier": str(uuid.uuid4()),
                "x-plex-language": "en",
                "x-plex-product": "Plex Mediaverse",
                "x-plex-provider-version": "6.5.0",
            }
        )
        user = self._request("POST", self.config["endpoints"]["user"])
        if not (auth_token := user.get("authToken")):
            raise ValueError(f"PLEX authentication failed: {user}")
        
        self.auth_token = auth_token
        self.session.headers.update({"x-plex-token": self.auth_token})


    def search(self) -> Generator[SearchResult, None, None]:
        results = self._request(
            "GET", "https://discover.provider.plex.tv/library/search",
            params={
                "searchTypes": "movies,tv",
                "searchProviders": "discover,plexAVOD,plexFAST",
                "includeMetadata": 1,
                "filterPeople": 1,
                "limit": 10,
                "query": quote(self.title),
            },
        )

        for result in results["MediaContainer"]["SearchResults"]:
            if "free on demand" not in result.get("title", "").lower():
                continue

            for result in result["SearchResult"]:
                kind = result.get("Metadata", {}).get("type")
                slug = result.get("Metadata", {}).get("slug")

                yield SearchResult(
                    id_=f"https://watch.plex.tv/{kind}/{slug}",
                    title=result.get("Metadata", {}).get("title"),
                    description=result.get("Metadata", {}).get("description"),
                    label=kind,
                    url=f"https://watch.plex.tv/{kind}/{slug}",
                )

    def get_titles(self) -> Movies | Series:
        url_pattern = re.compile(
            r"^https://watch.plex.tv/"
            r"(?:[a-z]{2}(?:-[A-Z]{2})?/)??"
            r"(?P<type>movie|show)/"
            r"(?P<id>[\w-]+)"
            r"(?P<url_path>(/season/\d+/episode/\d+))?"
        )

        match = url_pattern.match(self.title)
        if not match:
            raise ValueError(f"Could not parse ID from title: {self.title}")

        kind, guid, url_path = (match.group(i) for i in ("type", "id", "url_path"))

        if kind == "show":
            if url_path is not None:
                path = urlparse(self.title).path
                url = re.sub(r"/[a-z]{2}(?:-[A-Z]{2})?/", "/", path)
                episode = self._episode(url)
                return Series(episode)
            
            episodes = self._series(guid)
            return Series(episodes)

        elif kind == "movie":
            movie = self._movie(guid)
            return Movies(movie)

        else:
            raise ValueError(f"Could not parse content type from title: {self.title}")

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        dash_media = next((x for x in title.data.get("Media", []) if x.get("protocol", "").lower() == "dash"), None)
        if not dash_media:
            hls_media = next((x for x in title.data.get("Media", []) if x.get("protocol", "").lower() == "hls"), None)
        
        media = dash_media or hls_media
        if not media:
            raise ValueError("Failed to find either DASH or HLS media")
        
        manifest = DASH if dash_media else HLS

        media_key = media.get("id")
        has_drm = media.get("drm")

        if has_drm:
            manifest_url = (
                self.config["endpoints"]["base_url"]
                + self.config["endpoints"]["manifest_drm"].format(media_key, self.auth_token)
            )
            title.data["license_url"] = (
                self.config["endpoints"]["base_url"]
                + self.config["endpoints"]["license"].format(media_key, self.auth_token)
            )
        else:
            manifest_url = (
                self.config["endpoints"]["base_url"]
                + self.config["endpoints"]["manifest_clear"].format(media_key, self.auth_token)
            )
            title.data["license_url"] = None

        tracks = manifest.from_url(manifest_url, self.session).to_tracks(language="en")

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        if not (markers := title.data.get("Marker")):
            try:
                metadata = self._request(
                "POST", "/playQueues",
                    params={
                        "uri": self.config["endpoints"]["provider"] + title.data.get("key"),
                        "type": "video",
                        "continuous": "1",
                    },
                )
                markers = next((
                    x.get("Marker") for x in metadata.get("MediaContainer", {}).get("Metadata", [])
                    if x.get("key") == title.data.get("key")), [])

            except Exception as e:
                self.log.debug("Failed to fetch markers: %s", e)
                return Chapters()
        
        if not markers:
            return Chapters()

        chapters = []
        for cue in markers:
            if cue.get("startTimeOffset", 0) > 0:
                chapters.append(Chapter(name=cue.get("type", "").title(), timestamp=cue.get("startTimeOffset")))

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

    def _fetch_season(self, url: str) -> list:
        return self._request("GET", url).get("MediaContainer", {}).get("Metadata", [])

    def _series(self, guid: str) -> list[Episode]:
        data = self._request("GET", f"/library/metadata/show:{guid}")

        meta_key = data.get("MediaContainer", {}).get("Metadata", [])[0].get("key")
        if not meta_key:
            raise ValueError("Failed to find metadata for title")

        series = self._request("GET", f"{self.config['endpoints']['base_url']}/{meta_key}")

        seasons = [
            self.config["endpoints"]["base_url"] + item.get("key")
            for item in series.get("MediaContainer", {}).get("Metadata", [])
            if item.get("type") == "season"
        ]
        if not seasons:
            raise ValueError("Failed to find seasons for title")

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(self._fetch_season, seasons))

        episodes = [
            Episode(
                id_=episode.get("ratingKey"),
                service=self.__class__,
                name=episode.get("title"),
                season=int(episode.get("parentIndex", 0)),
                number=int(episode.get("index", 0)),
                title=re.sub(r"\s*\(\d{4}\)", "", episode.get("grandparentTitle", "")),
                # year=episode.get("year"),
                data=episode,
            )
            for season in results
            for episode in season
            if episode.get("type") == "episode"
        ]

        return episodes

    def _movie(self, guid: str) -> Movie:
        data = self._request("GET", f"/library/metadata/movie:{guid}")
        movie = data.get("MediaContainer", {}).get("Metadata", [])[0]
        if not movie:
            raise ValueError(f"Could not find any data for ID {guid}")

        movies = [
            Movie(
                id_=movie.get("ratingKey"),
                service=self.__class__,
                name=movie.get("title"),
                year=movie.get("year"),
                data=movie,
            )
        ]

        return movies

    def _episode(self, path: str) -> Episode:
        data = self._request("GET", self.config["endpoints"]["screen"] + path)
        meta_key = data.get("actions", [])[0].get("data", {}).get("key")
        if not meta_key:
            raise ValueError("Failed to find metadata for title")

        metadata = self._request(
            "POST", "/playQueues",
            params={
                "uri": self.config["endpoints"]["provider"] + meta_key,
                "type": "video",
                "continuous": "1",
            },
        )

        episode = next((x for x in metadata.get("MediaContainer", {}).get("Metadata", []) if x.get("key") == meta_key), None)
        if not episode:
            raise ValueError("Failed to find metadata for title")

        episodes = [
            Episode(
                id_=episode.get("ratingKey"),
                service=self.__class__,
                name=episode.get("title"),
                season=int(episode.get("parentIndex", 0)),
                number=int(episode.get("index", 0)),
                title=re.sub(r"\s*\(\d{4}\)", "", episode.get("grandparentTitle", "")),
                # year=episode.get("year"),
                data=episode,
            )
        ]

        return episodes

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any[dict | str]:
        url = urljoin(self.config["endpoints"]["base_url"], endpoint)

        prep = self.session.prepare_request(Request(method, url, **kwargs))

        response = self.session.send(prep)
        if response.status_code not in (200, 201, 426):
            raise ConnectionError(f"{response.text}")

        try:
            return json.loads(response.content)

        except json.JSONDecodeError:
            return response.text


