from __future__ import annotations

import json
import re
import sys
from collections.abc import Generator
from typing import Any, Optional, Union
from urllib.parse import urljoin

import click
from envied.core.constants import AnyTrack
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Tracks
from envied.core.utils.sslciphers import SSLCiphers
from envied.core.utils.xml import load_xml
from requests import Request


class CBS(Service):
    """
    \b
    Service code for CBS.com streaming service (https://cbs.com).
    Credit to @srpen6 for the tip on anonymous session

    \b
    Version: 1.0.0
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      Widevine:
        L3: 2160p, DDP5.1

    \b
    Tips:
        - Input should be complete URLs:
          SERIES: https://www.cbs.com/shows/tracker/
          EPISODE: https://www.cbs.com/shows/video/E0wG_ovVMkLlHOzv7KDpUV9bjeKFFG2v/

    \b
    Common VPN/proxy errors:
        - SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]'))
        - ConnectionError: 406 Not Acceptable, 403 Forbidden

    """

    GEOFENCE = ("us",)

    @staticmethod
    @click.command(name="CBS", short_help="https://cbs.com", help=__doc__)
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs) -> CBS:
        return CBS(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "term": self.title,
            "termCount": 50,
            "showCanVids": "true",
        }
        results = self._request("GET", "/apps-api/v3.1/androidphone/contentsearch/search.json", params=params)["terms"]

        for result in results:
            yield SearchResult(
                id_=result.get("path"),
                title=result.get("title"),
                description=None,
                label=result.get("term_type"),
                url=result.get("path"),
            )

    def get_titles(self) -> Titles_T:
        title_re = r"https://www\.cbs\.com/shows/(?P<video>video/)?(?P<id>[a-zA-Z0-9_-]+)/?$"
        try:
            video, title_id = (re.match(title_re, self.title).group(i) for i in ("video", "id"))
        except Exception:
            raise ValueError("- Could not parse ID from title")

        if video:
            episodes = self._episode(title_id)
        else:
            episodes = self._show(title_id)

        return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        self.token, self.license = self.ls_session(title.id)
        manifest = self.get_manifest(title)
        return DASH.from_url(url=manifest).to_tracks(language=title.language)

    def get_chapters(self, title: Episode) -> Chapters:
        if not title.data.get("playbackEvents", {}).get("endCreditChapterTimeMs"):
            return Chapters()

        end_credits = title.data["playbackEvents"]["endCreditChapterTimeMs"]
        return Chapters([Chapter(name="Credits", timestamp=end_credits)])

    def certificate(self, **_):
        return None  # will use common privacy cert

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        headers = {"Authorization": f"Bearer {self.token}"}
        r = self.session.post(self.license, headers=headers, data=challenge)
        if not r.ok:
            self.log.error(r.text)
            sys.exit(1)
        return r.content

    # Service specific functions

    def _show(self, title: str) -> Episode:
        data = self._request("GET", "/apps-api/v3.0/androidphone/shows/slug/{}.json".format(title))

        links = next((x.get("links") for x in data["showMenu"] if x.get("device_app_id") == "all_platforms"), None)
        config = next((x.get("videoConfigUniqueName") for x in links if x.get("title").strip() == "Episodes"), None)
        show = next((x for x in data["show"]["results"] if x.get("type").strip() == "show"), None)
        seasons = [x.get("seasonNum") for x in data["available_video_seasons"].get("itemList", [])]
        locale = show.get("locale", "en-US")

        show_data = self._request(
            "GET", "/apps-api/v2.0/androidphone/shows/{}/videos/config/{}.json".format(show.get("show_id"), config),
            params={"platformType": "apps", "rows": "1", "begin": "0"},
        )

        section = next(
            (x["sectionId"] for x in show_data["videoSectionMetadata"] if x["title"] == "Full Episodes"), None
        )

        episodes = []
        for season in seasons:
            res = self._request(
                "GET", "/apps-api/v2.0/androidphone/videos/section/{}.json".format(section),
                params={"begin": "0", "rows": "999", "params": f"seasonNum={season}", "seasonNum": season},
            )
            episodes.extend(res["sectionItems"].get("itemList", []))

        return [
            Episode(
                id_=episode["contentId"],
                title=episode["seriesTitle"],
                season=episode["seasonNum"] if episode["fullEpisode"] else 0,
                number=episode["episodeNum"] if episode["fullEpisode"] else episode["positionNum"],
                name=episode["label"],
                language=locale,
                service=self.__class__,
                data=episode,
            )
            for episode in episodes
            if episode["fullEpisode"]
        ]

    def _episode(self, title: str) -> Episode:
        data = self._request("GET", "/apps-api/v2.0/androidphone/video/cid/{}.json".format(title))

        return [
            Episode(
                id_=episode["contentId"],
                title=episode["seriesTitle"],
                season=episode["seasonNum"] if episode["fullEpisode"] else 0,
                number=episode["episodeNum"] if episode["fullEpisode"] else episode["positionNum"],
                name=episode["label"],
                language="en-US",
                service=self.__class__,
                data=episode,
            )
            for episode in data["itemList"]
        ]

    def ls_session(self, content_id: str) -> str:
        res = self._request(
            "GET", "/apps-api/v3.0/androidphone/irdeto-control/anonymous-session-token.json",
            params={"contentId": content_id},
        )

        return res.get("ls_session"), res.get("url")

    def get_manifest(self, title: Episode) -> str:
        try:
            res = self._request(
                "GET", "http://link.theplatform.com/s/{}/media/guid/2198311517/{}".format(
                    title.data.get("cmsAccountId"), title.id
                ),
                params={
                    "format": "SMIL",
                    "assetTypes": "|".join(self.config["assets"]),
                    "formats": "MPEG-DASH,MPEG4,M3U",
                },
            )

            body = load_xml(res).find("body").find("seq").findall("switch")
            bitrate = max(body, key=lambda x: int(x.find("video").get("system-bitrate")))
            videos = [x.get("src") for x in bitrate.findall("video")]
            if not videos:
                raise ValueError("Could not find any streams - is the title still available?")

            manifest = next(
                (x for x in videos if "hdr_dash" in x.lower()),
                next((x for x in videos if "cenc_dash" in x.lower()), videos[0]),
            )

        except Exception as e:
            self.log.warning("ThePlatform request failed: {}, falling back to standard manifest".format(e))
            if not title.data.get("streamingUrl"):
                raise ValueError("Could not find any streams - is the title still available?")

            manifest = title.data.get("streamingUrl")

        return manifest

    def _request(self, method: str, api: str, params: dict = None, headers: dict = None) -> Any[dict | str]:
        url = urljoin(self.config["endpoints"]["base_url"], api)
        self.session.headers.update(self.config["headers"])
        self.session.params = {"at": self.config["endpoints"]["token"]}
        for prefix in ("https://", "http://"):
            self.session.mount(prefix, SSLCiphers(security_level=2))

        if params:
            self.session.params.update(params)
        if headers:
            self.session.headers.update(headers)

        prep = self.session.prepare_request(Request(method, url))

        response = self.session.send(prep)
        if response.status_code != 200:
            raise ConnectionError(f"{response.text}")

        try:
            data = json.loads(response.content)
            if not data.get("success"):
                raise ValueError(data.get("message"))
            return data

        except json.JSONDecodeError:
            return response.text
