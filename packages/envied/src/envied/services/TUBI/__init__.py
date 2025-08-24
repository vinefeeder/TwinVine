from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from collections.abc import Generator
from typing import Any

import click
from envied.core.downloaders import aria2c, requests
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Subtitle, Track, Tracks
from langcodes import Language


class TUBI(Service):
    """
    Service code for TubiTV streaming service (https://tubitv.com/)

    \b
    Version: 1.0.1
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      Widevine:
        L3: 720p, AAC2.0

    \b
    Tips:
        - Input can be complete title URL or just the path:
            /series/300001423/gotham
            /tv-shows/200024793/s01-e01-pilot
            /movies/589279/the-outsiders

    \b
    Notes:
        - Due to the structure of the DASH manifest and requests downloader failing to output progress,
          aria2c is used as the downloader no matter what downloader is specified in the config.
        - Search is currently disabled.
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?tubitv\.com?)?/(?P<type>movies|series|tv-shows)/(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="TUBI", short_help="https://tubitv.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return TUBI(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)

    # Disable search for now
    # def search(self) -> Generator[SearchResult, None, None]:
    #     params = {
    #         "search": self.title,
    #         "include_linear": "true",
    #         "include_channels": "false",
    #         "is_kids_mode": "false",
    #     }

    #     r = self.session.get(self.config["endpoints"]["search"], params=params)
    #     r.raise_for_status()
    #     results = r.json()
    #     from envied.core.console import console
    #     console.print(results)
    #     exit()

    #     for result in results:
    #         label = "series" if result["type"] == "s" else "movies" if result["type"] == "v" else result["type"]
    #         title = (
    #             result.get("title", "")
    #             .lower()
    #             .replace(" ", "-")
    #             .replace(":", "")
    #             .replace("(", "")
    #             .replace(")", "")
    #             .replace(".", "")
    #         )
    #         yield SearchResult(
    #             id_=f"https://tubitv.com/{label}/{result.get('id')}/{title}",
    #             title=result.get("title"),
    #             description=result.get("description"),
    #             label=label,
    #             url=f"https://tubitv.com/{label}/{result.get('id')}/{title}",
    #         )

    def get_titles(self) -> Titles_T:
        try:
            kind, content_id = (re.match(self.TITLE_RE, self.title).group(i) for i in ("type", "id"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        params = {
            "platform": "android",
            "content_id": content_id,
            "device_id": str(uuid.uuid4()),
            "video_resources[]": [
                "dash",
                "dash_widevine",
            ],
        }

        if kind == "tv-shows":
            content = self.session.get(self.config["endpoints"]["content"], params=params)
            content.raise_for_status()
            series_id = "0" + content.json().get("series_id")
            params.update({"content_id": int(series_id)})
            data = self.session.get(self.config["endpoints"]["content"], params=params).json()

            return Series(
                [
                    Episode(
                        id_=episode["id"],
                        service=self.__class__,
                        title=data["title"],
                        season=int(season.get("id", 0)),
                        number=int(episode.get("episode_number", 0)),
                        name=episode["title"].split("-")[1],
                        year=data.get("year"),
                        language=Language.find(episode.get("lang", "en")).to_alpha3(),
                        data=episode,
                    )
                    for season in data["children"]
                    for episode in season["children"]
                    if episode["id"] == content_id
                ]
            )

        if kind == "series":
            r = self.session.get(self.config["endpoints"]["content"], params=params)
            r.raise_for_status()
            data = r.json()

            return Series(
                [
                    Episode(
                        id_=episode["id"],
                        service=self.__class__,
                        title=data["title"],
                        season=int(season.get("id", 0)),
                        number=int(episode.get("episode_number", 0)),
                        name=episode["title"].split("-")[1],
                        year=data.get("year"),
                        language=Language.find(episode.get("lang") or "en").to_alpha3(),
                        data=episode,
                    )
                    for season in data["children"]
                    for episode in season["children"]
                ]
            )

        if kind == "movies":
            r = self.session.get(self.config["endpoints"]["content"], params=params)
            r.raise_for_status()
            data = r.json()
            return Movies(
                [
                    Movie(
                        id_=data["id"],
                        service=self.__class__,
                        year=data.get("year"),
                        name=data["title"],
                        language=Language.find(data.get("lang", "en")).to_alpha3(),
                        data=data,
                    )
                ]
            )

    def get_tracks(self, title: Title_T) -> Tracks:
        if not title.data.get("video_resources"):
            self.log.error(" - Failed to obtain video resources. Check geography settings.")
            self.log.info(f"Title is available in: {title.data.get('country')}")
            sys.exit(1)

        self.manifest = title.data["video_resources"][0]["manifest"]["url"]
        self.license = title.data["video_resources"][0].get("license_server", {}).get("url")

        tracks = DASH.from_url(url=self.manifest, session=self.session).to_tracks(language=title.language)
        for track in tracks:
            rep_base = track.data["dash"]["representation"].find("BaseURL")
            if rep_base is not None:
                base_url = os.path.dirname(track.url)
                track_base = rep_base.text
                track.url = f"{base_url}/{track_base}"
                track.descriptor = Track.Descriptor.URL
                track.downloader = aria2c

        for track in tracks.audio:
            role = track.data["dash"]["adaptation_set"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        if title.data.get("subtitles"):
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(title.data["subtitles"][0]["url"].encode()).hexdigest()[0:6],
                    url=title.data["subtitles"][0]["url"],
                    codec=Subtitle.Codec.from_mime(title.data["subtitles"][0]["url"][-3:]),
                    language=title.data["subtitles"][0].get("lang_alpha3", title.language),
                    downloader=requests,
                    is_original_lang=True,
                    forced=False,
                    sdh=False,
                )
            )
        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        cue_points = title.data.get("credit_cuepoints")
        if not cue_points:
            return Chapters()
        
        chapters = []
        for title, cuepoint in cue_points.items():
            if cuepoint > 0:
                chapters.append(Chapter(timestamp=float(cuepoint), name=title))

        return Chapters(chapters)

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        if not self.license:
            return None

        r = self.session.post(url=self.license, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        return r.content
