import json
import re
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import click
import requests

from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Tracks


class ROKU(Service):
    """
    Service code for The Roku Channel (https://therokuchannel.roku.com)

    \b
    Version: 1.0.1
    Author: stabbedbybrick
    Authorization: Cookies
    Robustness:
      Widevine:
        L3: 1080p, DD5.1

    \b
    Tips:
        - Use complete title/episode URL or id as input:
            https://therokuchannel.roku.com/details/e05fc677ab9c5d5e8332f123770697b9/paddington
            OR
            e05fc677ab9c5d5e8332f123770697b9
        - Supports movies, series, and single episodes
        - Search is geofenced
    """

    GEOFENCE = ("us",)
    TITLE_RE = r"^(?:https?://(?:www.)?therokuchannel.roku.com/(?:details|watch)/)?(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="ROKU", short_help="https://therokuchannel.roku.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return ROKU(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = re.match(self.TITLE_RE, title).group("id")
        super().__init__(ctx)

        self.license: str

    def authenticate(
        self,
        cookies: Optional[CookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if cookies is not None:
            self.session.cookies.update(cookies)

    def search(self) -> Generator[SearchResult, None, None]:
        token = self.session.get(self.config["endpoints"]["token"]).json()["csrf"]

        headers = {"csrf-token": token}
        payload = {"query": self.title}

        r = self.session.post(self.config["endpoints"]["search"], headers=headers, json=payload)
        r.raise_for_status()

        results = r.json()
        for result in results["view"]:
            if result["content"]["type"] not in ["zone", "provider"]:
                _id = result["content"].get("meta", {}).get("id")
                _desc = result["content"].get("descriptions", {})

                label = f'{result["content"].get("type")} ({result["content"].get("releaseYear")})'
                if result["content"].get("viewOptions"):
                    label += f' ({result["content"]["viewOptions"][0].get("priceDisplay")})'

                title = re.sub(r"^-|-$", "", re.sub(r"\W+", "-", result["content"].get("title").lower()))

                yield SearchResult(
                    id_=_id,
                    title=title,
                    description=_desc["250"]["text"] if _desc.get("250") else None,
                    label=label,
                    url=f"https://therokuchannel.roku.com/details/{_id}/{title}",
                )

    def get_titles(self) -> Titles_T:
        data = self.session.get(self.config["endpoints"]["content"] + self.title).json()
        if not data["isAvailable"]:
            self.log.error("This title is temporarily unavailable or expired")
            sys.exit(1)

        if data["type"] in ["movie", "tvspecial"]:
            return Movies(
                [
                    Movie(
                        id_=data["meta"]["id"],
                        service=self.__class__,
                        name=data["title"],
                        year=data["releaseYear"],
                        language=data["viewOptions"][0]["media"].get("originalAudioLanguage", "en"),
                        data=data,
                    )
                ]
            )

        elif data["type"] == "series":
            episodes = self.fetch_episodes(data)
            return Series(
                [
                    Episode(
                        id_=episode["meta"]["id"],
                        service=self.__class__,
                        title=data["title"],
                        season=int(episode["seasonNumber"]),
                        number=int(episode["episodeNumber"]),
                        name=episode["title"],
                        year=data["releaseYear"],
                        language=episode["viewOptions"][0]["media"].get("originalAudioLanguage", "en"),
                        data=data,
                    )
                    for episode in episodes
                ]
            )

        elif data["type"] == "episode":
            return Series(
                [
                    Episode(
                        id_=data["meta"]["id"],
                        service=self.__class__,
                        title=data["title"],
                        season=int(data["seasonNumber"]),
                        number=int(data["episodeNumber"]),
                        name=data["title"],
                        year=data["releaseYear"],
                        language=data["viewOptions"][0]["media"].get("originalAudioLanguage", "en"),
                        data=data,
                    )
                ]
            )

    def get_tracks(self, title: Title_T) -> Tracks:
        token = self.session.get(self.config["endpoints"]["token"]).json()["csrf"]

        options = title.data["viewOptions"]
        subscription = options[0].get("license", "").lower()
        authenticated = next((x for x in options if x.get("isAuthenticated")), None)

        if subscription == "subscription" and not authenticated:
            self.log.error("This title is only available to subscribers")
            sys.exit(1)

        play_id = authenticated.get("playId") if authenticated else options[0].get("playId")
        provider_id = authenticated.get("providerId") if authenticated else options[0].get("providerId")

        headers = {
            "csrf-token": token,
        }
        payload = {
            "rokuId": title.id,
            "playId": play_id,
            "mediaFormat": "mpeg-dash",
            "drmType": "widevine",
            "quality": "fhd",
            "providerId": provider_id,
        }

        r = self.session.post(
            self.config["endpoints"]["vod"],
            headers=headers,
            json=payload,
        )
        r.raise_for_status()

        videos = r.json()["playbackMedia"]["videos"]
        self.license = next(
            (
                x["drmParams"]["licenseServerURL"]
                for x in videos
                if x.get("drmParams") and x["drmParams"]["keySystem"] == "Widevine"
            ),
            None,
        )

        url = next((x["url"] for x in videos if x["streamFormat"] == "dash"), None)
        if url and "origin" in urlparse(url).query:
            url = unquote(urlparse(url).query.split("=")[1]).split("?")[0]

        tracks = DASH.from_url(url=url).to_tracks(language=title.language)
        tracks.videos[0].data["playbackMedia"] = r.json()["playbackMedia"]

        for track in tracks.audio:
            label = track.data["dash"]["adaptation_set"].find("Label")
            if label is not None and "description" in label.text:
                track.descriptive = True

        for track in tracks.subtitles:
            label = track.data["dash"]["adaptation_set"].find("Label")
            if label is not None and "caption" in label.text:
                track.cc = True

        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        track = title.tracks.videos[0]

        chapters = []
        if track.data.get("playbackMedia", {}).get("adBreaks"):
            timestamps = sorted(track.data["playbackMedia"]["adBreaks"])
            chapters = [Chapter(name=f"Chapter {i + 1:02}", timestamp=ad.split(".")[0]) for i, ad in enumerate(timestamps)]

        if track.data.get("playbackMedia", {}).get("creditCuePoints"):
            start = next((
                x.get("start") for x in track.data["playbackMedia"]["creditCuePoints"] if x.get("start") != 0), None)
            if start:
                chapters.append(
                    Chapter(
                        name="Credits",
                        timestamp=datetime.fromtimestamp((start / 1000), tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                    )
                )

        return chapters

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return  # WidevineCdm.common_privacy_cert

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        r = self.session.post(url=self.license, data=challenge)
        if r.status_code != 200:
            self.log.error(r.text)
            sys.exit(1)
        return r.content

    # service specific functions

    def fetch_episode(self, episode: dict) -> json:
        try:
            r = self.session.get(self.config["endpoints"]["content"] + episode["meta"]["id"])
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            self.log.error(f"An error occurred while fetching episode {episode['meta']['id']}: {e}")
            return None

    def fetch_episodes(self, data: dict) -> list:
        """TODO: Switch to async once https proxies are fully supported"""
        with ThreadPoolExecutor(max_workers=10) as executor:
            tasks = list(executor.map(self.fetch_episode, data["episodes"]))
        return [task for task in tasks if task is not None]
