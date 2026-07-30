from __future__ import annotations

import re
from collections.abc import Generator
from datetime import timedelta
from typing import Any, Union
from urllib.parse import urlparse

import click
from click import Context
from lxml import etree
from envied.core.manifests.dash import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Tracks
import json


class STV(Service):
    """
    Service code for STV Player streaming service (https://player.stv.tv/).

    \b
    Version: 1.0.4
    Author: stabbedbybrick; search corrected by Angela 
    Authorization: None
    Robustness:
      L3: 1080p

    \b
    Tips:
        - Use complete title URL as input:
            SERIES: https://player.stv.tv/summary/rebus
            EPISODE: https://player.stv.tv/episode/2ro8/rebus
        - Use the episode URL for movies:
            MOVIE: https://player.stv.tv/episode/4lw7/wonder-woman-1984

    """

    GEOFENCE = ("gb",)
    ALIASES = ("stvplayer",)

    @staticmethod
    @click.command(name="STV", short_help="https://player.stv.tv/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> STV:
        return STV(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update({"user-agent": "okhttp/4.11.0"})
        self.base = self.config["endpoints"]["base"]

    def search(self) -> Generator[SearchResult, None, None]:
        
        search_term = self.title.replace(" ", "+")
        r = self.session.get(self.config["endpoints"]["search"].format(search_term=search_term))
        r.raise_for_status()
        results = json.loads(r.text)["data"]

        for result in results:
            title = result['attributes']["title"]
            url  =  result['attributes']["permalink"]
            synopsis = result['attributes']["long_description"]
    
            label = result["attributes"]["subGenre"]
            #image = result["attributes"]["images"][0]["_filepath"].split("&w")[0] if result["attributes"]["images"] else None

            yield SearchResult(
                id_= self.config["endpoints"]["prefix"].strip("/") + result["attributes"]["permalink"],
                title=title,
                description=synopsis,
                label=label,
                url=url,
                #image=image
            )

    def get_titles(self) -> Union[Movies, Series]:
        kind, slug = self.parse_title(self.title)
        self.session.headers.update({"stv-drm": "true"})

        if kind == "episode":
            r = self.session.get(self.base + f"episodes/{slug}")
            r.raise_for_status()
            episode = r.json()["results"]

            if episode.get("genre").lower() == "movie":
                return Movies(
                    [
                        Movie(
                            id_=episode["video"].get("id"),
                            service=self.__class__,
                            year=None,
                            name=episode.get("title"),
                            language="en",
                            data=episode,
                        )
                    ]
                )

            episodes = [
                Episode(
                    id_=episode["video"].get("id"),
                    service=self.__class__,
                    title=episode["programme"].get("name"),
                    season=int(episode["playerSeries"]["name"].split(" ")[1])
                    if episode.get("playerSeries") and re.match(r"Series \d+", episode["playerSeries"]["name"])
                    else 0,
                    number=int(episode.get("number", 0)),
                    name=re.sub(r"^\d+\.\s+", "", episode.get("title", "")),
                    language="en",
                    data=episode,
                )
            ]

        elif kind == "summary":
            r = self.session.get(self.base + f"episodes?programme.guid={slug}&limit=999")
            if not r.ok:
                raise ConnectionError(f"Failed to find content for {slug}")

            data = r.json()

            episodes = [
                Episode(
                    id_=episode["video"].get("id"),
                    service=self.__class__,
                    title=episode["programme"].get("name"),
                    season=int(episode["playerSeries"]["name"].split(" ")[1])
                    if episode.get("playerSeries")
                    and re.match(r"Series \d+", episode["playerSeries"]["name"])
                    else 0,
                    number=int(episode.get("number", 0)),
                    name=re.sub(r"^\d+\.\s+", "", episode.get("title", "")),
                    language="en",
                    data=episode,
                )
                for episode in data["results"]
            ]

        self.session.headers.pop("stv-drm")
        return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        self.drm = title.data["programme"].get("drmEnabled")
        headers = self.config["headers"]["drm"] if self.drm else self.config["headers"]["clear"]
        accounts = self.config["accounts"]["drm"] if self.drm else self.config["accounts"]["clear"]

        r = self.session.get(
            self.config["endpoints"]["playback"].format(accounts=accounts, id=title.id),
            headers=headers,
        )
        if not r.ok:
            raise ConnectionError(r.text)
        data = r.json()

        source_manifest = next(
            (source["src"] for source in data["sources"] if source.get("type") == "application/dash+xml"),
            None,
        )

        self.license = None
        if self.drm:
            key_systems = next((
                source
                for source in data["sources"]
                if source.get("type") == "application/dash+xml"
                and source.get("key_systems").get("com.widevine.alpha")),
                None,
            )

            self.license = key_systems["key_systems"]["com.widevine.alpha"]["license_url"] if key_systems else None

        manifest = self.trim_duration(source_manifest)
        tracks = DASH.from_text(manifest, source_manifest).to_tracks(title.language)

        for track in tracks.audio:
            role = track.data["dash"]["representation"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        cue_points = title.data.get("_cuePoints")
        if not cue_points:
            return Chapters()

        return Chapters([Chapter(timestamp=int(cue)) for cue in cue_points])

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        if not self.license:
            return None

        r = self.session.post(url=self.license, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.content

    # Service specific functions

    @staticmethod
    def parse_title(title: str) -> tuple[str, str]:
        parsed_url = urlparse(title).path.split("/")
        kind, slug = parsed_url[1], parsed_url[2]
        if kind not in ["episode", "summary"]:
            raise ValueError("Failed to parse title - is the URL correct?")

        return kind, slug

    @staticmethod
    def trim_duration(source_manifest: str) -> str:
        """
        The last segment on all tracks return a 404 for some reason, causing a failed download.
        So we trim the duration by exactly one segment to account for that.

        TODO: Calculate the segment duration instead of assuming length.
        """
        manifest = DASH.from_url(source_manifest).manifest
        period_duration = manifest.get("mediaPresentationDuration")
        period_duration = DASH.pt_to_sec(period_duration)

        hours, minutes, seconds = str(timedelta(seconds=period_duration - 6)).split(":")
        new_duration = f"PT{hours}H{minutes}M{seconds}S"
        manifest.set("mediaPresentationDuration", new_duration)

        return etree.tostring(manifest, encoding="unicode")
