from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from typing import Any, Optional

import click
from pywidevine.cdm import Cdm as WidevineCdm

from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Subtitle, Video, Audio, Tracks


class CTV(Service):
    """
    Service code for CTV.ca (https://www.ctv.ca)

    \b
    Version: 1.0.0
    Author: stabbedbybrick
    Authorization: Credentials for subscription, none for freely available titles
    Robustness:
      Widevine:
        L3: 1080p, DD5.1

    \b
    Tips:
        - Input can be either complete title/episode URL or just the path:
            /shows/young-sheldon
            /shows/young-sheldon/baptists-catholics-and-an-attempted-drowning-s7e6
            /movies/war-for-the-planet-of-the-apes
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?ctv\.ca(?:/[a-z]{2})?)?/(?P<type>movies|shows)/(?P<id>[a-z0-9-]+)(?:/(?P<episode>[a-z0-9-]+))?$"
    GEOFENCE = ("ca",)

    @staticmethod
    @click.command(name="CTV", short_help="https://www.ctv.ca", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return CTV(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)

        self.authorization: str = None

        self.api = self.config["endpoints"]["api"]
        self.license_url = self.config["endpoints"]["license"]

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if credential:
            cache = self.cache.get(f"tokens_{credential.sha1}")

            if cache and not cache.expired:
                # cached
                self.log.info(" + Using cached Tokens...")
                tokens = cache.data
            elif cache and cache.expired:
                # expired, refresh
                self.log.info("Refreshing cached Tokens")
                r = self.session.post(
                    self.config["endpoints"]["login"],
                    headers={"authorization": f"Basic {self.config['endpoints']['auth']}"},
                    data={
                        "grant_type": "refresh_token",
                        "username": credential.username,
                        "password": credential.password,
                        "refresh_token": cache.data["refresh_token"],
                    },
                )
                try:
                    res = r.json()
                except json.JSONDecodeError:
                    raise ValueError(f"Failed to refresh tokens: {r.text}")

                tokens = res
                self.log.info(" + Refreshed")
            else:
                # new
                r = self.session.post(
                    self.config["endpoints"]["login"],
                    headers={"authorization": f"Basic {self.config['endpoints']['auth']}"},
                    data={
                        "grant_type": "password",
                        "username": credential.username,
                        "password": credential.password,
                    },
                )
                try:
                    res = r.json()
                except json.JSONDecodeError:
                    raise ValueError(f"Failed to log in: {r.text}")

                tokens = res
                self.log.info(" + Acquired tokens...")

            cache.set(tokens, expiration=tokens["expires_in"])

            self.authorization = f"Bearer {tokens['access_token']}"

    def search(self) -> Generator[SearchResult, None, None]:
        payload = {
            "operationName": "searchMedia",
            "variables": {"title": f"{self.title}"},
            "query": """
                        query searchMedia($title: String!) {searchMedia(titleMatches: $title) {
                        ... on Medias {page {items {title\npath}}}}}, """,
        }

        r = self.session.post(self.config["endpoints"]["search"], json=payload)
        if r.status_code != 200:
            self.log.error(r.text)
            return

        for result in r.json()["data"]["searchMedia"]["page"]["items"]:
            yield SearchResult(
                id_=result.get("path"),
                title=result.get("title"),
                description=result.get("description"),
                label=result["path"].split("/")[1],
                url="https://www.ctv.ca" + result.get("path"),
            )

    def get_titles(self) -> Titles_T:
        title, kind, episode = (re.match(self.TITLE_RE, self.title).group(i) for i in ("id", "type", "episode"))
        title_path = self.get_title_id(kind, title, episode)

        if episode is not None:
            data = self.get_episode_data(title_path)
            return Series(
                [
                    Episode(
                        id_=data["axisId"],
                        service=self.__class__,
                        title=data["axisMedia"]["title"],
                        season=int(data["seasonNumber"]),
                        number=int(data["episodeNumber"]),
                        name=data["title"],
                        year=data.get("firstAirYear"),
                        language=data["axisPlaybackLanguages"][0].get("language", "en"),
                        data=data["axisPlaybackLanguages"][0]["destinationCode"],
                    )
                ]
            )

        if kind == "shows":
            data = self.get_series_data(title_path)
            titles = self.fetch_episodes(data["contentData"]["seasons"])
            return Series(
                [
                    Episode(
                        id_=episode["axisId"],
                        service=self.__class__,
                        title=data["contentData"]["title"],
                        season=int(episode["seasonNumber"]),
                        number=int(episode["episodeNumber"]),
                        name=episode["title"],
                        year=data["contentData"]["firstAirYear"],
                        language=episode["axisPlaybackLanguages"][0].get("language", "en"),
                        data=episode["axisPlaybackLanguages"][0]["destinationCode"],
                    )
                    for episode in titles
                ]
            )

        if kind == "movies":
            data = self.get_movie_data(title_path)
            return Movies(
                [
                    Movie(
                        id_=data["contentData"]["firstPlayableContent"]["axisId"],
                        service=self.__class__,
                        name=data["contentData"]["title"],
                        year=data["contentData"]["firstAirYear"],
                        language=data["contentData"]["firstPlayableContent"]["axisPlaybackLanguages"][0].get(
                            "language", "en"
                        ),
                        data=data["contentData"]["firstPlayableContent"]["axisPlaybackLanguages"][0]["destinationCode"],
                    )
                ]
            )

    def get_tracks(self, title: Title_T) -> Tracks:
        content = "https://capi.9c9media.com/destinations/{}/platforms/desktop/contents/{}/contentPackages".format(
            title.data, title.id
        )

        params = {
            "$include": "[Desc,Constraints,EndCreditOffset,Breaks,Stacks.ManifestHost.mpd]",
        }
        r = self.session.get(content, params=params)
        r.raise_for_status()

        pkg_id = r.json()["Items"][0]["Id"]
        manifest = f"{content}/{pkg_id}/manifest.mpd"
        subtitle = f"{content}/{pkg_id}/manifest.vtt"

        if self.authorization:
            self.session.headers.update({"authorization": self.authorization})

        tracks = Tracks()
        for num in ["14", "3", "25", "fe&mca=true&mta=true"]:
            version = DASH.from_url(url=f"{manifest}?filter={num}", session=self.session).to_tracks(language=title.language)
            tracks.videos.extend(version.videos)
            tracks.audio.extend(version.audio)
            
        tracks.add(
            Subtitle(
                id_=hashlib.md5(subtitle.encode()).hexdigest()[0:6],
                url=subtitle,
                codec=Subtitle.Codec.from_mime(subtitle[-3:]),
                language=title.language,
                is_original_lang=True,
                forced=False,
                sdh=True,
            )
        )
        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []  # Chapters not available

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return WidevineCdm.common_privacy_cert

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        r = self.session.post(url=self.license_url, data=challenge)
        if r.status_code != 200:
            self.log.error(r.text)
            sys.exit(1)
        return r.content

    # service specific functions

    def get_title_id(self, kind: str, title: tuple, episode: str) -> str:
        if episode is not None:
            title += f"/{episode}"
        payload = {
            "operationName": "resolvePath",
            "variables": {"path": f"{kind}/{title}"},
            "query": """
            query resolvePath($path: String!) {
                resolvedPath(path: $path) {
                    lastSegment {
                        content {
                            id
                        }
                    }
                }
            }
            """,
        }
        r = self.session.post(self.api, json=payload).json()
        return r["data"]["resolvedPath"]["lastSegment"]["content"]["id"]

    def get_series_data(self, title_id: str) -> json:
        payload = {
            "operationName": "axisMedia",
            "variables": {"axisMediaId": f"{title_id}"},
            "query": """
                query axisMedia($axisMediaId: ID!) {
                    contentData: axisMedia(id: $axisMediaId) {
                        title
                        description
                        originalSpokenLanguage
                        mediaType
                        firstAirYear
                        seasons {
                            title
                            id
                            seasonNumber
                        }
                    }
                }
                """,
        }

        return self.session.post(self.api, json=payload).json()["data"]

    def get_movie_data(self, title_id: str) -> json:
        payload = {
            "operationName": "axisMedia",
            "variables": {"axisMediaId": f"{title_id}"},
            "query": """
                query axisMedia($axisMediaId: ID!) {
                    contentData: axisMedia(id: $axisMediaId) {
                        title
                        description
                        firstAirYear
                        firstPlayableContent {
                            axisId
                            axisPlaybackLanguages {
                                destinationCode
                            }
                        }
                    }
                }
                """,
        }

        return self.session.post(self.api, json=payload).json()["data"]

    def get_episode_data(self, title_path: str) -> json:
        payload = {
            "operationName": "axisContent",
            "variables": {"id": f"{title_path}"},
            "query": """
                    query axisContent($id: ID!) {
                        axisContent(id: $id) {
                            axisId
                            title
                            description
                            contentType
                            seasonNumber
                            episodeNumber
                            axisMedia {
                                title
                            }
                            axisPlaybackLanguages {
                                    language
                                    destinationCode
                            }
                        }
                    }
                    """,
        }
        return self.session.post(self.api, json=payload).json()["data"]["axisContent"]

    def fetch_episode(self, episode: str) -> json:
        payload = {
            "operationName": "season",
            "variables": {"seasonId": f"{episode}"},
            "query": """
                query season($seasonId: ID!) {
                    axisSeason(id: $seasonId) {
                        episodes {
                            axisId
                            title
                            description
                            contentType
                            seasonNumber
                            episodeNumber
                            axisPlaybackLanguages {
                                language
                                destinationCode
                            }
                        }
                    }
                }
                """,
        }
        response = self.session.post(self.api, json=payload)
        return response.json()["data"]["axisSeason"]["episodes"]

    def fetch_episodes(self, data: dict) -> list:
        """TODO: Switch to async once https proxies are fully supported"""
        with ThreadPoolExecutor(max_workers=10) as executor:
            tasks = [executor.submit(self.fetch_episode, x["id"]) for x in data]
            titles = [future.result() for future in as_completed(tasks)]
        return [episode for episodes in titles for episode in episodes]
