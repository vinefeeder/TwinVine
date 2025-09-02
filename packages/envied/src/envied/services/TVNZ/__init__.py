from __future__ import annotations

import json
from collections.abc import Generator
from datetime import timedelta
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union
from urllib.parse import urljoin, urlparse

import click
from click import Context
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapters, Tracks
from lxml import etree
from pywidevine.cdm import Cdm as WidevineCdm
from requests import Request


class TVNZ(Service):
    """
    \b
    Service code for TVNZ streaming service (https://www.tvnz.co.nz).

    \b
    Version: 1.0.0
    Author: stabbedbybrick
    Authorization: Credentials
    Robustness:
      L3: 1080p, AAC2.0

    \b
    Tips:
        - Input can be comlete URL or path:
          SHOW: /shows/tulsa-king
          EPISODE: /shows/tulsa-king/episodes/s1-e1
          MOVIE: /shows/the-revenant
          SPORT: /sport/tennis/wta-tour/guadalajara-open-final

    """

    GEOFENCE = ("nz",)

    @staticmethod
    @click.command(name="TVNZ", short_help="https://www.tvnz.co.nz", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> TVNZ:
        return TVNZ(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update(self.config["headers"])

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "q": self.title.strip(),
            "includeTypes": "all",
        }

        results = self._request("GET", "/api/v1/android/play/search", params=params)["results"]

        for result in results:
            yield SearchResult(
                id_=result["page"].get("url"),
                title=result.get("title"),
                description=result.get("synopsis"),
                label=result.get("type"),
                url="https://www.tvnz.co.nz" + result["page"].get("url"),
            )

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        cache = self.cache.get(f"tokens_{credential.sha1}")

        if cache and not cache.expired:
            self.log.info(" + Using cached Tokens...")
            tokens = cache.data
        else:
            self.log.info(" + Logging in...")
            payload = {"email": credential.username, "password": credential.password, "keepMeLoggedIn": True}

            response = self.session.post(
                self.config["endpoints"]["base_api"] + "/api/v1/androidtv/consumer/login", json=payload
            )
            response.raise_for_status()
            if not response.headers.get("aat"):
                raise ValueError("Failed to authenticate: " + response.text)

            tokens = {
                "access_token": response.headers.get("aat"),
                "aft_token": response.headers.get("aft"),  # ?
            }

            cache.set(tokens, expiration=response.headers.get("aat_expires_in"))

        self.session.headers.update({"Authorization": "Bearer {}".format(tokens["access_token"])})

    def get_titles(self) -> Union[Movies, Series]:
        try:
            path = urlparse(self.title).path
        except Exception as e:
            raise ValueError("Could not parse ID from title: {}".format(e))

        page = self._request("GET", "/api/v4/androidtv/play/page/{}".format(path))

        if page["layout"].get("video"):
            title = page.get("title", "").replace("Episodes", "")
            video = self._request("GET", page["layout"]["video"].get("href"))
            episodes = self._episode(video, title)
            return Series(episodes)

        else:
            module = page["layout"]["slots"]["main"]["modules"][0]
            label = module.get("label", "")
            lists = module.get("lists")
            title = page.get("title", "").replace(label, "")

            seasons = [x.get("href") for x in lists]

            episodes = []
            for season in seasons:
                data = self._request("GET", season)
                episodes.extend([x for x in data["_embedded"].values()])

                while data.get("nextPage"):
                    data = self._request("GET", data["nextPage"])
                    episodes.extend([x for x in data["_embedded"].values()])

        if label in ("Episodes", "Stream"):
            episodes = self._show(episodes, title)
            return Series(episodes)

        elif label in ("Movie", "Movies"):
            movie = self._movie(episodes, title)
            return Movies(movie)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        metadata = title.data.get("publisherMetadata") or title.data.get("media")
        if not metadata:
            self.log.error("Unable to find metadata for this episode")
            return

        source = metadata.get("type") or metadata.get("source")
        video_id = metadata.get("brightcoveVideoId") or metadata.get("id")
        account_id = metadata.get("brightcoveAccountId") or metadata.get("accountId")
        playback = title.data.get("playbackHref", "")

        self.drm_token = None
        if source != "brightcove":
            data = self._request("GET", playback)
            self.license = (
                data["encryption"]["licenseServers"]["widevine"]
                if data["encryption"].get("drmEnabled")
                else None
            )
            self.drm_token = data["encryption"].get("drmToken")
            source_manifest = data["streaming"]["dash"].get("url")

        else:
            data = self._request(
                "GET", self.config["endpoints"]["brightcove"].format(account_id, video_id),
                headers={"BCOV-POLICY": self.config["policy"]},
            )

            self.license = next((
                x["key_systems"]["com.widevine.alpha"]["license_url"]
                for x in data["sources"]
                if x.get("key_systems").get("com.widevine.alpha")),
                None,
            )
            source_manifest = next((
                x["src"] for x in data["sources"]
                if x.get("key_systems").get("com.widevine.alpha")),
                None,
            )

        manifest = self.trim_duration(source_manifest)
        tracks = DASH.from_text(manifest, source_manifest).to_tracks(title.language)

        for track in tracks.audio:
            role = track.data["dash"]["representation"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return WidevineCdm.common_privacy_cert

    def get_widevine_license(self, challenge: bytes, **_: Any) -> str:
        if not self.license:
            return None

        headers = {"Authorization": f"Bearer {self.drm_token}"} if self.drm_token else self.session.headers
        r = self.session.post(self.license, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content

    # Service specific

    def _show(self, episodes: list, title: str) -> Episode:
        return [
            Episode(
                id_=episode.get("videoId"),
                service=self.__class__,
                title=title,
                season=int(episode.get("seasonNumber")) if episode.get("seasonNumber") else 0,
                number=int(episode.get("episodeNumber")) if episode.get("episodeNumber") else 0,
                name=episode.get("title"),
                language="en",
                data=episode,
            )
            for episode in episodes
        ]

    def _movie(self, movies: list, title: str) -> Movie:
        return [
            Movie(
                id_=movie.get("videoId"),
                service=self.__class__,
                name=title,
                year=None,
                language="en",
                data=movie,
            )
            for movie in movies
        ]

    def _episode(self, video: dict, title: str) -> Episode:
        kind = video.get("type")
        name = video.get("title")

        if kind == "sportVideo" and video.get("_embedded"):
            _type = next((x for x in video["_embedded"].values() if x.get("type") == "competition"), None)
            title = _type.get("title") if _type else title
            name = video.get("title", "") + " " + video.get("phase", "")

        return [
            Episode(
                id_=video.get("videoId"),
                service=self.__class__,
                title=title,
                season=int(video.get("seasonNumber")) if video.get("seasonNumber") else 0,
                number=int(video.get("episodeNumber")) if video.get("episodeNumber") else 0,
                name=name,
                language="en",
                data=video,
            )
        ]

    def _request(
        self,
        method: str,
        api: str,
        params: dict = None,
        headers: dict = None,
        payload: dict = None,
    ) -> Any[dict | str]:
        url = urljoin(self.config["endpoints"]["base_api"], api)
        if headers:
            self.session.headers.update(headers)

        prep = self.session.prepare_request(Request(method, url, params=params, json=payload))
        response = self.session.send(prep)

        try:
            data = json.loads(response.content)

            if data.get("message"):
                raise ConnectionError(f"{response.status_code} - {data.get('message')}")

            return data

        except Exception as e:
            raise ConnectionError("Request failed: {} - {}".format(response.status_code, response.text))

    def trim_duration(self, source_manifest: str) -> str:
        """
        The last segment on all tracks return a 404 for some reason, causing a failed download.
        So we trim the duration by exactly one segment to account for that.

        TODO: Calculate the segment duration instead of assuming length.
        """
        manifest = DASH.from_url(source_manifest, self.session).manifest
        period_duration = manifest.get("mediaPresentationDuration")
        period_duration = DASH.pt_to_sec(period_duration)

        hours, minutes, seconds = str(timedelta(seconds=period_duration - 6)).split(":")
        new_duration = f"PT{hours}H{minutes}M{seconds}S"
        manifest.set("mediaPresentationDuration", new_duration)

        return etree.tostring(manifest, encoding="unicode")
