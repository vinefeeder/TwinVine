from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Generator
from typing import Any, Optional, Union
from urllib.parse import urljoin

import click
from click import Context

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Subtitle, Tracks
from requests import Request


class AUBC(Service):
    """
    \b
    Service code for ABC iView streaming service (https://iview.abc.net.au/).

    \b
    Version: 1.0.1
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      L3: 1080p, AAC2.0

    \b
    Tips:
        - Input should be complete URL:
          SHOW: https://iview.abc.net.au/show/return-to-paradise
          EPISODE: https://iview.abc.net.au/video/DR2314H001S00
          MOVIE: https://iview.abc.net.au/show/way-back / https://iview.abc.net.au/show/way-back/video/ZW3981A001S00

    """

    GEOFENCE = ("au",)
    ALIASES = ("iview", "abciview", "iv",)

    @staticmethod
    @click.command(name="AUBC", short_help="https://iview.abc.net.au/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> AUBC:
        return AUBC(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update(self.config["headers"])

    def search(self) -> Generator[SearchResult, None, None]:
        url = (
            "https://y63q32nvdl-1.algolianet.com/1/indexes/*/queries?x-algolia-agent=Algolia"
            "%20for%20JavaScript%20(4.9.1)%3B%20Browser%20(lite)%3B%20react%20(17.0.2)%3B%20"
            "react-instantsearch%20(6.30.2)%3B%20JS%20Helper%20(3.10.0)&x-"
            "algolia-api-key=bcdf11ba901b780dc3c0a3ca677fbefc&x-algolia-application-id=Y63Q32NVDL"
        )
        payload = {
            "requests": [
                {
                    "indexName": "ABC_production_iview_web",
                    "params": f"query={self.title}&tagFilters=&userToken=anonymous-74be3cf1-1dc7-4fa1-9cff-19592162db1c",
                }
            ],
        }

        results = self._request("POST", url, payload=payload)["results"]
        hits = [x for x in results[0]["hits"] if x["docType"] == "Program"]

        for result in hits:
            yield SearchResult(
                id_="https://iview.abc.net.au/show/{}".format(result.get("slug")),
                title=result.get("title"),
                description=result.get("synopsis"),
                label=result.get("subType"),
                url="https://iview.abc.net.au/show/{}".format(result.get("slug")),
            )

    def get_titles(self) -> Union[Movies, Series]:
        title_re = r"^(?:https?://(?:www.)?iview.abc.net.au/(?P<type>show|video)/)?(?P<id>[a-zA-Z0-9_-]+)"
        try:
            kind, title_id = (re.match(title_re, self.title).group(i) for i in ("type", "id"))
        except Exception:
            raise ValueError("- Could not parse ID from title")

        if kind == "show":
            data = self._request("GET", "/v3/show/{}".format(title_id))
            label = data.get("type")

            if label.lower() in ("series", "program"):
                episodes = self._series(title_id)
                return Series(episodes)

            elif label.lower() in ("feature", "movie"):
                movie = self._movie(data)
                return Movies(movie)

        elif kind == "video":
            episode = self._episode(title_id)
            return Series([episode])

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        video = self._request("GET", "/v3/video/{}".format(title.id))
        if not video.get("playable"):
            raise ConnectionError(video.get("unavailableMessage"))

        playlist = video.get("_embedded", {}).get("playlist", {})
        if not playlist:
            raise ConnectionError("Could not find a playlist for this title")

        streams = next(x["streams"]["mpegdash"] for x in playlist if x["type"] == "program")
        captions = next((x.get("captions") for x in playlist if x["type"] == "program"), None)
        title.data["protected"] = streams.get("protected", False)

        if "720" in streams:
            streams["1080"] = streams["720"].replace("720", "1080")

        manifest = next(
            (url for key in ["1080", "720", "sd", "sd-low"] if key in streams
            for url in [streams[key]] 
            if self.session.head(url).status_code == 200),
            None
        )
        if not manifest:
            raise ValueError("Could not find a manifest for this title")

        tracks = DASH.from_url(manifest, self.session).to_tracks(title.language)

        for track in tracks.audio:
            role = track.data["dash"]["adaptation_set"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        if captions:
            subtitles = captions.get("src-vtt")
            tracks.add(
            Subtitle(
                id_=hashlib.md5(subtitles.encode()).hexdigest()[0:6],
                url=subtitles,
                codec=Subtitle.Codec.from_mime(subtitles[-3:]),
                language=title.language,
                forced=False,
            )
        )

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        if not title.data.get("cuePoints"):
            return Chapters()
        
        credits = next((x.get("start") for x in title.data["cuePoints"] if x["type"] == "end-credits"), None)
        if credits:
            return Chapters([Chapter(name="Credits", timestamp=credits * 1000)])
        
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Union[Movies, Series], track: AnyTrack) -> Optional[Union[bytes, str]]:
        if not title.data.get("protected"):
            return None

        customdata = self._license(title.id)
        headers = {"customdata": customdata}

        r = self.session.post(self.config["endpoints"]["license"], headers=headers, data=challenge)
        r.raise_for_status()
        return r.content

    # Service specific

    def _series(self, title: str) -> Episode:
        data = self._request("GET", "/v3/series/{}".format(title))

        episodes = [
            self.create_episode(episode)
            for season in data
            for episode in reversed(season["_embedded"]["videoEpisodes"]["items"])
            if season.get("episodeCount")
        ]
        return Series(episodes)

    def _movie(self, data: dict) -> Movie:
        return [
            Movie(
                id_=data["_embedded"]["highlightVideo"]["id"],
                service=self.__class__,
                name=data.get("title"),
                year=data.get("productionYear"),
                data=data,
                language=data.get("analytics", {}).get("dataLayer", {}).get("d_language", "en"),
            )
        ]

    def _episode(self, video_id: str) -> Episode:
        data = self._request("GET", "/v3/video/{}".format(video_id))
        return self.create_episode(data)

    def _license(self, video_id: str):
        token = self._request("POST", "/v3/token/jwt", data={"clientId": self.config["client"]})["token"]
        response = self._request("GET", "/v3/token/drm/{}".format(video_id), headers={"bearer": token})

        return response["license"]
    
    def create_episode(self, episode: dict) -> Episode:
        title = episode["showTitle"]
        season = re.search(r"Series (\d+)", episode.get("title"))
        number = re.search(r"Episode (\d+)", episode.get("title"))
        names_a = re.search(r"Series \d+ Episode \d+ (.+)", episode.get("title"))
        names_b = re.search(r"Series \d+ (.+)", episode.get("title"))
        name = names_a.group(1) if names_a else names_b.group(1) if names_b else episode.get("displaySubtitle")

        language = episode.get("analytics", {}).get("dataLayer", {}).get("d_language", "en")

        return Episode(
            id_=episode["id"],
            service=self.__class__,
            title=title,
            season=int(season.group(1)) if season else 0,
            number=int(number.group(1)) if number else 0,
            name=name,
            data=episode,
            language=language,
        )

    def _request(self, method: str, api: str, **kwargs: Any) -> Any[dict | str]:
        url = urljoin(self.config["endpoints"]["base_url"], api)

        prep = self.session.prepare_request(Request(method, url, **kwargs))

        response = self.session.send(prep)
        if response.status_code != 200:
            raise ConnectionError(f"{response.text}")

        try:
            return json.loads(response.content)

        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse JSON: {response.text}") from e

