from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Generator
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import click
import jwt
from click import Context
from requests import Request
from envied.core.credential import Credential
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapters, Subtitle, Tracks


class SBS(Service):
    """
    \b
    Service code for SBS ondemand streaming service (https://www.sbs.com.au/ondemand/).

    \b
    Version: 1.0.2
    Author: stabbedbybrick
    Authorization: Credentials
    Geofence: AU (API and downloads)
    Robustness:
      AES: 720p, AAC2.0

    \b
    Tips:
        - Input should be complete URL:
          SERIES: https://www.sbs.com.au/ondemand/tv-series/reckless
          EPISODE: https://www.sbs.com.au/ondemand/tv-series/reckless/season-1/reckless-s1-ep1/2459384899653
          MOVIE: https://www.sbs.com.au/ondemand/movie/silence/1363535939614
          SPORT: https://www.sbs.com.au/ondemand/sports-series/australian-championship-2025/football-australian-championship-2025/australian-championship-2025-s2025-ep40/2457638979614

    """

    GEOFENCE = ("au",)

    @staticmethod
    @click.command(name="SBS", short_help="https://www.sbs.com.au/ondemand/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> SBS:
        return SBS(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update(self.config["headers"])

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        cache = self.cache.get(f"tokens_{credential.sha1}")

        if cache and not cache.expired:
            self.log.info(" + Using cached tokens")
            tokens = cache.data
        elif cache and cache.expired:
            self.log.info("Refreshing cached tokens...")
            payload = {
                "refreshToken": cache.data["refreshToken"],
                "deviceName": "Android TV",
                "refreshTokenInBody": True,
            }

            r = self.session.post(
                self.config["endpoints"]["refresh"],
                json=payload,
            )
            r.raise_for_status()
            tokens = r.json()
            expiry = jwt.decode(tokens.get("accessToken"), options={"verify_signature": False}).get("exp")
            cache.set(tokens, expiration=expiry - 60)
        else:
            self.log.info(" + Logging in...")
            payload = {
                "email": credential.username,
                "password": credential.password,
                "deviceName": "Android TV",
                "refreshTokenInBody": True,
            }

            response = self.session.post(
                self.config["endpoints"]["auth"], json=payload
            )
            response.raise_for_status()
            tokens = response.json()
            expiry = jwt.decode(tokens.get("accessToken"), options={"verify_signature": False}).get("exp")
            cache.set(tokens, expiration=expiry - 60)

        self.session.headers.update({"Authorization": "Bearer {}".format(tokens["accessToken"])})

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "q": self.title.strip(),
        }

        results = self._request("GET", "https://content-search.pr.sbsod.com/catalogue", params=params)["items"]

        for result in results:
            if result.get("entityType") in ("PAGE"):
                continue

            label = result.get("entityType")
            slug = result.get("slug")
            title = result.get("title")
            description = result.get("description")
            yield SearchResult(
                id_=f"https://www.sbs.com.au/ondemand/{label}/{slug}",
                title=title,
                description=description,
                label=label,
                url=f"https://www.sbs.com.au/ondemand/{label}/{slug}",
            )


    def get_titles(self) -> Movies | Series:
        regex = re.compile(
            r"^https://www.sbs.com.au/ondemand/"
            r"(?P<entity>tv-series|tv-program|sports-series|sports-program|movie|watch)"
            r"(?:/|/.*/)"
            r"(?P<id>[^/]+)/?$"
        )

        match = regex.search(self.title)
        if not match:
            raise ValueError(f"Invalid URL input: {self.title}")

        entity_type, entity_id = (match.group(i) for i in ("entity", "id"))

        if entity_type in ("movie", "tv-program", "sports-program") and entity_id.isdigit():
            movie = self._movie(entity_id)
            return Movies(movie)
        
        elif entity_id.isdigit():
            episode = self._episode(entity_id)
            return Series(episode)

        elif entity_type in ("tv-series", "sports-series"):
            episodes = self._series(urlparse(self.title).path)
            return Series(episodes)

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        json_data = {
            "deviceClass": "androidtv",
            "advertising": {
                "headerBidding": True,
                "telariaID": "",
                "subtitle": "en",
                "resume": False,
            },
            "streamOptions": {
                "audio": "demuxed",
            },
        }
        content = self._request("POST", f"{self.config['endpoints']['playback']}/{title.id}", json=json_data)
        provider = next((x for x in content.get("streamProviders", []) if x.get("type", "").lower() == "hls"), None)

        if not provider:
            raise ValueError("Unable to find manifest for this title")

        manifest = provider.get("url")
        tracks = HLS.from_url(manifest, self.session).to_tracks(title.language)

        if subtitles := provider.get("textTracks", []):
            for subtitle in subtitles:
                url = subtitle.get("url")
                lang = subtitle.get("lang")
                codec = subtitle.get("url").split(".")[-1]
                tracks.add(
                    Subtitle(
                        id_=hashlib.md5(url.encode()).hexdigest()[0:6],
                        url=url,
                        codec=Subtitle.Codec.from_codecs(codec),
                        language=lang,
                        sdh="CC" in subtitle.get("name", ""),
                    )
                )

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        return Chapters()

    # Service specific

    def _series(self, path: str) -> Episode:
        if "ondemand" in path:
            path = path.split("ondemand")[1]

        metadata = self._request("GET", f"https://catalogue.pr.sbsod.com{path}")

        seasons = metadata.get("seasons")
        if not seasons:
            raise ValueError(f"Failed to find seasons for title: {path}")
        
        episodes = []
        for season in seasons:
            for episode in season.get("episodes"):
                episodes.append(
                    Episode(
                        id_=episode.get("mpxMediaID"),
                        service=self.__class__,
                        title=episode.get("seriesTitle"),
                        season=int(episode.get("seasonNumber", 0)),
                        number=int(episode.get("episodeNumber", 0)),
                        name=episode.get("title"),
                        year=episode.get("releaseYear"),
                        language=metadata.get("localeID") or "en",
                        data=episode,
                    )
                )

        return episodes

    def _movie(self, entity_id: str) -> Movie:
        metadata = self._request("GET", f"https://catalogue.pr.sbsod.com/mpx-media/{entity_id}")

        return [
            Movie(
                id_=metadata.get("mpxMediaID"),
                service=self.__class__,
                name=metadata.get("title") or metadata.get("cdpTitle"),
                year=metadata.get("releaseYear"),
                language=metadata.get("localeID") or "en",
                data=metadata,
            )
        ]

    def _episode(self, entity_id: str) -> Episode:
        metadata = self._request("GET", f"https://catalogue.pr.sbsod.com/mpx-media/{entity_id}")

        return [
            Episode(
                id_=metadata.get("mpxMediaID"),
                service=self.__class__,
                title=metadata.get("seriesTitle"),
                season=int(metadata.get("seasonNumber", 0)),
                number=int(metadata.get("episodeNumber", 0)),
                name=metadata.get("title") or metadata.get("cdpTitle"),
                year=metadata.get("releaseYear"),
                language=metadata.get("localeID") or "en",
                data=metadata,
            )
        ]

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

        except ValueError as e:
            raise ValueError(f"Failed to parse JSON: {response.text}") from e

