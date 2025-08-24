from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Generator
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union

import click
from bs4 import BeautifulSoup
from click import Context
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Subtitle, Tracks


class ITV(Service):
    """
    Service code for ITVx streaming service (https://www.itv.com/).

    \b
    Version: 1.0.1
    Author: stabbedbybrick
    Authorization: Cookies (Optional for free content | Required for premium content)
    Robustness:
      L3: 1080p

    \b
    Tips:
        - Use complete title URL as input (pay attention to the URL format):
            SERIES: https://www.itv.com/watch/bay-of-fires/10a5270
            EPISODE: https://www.itv.com/watch/bay-of-fires/10a5270/10a5270a0001
            FILM: https://www.itv.com/watch/mad-max-beyond-thunderdome/2a7095
        - Some shows aren't listed as series, only as "Latest episodes"
            Download by SERIES URL for those titles, not by EPISODE URL

    \b
    Examples:
        - SERIES: envied dl -w s01e01 itv https://www.itv.com/watch/bay-of-fires/10a5270
        - EPISODE: envied dl itv https://www.itv.com/watch/bay-of-fires/10a5270/10a5270a0001
        - FILM: envied dl itv https://www.itv.com/watch/mad-max-beyond-thunderdome/2a7095

    \b
    Notes:
        ITV seem to detect and throttle multiple connections against the server.
        It's recommended to use requests as downloader, with few workers.

    """

    GEOFENCE = ("gb",)
    ALIASES = ("itvx",)

    @staticmethod
    @click.command(name="ITV", short_help="https://www.itv.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> ITV:
        return ITV(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.profile = ctx.parent.params.get("profile")
        if not self.profile:
            self.profile = "default"

        self.session.headers.update(self.config["headers"])

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.authorization = None

        if credential and not cookies:
            self.log.error(" - Error: This service requires cookies for authentication.")
            sys.exit(1)

        if cookies is not None:
            self.log.info(f"\n + Cookies for '{self.profile}' profile found, authenticating...")
            itv_session = next((cookie.value for cookie in cookies if cookie.name == "Itv.Session"), None)
            if not itv_session:
                self.log.error(" - Error: Session cookie not found. Cookies may be invalid.")
                sys.exit(1)

            itv_session = json.loads(itv_session)
            refresh_token = itv_session["tokens"]["content"].get("refresh_token")
            if not refresh_token:
                self.log.error(" - Error: Access tokens not found. Try refreshing your cookies.")
                sys.exit(1)

            cache = self.cache.get(f"tokens_{self.profile}")

            headers = {
                "Host": "auth.prd.user.itv.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
                "Accept": "application/vnd.user.auth.v2+json",
                "Accept-Language": "en-US,en;q=0.8",
                "Origin": "https://www.itv.com",
                "Connection": "keep-alive",
                "Referer": "https://www.itv.com/",
            }

            params = {"refresh": cache.data["refresh_token"]} if cache else {"refresh": refresh_token}

            r = self.session.get(
                self.config["endpoints"]["refresh"],
                headers=headers,
                params=params,
            )
            if r.status_code != 200:
                raise ConnectionError(f"Failed to refresh tokens: {r.text}")

            tokens = r.json()
            cache.set(tokens)
            self.log.info(" + Tokens refreshed and placed in cache\n")

            self.authorization = tokens["access_token"]

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "broadcaster": "itv",
            "featureSet": "clearkey,outband-webvtt,hls,aes,playready,widevine,fairplay,bbts,progressive,hd,rtmpe",
            "onlyFree": "false",
            "platform": "dotcom",
            "query": self.title,
        }

        r = self.session.get(self.config["endpoints"]["search"], params=params)
        r.raise_for_status()

        results = r.json()["results"]
        if isinstance(results, list):
            for result in results:
                special = result["data"].get("specialTitle")
                standard = result["data"].get("programmeTitle")
                film = result["data"].get("filmTitle")
                title = special if special else standard if standard else film
                tier = result["data"].get("tier")

                slug = self._sanitize(title)

                _id = result["data"]["legacyId"]["apiEncoded"]
                _id = "_".join(_id.split("_")[:2]).replace("_", "a")
                _id = re.sub(r"a000\d+", "", _id)

                yield SearchResult(
                    id_=f"https://www.itv.com/watch/{slug}/{_id}",
                    title=title,
                    description=result["data"].get("synopsis"),
                    label=result.get("entityType") + f" {tier}",
                    url=f"https://www.itv.com/watch/{slug}/{_id}",
                )

    def get_titles(self) -> Union[Movies, Series]:
        data = self.get_data(self.title)
        kind = next(
            (x.get("seriesType") for x in data.get("seriesList") if x.get("seriesType") in ["SERIES", "FILM"]), None
        )

        # Some shows are not listed as "SERIES" or "FILM", only as "Latest episodes"
        if not kind and next(
            (x for x in data.get("seriesList") if x.get("seriesLabel").lower() in ("latest episodes", "other episodes")), None
        ):
            titles = data["seriesList"][0]["titles"]
            episodes =[
                    Episode(
                        id_=episode["episodeId"],
                        service=self.__class__,
                        title=data["programme"]["title"],
                        season=episode.get("series") if isinstance(episode.get("series"), int) else 0,
                        number=episode.get("episode") if isinstance(episode.get("episode"), int) else 0,
                        name=episode["episodeTitle"],
                        language="en",  # TODO: language detection
                        data=episode,
                    )
                    for episode in titles
                ]
            # Assign episode numbers to special seasons
            counter = 1
            for episode in episodes:
                if episode.season == 0 and episode.number == 0:
                    episode.number = counter
                    counter += 1
            return Series(episodes)

        if kind == "SERIES" and data.get("episode"):
            episode = data.get("episode")
            return Series(
                [
                    Episode(
                        id_=episode["episodeId"],
                        service=self.__class__,
                        title=data["programme"]["title"],
                        season=episode.get("series") if isinstance(episode.get("series"), int) else 0,
                        number=episode.get("episode") if isinstance(episode.get("episode"), int) else 0,
                        name=episode["episodeTitle"],
                        language="en",  # TODO: language detection
                        data=episode,
                    )
                ]
            )

        elif kind == "SERIES":
            return Series(
                [
                    Episode(
                        id_=episode["episodeId"],
                        service=self.__class__,
                        title=data["programme"]["title"],
                        season=episode.get("series") if isinstance(episode.get("series"), int) else 0,
                        number=episode.get("episode") if isinstance(episode.get("episode"), int) else 0,
                        name=episode["episodeTitle"],
                        language="en",  # TODO: language detection
                        data=episode,
                    )
                    for series in data["seriesList"]
                    if "Latest episodes" not in series["seriesLabel"]
                    for episode in series["titles"]
                ]
            )

        elif kind == "FILM":
            return Movies(
                [
                    Movie(
                        id_=movie["episodeId"],
                        service=self.__class__,
                        name=data["programme"]["title"],
                        year=movie.get("productionYear"),
                        language="en",  # TODO: language detection
                        data=movie,
                    )
                    for movies in data["seriesList"]
                    for movie in movies["titles"]
                ]
            )

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        playlist = title.data.get("playlistUrl")

        headers = {
            "Accept": "application/vnd.itv.vod.playlist.v4+json",
            "Accept-Language": "en-US,en;q=0.9,da;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        }

        payload = {
            "client": {
                "id": "lg",
            },
            "device": {
                "deviceGroup": "ctv",
            },
            "variantAvailability": {
                "player": "dash",
                "featureset": [
                    "mpeg-dash",
                    "widevine",
                    "outband-webvtt",
                    "hd",
                    "single-track",
                ],
                "platformTag": "ctv",
                "drm": {
                    "system": "widevine",
                    "maxSupported": "L3",
                },
            },
        }
        if self.authorization:
            payload["user"] = {"token": self.authorization}

        r = self.session.post(playlist, headers=headers, json=payload)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        data = r.json()
        video = data["Playlist"]["Video"]
        subtitles = video.get("Subtitles")
        self.manifest = video["MediaFiles"][0].get("Href")
        self.license = video["MediaFiles"][0].get("KeyServiceUrl")

        tracks = DASH.from_url(self.manifest, self.session).to_tracks(title.language)
        tracks.videos[0].data = data

        if subtitles is not None:
            for subtitle in subtitles:
                tracks.add(
                    Subtitle(
                        id_=hashlib.md5(subtitle.get("Href", "").encode()).hexdigest()[0:6],
                        url=subtitle.get("Href", ""),
                        codec=Subtitle.Codec.from_mime(subtitle.get("Href", "")[-3:]),
                        language=title.language,
                        forced=False,
                    )
                )

        '''for track in tracks.audio:
            role = track.data["dash"]["representation"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True'''

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        track = title.tracks.videos[0]
        if not track.data["Playlist"].get("ContentBreaks"):
            return Chapters()

        breaks = track.data["Playlist"]["ContentBreaks"]
        timecodes = [".".join(x.get("TimeCode").rsplit(":", 1)) for x in breaks if x.get("TimeCode") != "00:00:00:000"]

        # End credits are sometimes listed before the last chapter, so we skip those for now
        return Chapters([Chapter(timecode) for timecode in timecodes])

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        r = self.session.post(url=self.license, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.content

    # Service specific functions

    def get_data(self, url: str) -> dict:
        # TODO: Find a proper endpoint for this

        r = self.session.get(url)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        soup = BeautifulSoup(r.text, "html.parser")
        props = soup.select_one("#__NEXT_DATA__").text

        try:
            data = json.loads(props)
        except Exception as e:
            raise ValueError(f"Failed to parse JSON: {e}")

        return data["props"]["pageProps"]

    @staticmethod
    def _sanitize(title: str) -> str:
        title = title.lower()
        title = title.replace("&", "and")
        title = re.sub(r"[:;/()]", "", title)
        title = re.sub(r"[ ]", "-", title)
        title = re.sub(r"[\\*!?¿,'\"<>|$#`’]", "", title)
        title = re.sub(rf"[{'.'}]{{2,}}", ".", title)
        title = re.sub(rf"[{'_'}]{{2,}}", "_", title)
        title = re.sub(rf"[{'-'}]{{2,}}", "-", title)
        title = re.sub(rf"[{' '}]{{2,}}", " ", title)
        return title
