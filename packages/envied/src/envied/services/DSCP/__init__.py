from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional, Union
from urllib.parse import urljoin

import click
from click import Context
from envied.core.credential import Credential
from envied.core.manifests import DASH, HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapters, Tracks
from requests import Request


class DSCP(Service):
    """
    \b
    Service code for Discovery Plus (https://discoveryplus.com).

    \b
    Version: 1.0.0
    Author: stabbedbybrick
    Authorization: Cookies
    Robustness:
        Widevine:
            L3: 2160p, AAC2.0
        ClearKey:
            AES-128: 1080p, AAC2.0

    \b
    Tips:
        - Input can be either complete title URL or just the path:
            SHOW: /show/richard-hammonds-workshop
            EPISODE: /video/richard-hammonds-workshop/new-beginnings
            SPORT: /video/sport/tnt-sports-1/uefa-champions-league
        - Use the --lang LANG_RANGE option to request non-english tracks
        - use -v H.265 to request H.265 UHD tracks (if available)

    \b
    Notes:
        - Using '-v H.265' will request DASH manifest even if no H.265 tracks are available.
          This can be useful if HLS is not available for some reason.

    """

    ALIASES = ("dplus", "discoveryplus", "discovery+")
    TITLE_RE = r"^(?:https?://(?:www\.)?discoveryplus\.com(?:/[a-z]{2})?)?/(?P<type>show|video)/(?P<id>[a-z0-9-/]+)"

    @staticmethod
    @click.command(name="DSCP", short_help="https://discoveryplus.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> DSCP:
        return DSCP(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        self.vcodec = ctx.parent.params.get("vcodec")
        super().__init__(ctx)

    def authenticate(
        self,
        cookies: Optional[CookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")

        self.session.cookies.update(cookies)

        self.base_url = None
        info = self._request("GET", "https://global-prod.disco-api.com/bootstrapInfo")
        self.base_url = info["data"]["attributes"].get("baseApiUrl")

        user = self._request("GET", "/users/me")
        self.territory = user["data"]["attributes"]["currentLocationTerritory"]
        self.user_language = user["data"]["attributes"]["clientTranslationLanguageTags"][0]
        self.site_id = user["meta"]["site"]["id"]

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "include": "default",
            "decorators": "viewingHistory,isFavorite,playbackAllowed,contentAction,badges",
            "contentFilter[query]": self.title,
            "page[items.number]": "1",
            "page[items.size]": "8",
        }
        data = self._request("GET", "/cms/routes/search/result", params=params)

        results = [x.get("attributes") for x in data["included"] if x.get("type") == "show"]

        for result in results:
            yield SearchResult(
                id_=f"/show/{result.get('alternateId')}",
                title=result.get("name"),
                description=result.get("description"),
                label="show",
                url=f"/show/{result.get('alternateId')}",
            )

    def get_titles(self) -> Union[Movies, Series]:
        try:
            kind, content_id = (re.match(self.TITLE_RE, self.title).group(i) for i in ("type", "id"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        if kind == "video":
            episodes = self._episode(content_id)

        if kind == "show":
            episodes = self._show(content_id)

        return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        payload = {
            "videoId": title.id,
            "deviceInfo": {
                "adBlocker": "false",
                "drmSupported": "false",
                "hwDecodingCapabilities": ["H264", "H265"],
                "screen": {"width": 3840, "height": 2160},
                "player": {"width": 3840, "height": 2160},
            },
            "wisteriaProperties": {
                "product": "dplus_emea",
                "sessionId": str(uuid.uuid1()),
            },
        }

        if self.vcodec == "H.265":
            payload["wisteriaProperties"]["device"] = {
                "browser": {"name": "chrome", "version": "96.0.4664.55"},
                "type": "firetv",
            }
            payload["wisteriaProperties"]["platform"] = "firetv"

        res = self._request("POST", "/playback/v3/videoPlaybackInfo", payload=payload)

        streaming = res["data"]["attributes"]["streaming"][0]
        streaming_type = streaming["type"].strip().lower()
        manifest = streaming["url"]

        self.token = None
        self.license = None
        if streaming["protection"]["drmEnabled"]:
            self.token = streaming["protection"]["drmToken"]
            self.license = streaming["protection"]["schemes"]["widevine"]["licenseUrl"]

        if streaming_type == "hls":
            tracks = HLS.from_url(url=manifest, session=self.session).to_tracks(language=title.language)

        elif streaming_type == "dash":
            tracks = DASH.from_url(url=manifest, session=self.session).to_tracks(language=title.language)

        else:
            raise ValueError(f"Unknown streaming type: {streaming_type}")

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> str:
        if not self.license:
            return None

        r = self.session.post(self.license, headers={"Preauthorization": self.token}, data=challenge)
        if not r.ok:
            raise ConnectionError(r.text)

        return r.content

    # Service specific functions

    def _show(self, title: str) -> Episode:
        params = {
            "include": "default",
            "decorators": "playbackAllowed,contentAction,badges",
        }
        data = self._request("GET", "/cms/routes/show/{}".format(title), params=params)

        content = next(x for x in data["included"] if x["attributes"].get("alias") == "generic-show-episodes")
        content_id = content["id"]
        show_id = content["attributes"]["component"]["mandatoryParams"]
        season_params = [x.get("parameter") for x in content["attributes"]["component"]["filters"][0]["options"]]
        page = next(x for x in data["included"] if x.get("type", "") == "page")

        seasons = [
            self._request(
                "GET", "/cms/collections/{}?{}&{}".format(content_id, season, show_id),
                params={"include": "default", "decorators": "playbackAllowed,contentAction,badges"},
            )
            for season in season_params
        ]

        videos = [[x for x in season["included"] if x["type"] == "video"] for season in seasons]

        return [
            Episode(
                id_=ep["id"],
                service=self.__class__,
                title=page["attributes"]["title"],
                year=ep["attributes"]["airDate"][:4],
                season=ep["attributes"].get("seasonNumber"),
                number=ep["attributes"].get("episodeNumber"),
                name=ep["attributes"]["name"],
                language=ep["attributes"]["audioTracks"][0]
                if ep["attributes"].get("audioTracks")
                else self.user_language,
                data=ep,
            )
            for episodes in videos
            for ep in episodes
            if ep["attributes"]["videoType"] == "EPISODE"
        ]

    def _episode(self, title: str) -> Episode:
        params = {
            "include": "default",
            "decorators": "playbackAllowed,contentAction,badges",
        }
        data = self._request("GET", "/cms/routes/video/{}".format(title), params=params)
        page = next((x for x in data["included"] if x.get("type", "") == "page"), None)
        if not page:
            raise IndexError("Episode page not found")

        video_id = page["relationships"].get("primaryContent", {}).get("data", {}).get("id")
        if not video_id:
            raise IndexError("Episode id not found")

        params = {"decorators": "isFavorite", "include": "primaryChannel"}
        content = self._request("GET", "/content/videos/{}".format(video_id), params=params)
        episode = content["data"]["attributes"]
        name = episode.get("name")
        if episode.get("secondaryTitle"):
            name += f" {episode.get('secondaryTitle')}"

        return [
            Episode(
                id_=content["data"].get("id"),
                service=self.__class__,
                title=page["attributes"]["title"],
                year=int(episode.get("airDate")[:4]) if episode.get("airDate") else None,
                season=episode.get("seasonNumber") or 0,
                number=episode.get("episodeNumber") or 0,
                name=name,
                language=episode["audioTracks"][0] if episode.get("audioTracks") else self.user_language,
                data=episode,
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
        url = urljoin(self.base_url, api)
        self.session.headers.update(self.config["headers"])

        if params:
            self.session.params.update(params)
        if headers:
            self.session.headers.update(headers)

        prep = self.session.prepare_request(Request(method, url, json=payload))
        response = self.session.send(prep)

        try:
            data = json.loads(response.content)

            if data.get("errors"):
                if "invalid.token" in data["errors"][0]["code"]:
                    self.log.error("- Invalid Token. Cookies are invalid or may have expired.")
                    sys.exit(1)

                if "missingpackage" in data["errors"][0]["code"]:
                    self.log.error("- Access Denied. Title is not available for this subscription.")
                    sys.exit(1)

                raise ConnectionError(data["errors"])

            return data

        except Exception as e:
            raise ConnectionError("Request failed: {}".format(e))
