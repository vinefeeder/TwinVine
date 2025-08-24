from __future__ import annotations

import base64
import json
import re
from collections.abc import Generator
from typing import Any, Optional, Union
from urllib.parse import urljoin

import click
from envied.core.constants import AnyTrack
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Tracks
from envied.core.utils.xml import load_xml
from requests import Request


class RTE(Service):
    """
    \b
    Service code for RTE Player streaming service (https://www.rte.ie/player/).

    \b
    Version: 1.0.0
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      Widevine:
        L3: 1080p, AAC2.0

    \b
    Tips:
        - Input (pay attention to the URL format):
          SERIES: https://www.rte.ie/player/series/crossfire/10003928-00-0000
          EPISODE: https://www.rte.ie/player/series/crossfire/10003928-00-0000?epguid=AQ10003929-01-0001
          MOVIE: https://www.rte.ie/player/movie/glass/360230440380

    \b
    Notes:
        - Since some content is accessible worldwide, geofence is deactivated.
        - Using an IE IP-address is recommended to access everything.

    """

    # GEOFENCE = ("ie",)

    @staticmethod
    @click.command(name="RTE", short_help="https://www.rte.ie/player/", help=__doc__)
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs) -> RTE:
        return RTE(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)

        self.base_url = self.config["endpoints"]["base_url"]
        self.feed = self.config["endpoints"]["feed"]
        self.license = self.config["endpoints"]["license"]

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "byProgramType": "Series|Movie",
            "q": f"title:({self.title})",
            "range": "0-40",
            "schema": "2.15",
            "sort": "rte$rank|desc",
            "gzip": "true",
            "omitInvalidFields": "true",
        }
        results = self._request(f"{self.feed}/f/1uC-gC/rte-prd-prd-search", params=params)["entries"]

        for result in results:
            link = "https://www.rte.ie/player/{}/{}/{}"
            series = result.get("plprogram$programType").lower() == "series"
            _id = result.get("guid") if series else result.get("id").split("/")[-1]
            _title = result.get("title") if series else result.get("plprogram$longTitle")
            _type = result.get("plprogram$programType")

            title = _title.format(_type, _title, _id).lower()
            title = re.sub(r"\W+", "-", title)
            title = re.sub(r"^-|-$", "", title)

            yield SearchResult(
                id_=link.format(_type, title, _id),
                title=_title,
                description=result.get("plprogram$shortDescription"),
                label=_type,
                url=link.format(_type, title, _id),
            )

    def get_titles(self) -> Titles_T:
        title_re = (
            r"https://www\.rte\.ie/player"
            r"/(?P<type>series|movie)"
            r"/(?P<slug>[a-zA-Z0-9_-]+)"
            r"/(?P<id>[a-zA-Z0-9_\-=?]+)/?$"
        )
        try:
            kind, _, title_id = (re.match(title_re, self.title).group(i) for i in ("type", "slug", "id"))
        except Exception:
            raise ValueError("- Could not parse ID from input")

        episode = title_id.split("=")[1] if "epguid" in title_id else None

        if episode:
            episode = self._episode(title_id, episode)
            return Series(episode)

        elif kind == "movie":
            movie = self._movie(title_id)
            return Movies(movie)

        elif kind == "series":
            episodes = self._show(title_id)
            return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        self.token, self.account = self.get_config()
        media = title.data["plprogramavailability$media"][0].get("plmedia$publicUrl")
        if not media:
            raise ValueError("Could not find any streams - is the title still available?")

        manifest, self.pid = self.get_manifest(media)
        tracks = DASH.from_url(manifest, self.session).to_tracks(language=title.language)
        for track in tracks.audio:
            role = track.data["dash"]["adaptation_set"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Episode) -> Chapters:
        if not title.data.get("rte$chapters"):
            return Chapters()

        timecodes = [x for x in title.data["rte$chapters"]]
        chapters = [Chapter(timestamp=float(x)) for x in timecodes]

        if title.data.get("rte$creditStart"):
            chapters.append(Chapter(name="Credits", timestamp=float(title.data["rte$creditStart"])))

        return chapters

    def certificate(self, **_):
        return None  # will use common privacy cert

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        params = {
            "token": self.token,
            "account": self.account,
            "form": "json",
            "schema": "1.0",
        }
        payload = {
            "getWidevineLicense": {
                "releasePid": self.pid,
                "widevineChallenge": base64.b64encode(challenge).decode("utf-8"),
            }
        }
        r = self.session.post(url=self.license, params=params, json=payload)
        if not r.ok:
            raise ConnectionError(f"License request failed: {r.text}")

        return r.json()["getWidevineLicenseResponse"]["license"]

    # Service specific functions

    def _movie(self, title: str) -> Movie:
        params = {"count": "true", "entries": "true", "byId": title}
        data = self._request("/mpx/1uC-gC/rte-prd-prd-all-programs", params=params)["entries"]

        return [
            Movie(
                id_=movie["guid"],
                service=self.__class__,
                name=movie.get("plprogram$longTitle"),
                year=movie.get("plprogram$year"),
                language=movie["plprogram$languages"][0] if movie.get("plprogram$languages") else "eng",
                data=movie,
            )
            for movie in data
        ]

    def _show(self, title: str) -> Episode:
        entry = self._request("/mpx/1uC-gC/rte-prd-prd-all-movies-series?byGuid={}".format(title))["entries"][0]["id"]
        data = self._request("/mpx/1uC-gC/rte-prd-prd-all-programs?bySeriesId={}".format(entry.split("/")[-1]))["entries"]

        return [
            Episode(
                id_=episode.get("guid"),
                title=episode.get("plprogram$longTitle"),
                season=episode.get("plprogram$tvSeasonNumber") or 0,
                number=episode.get("plprogram$tvSeasonEpisodeNumber") or 0,
                name=episode.get("description"),
                language=episode["plprogram$languages"][0] if episode.get("plprogram$languages") else "eng",
                service=self.__class__,
                data=episode,
            )
            for episode in data
            if episode["plprogram$programType"] == "episode"
        ]

    def _episode(self, title: str, guid: str) -> Episode:
        title = title.split("?")[0]
        entry = self._request("/mpx/1uC-gC/rte-prd-prd-all-movies-series?byGuid={}".format(title))["entries"][0]["id"]
        data = self._request("/mpx/1uC-gC/rte-prd-prd-all-programs?bySeriesId={}".format(entry.split("/")[-1]))["entries"]

        return [
            Episode(
                id_=episode.get("guid"),
                title=episode.get("plprogram$longTitle"),
                season=episode.get("plprogram$tvSeasonNumber") or 0,
                number=episode.get("plprogram$tvSeasonEpisodeNumber") or 0,
                name=episode.get("description"),
                language=episode["plprogram$languages"][0] if episode.get("plprogram$languages") else "eng",
                service=self.__class__,
                data=episode,
            )
            for episode in data
            if episode["plprogram$programType"] == "episode" and episode.get("guid") == guid
        ]

    def get_config(self):
        token = self._request("/servicelayer/api/anonymouslogin")["mpx_token"]
        account = self._request("/wordpress/wp-content/uploads/standard/web/config.json")["mpx_config"]["account_id"]
        return token, account

    def get_manifest(self, media_url: str) -> str:
        try:
            res = self._request(
                media_url,
                params={
                    "formats": "MPEG-DASH",
                    "auth": self.token,
                    "assetTypes": "default:isl",
                    "tracking": "true",
                    "format": "SMIL",
                    "iu": "/3014/RTE_Player_VOD/Android_Phone/NotRegistered",
                    "policy": "168602703",
                },
            )

            root = load_xml(res)
            video = root.xpath("//switch/video")
            manifest = video[0].get("src")

            elem = root.xpath("//switch/ref")
            value = elem[0].find(".//param[@name='trackingData']").get("value")
            pid = re.search(r"pid=([^|]+)", value).group(1)

            return manifest, pid

        except Exception as e:
            raise ValueError(
                f"Request for manifest failed: {e}.\n"
                "Content may be geo-restricted to IE"
            )

    def _request(self, api: str, params: dict = None, headers: dict = None) -> Any[dict | str]:
        url = urljoin(self.base_url, api)
        self.session.headers.update(self.config["headers"])

        if params:
            self.session.params.update(params)
        if headers:
            self.session.headers.update(headers)

        prep = self.session.prepare_request(Request("GET", url))

        response = self.session.send(prep)
        if response.status_code != 200:
            raise ConnectionError(
                f"Status: {response.status_code} - {response.url}\n"
                "Content may be geo-restricted to IE"
            )

        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            return response.text
