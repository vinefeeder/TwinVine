from __future__ import annotations

import re
from collections.abc import Generator
from datetime import timedelta
from typing import Any, Union

import click
from click import Context
from envied.core.manifests.dash import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Tracks
from lxml import etree


class UKTV(Service):
    """
    Service code for 'U' (formerly UKTV Play) streaming service (https://u.co.uk/).

    \b
    Version: 1.0.0
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      L3: 1080p

    \b
    Tips:
        - Use complete title URL as input:
            SERIES: https://u.co.uk/shows/love-me/watch-online
            EPISODE: https://u.co.uk/shows/love-me/series-1/episode-1/6355269425112

    """

    GEOFENCE = ("gb",)
    ALIASES = ("uktvplay", "u",)

    @staticmethod
    @click.command(name="UKTV", short_help="https://u.co.uk/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> UKTV:
        return UKTV(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update({"user-agent": "okhttp/4.7.2"})
        self.base = self.config["endpoints"]["base"]

    def search(self) -> Generator[SearchResult, None, None]:
        r = self.session.get(self.base + f"search/?q={self.title}")
        r.raise_for_status()
        results = r.json()

        for result in results:
            link = "https://u.co.uk/shows/{}/watch-online"

            yield SearchResult(
                id_=link.format(result.get("slug")),
                title=result.get("name"),
                description=result.get("synopsis"),
                label=result.get("type"),
                url=link.format(result.get("slug")),
            )

    def get_titles(self) -> Union[Movies, Series]:
        slug, video = self.parse_title(self.title)

        r = self.session.get(self.base + f"brand/?slug={slug}")
        r.raise_for_status()
        data = r.json()

        series = [series["id"] for series in data["series"]]
        seasons = [self.session.get(self.base + f"series/?id={i}").json() for i in series]

        if video:
            episodes = [
                Episode(
                    id_=episode.get("video_id"),
                    service=self.__class__,
                    title=episode.get("brand_name"),
                    season=int(episode.get("series_number", 0)),
                    number=int(episode.get("episode_number", 0)),
                    name=episode.get("name"),
                    language="en",
                    data=episode,
                )
                for season in seasons
                for episode in season["episodes"]
                if int(episode.get("video_id")) == int(video)
            ]
        else:
            episodes = [
                Episode(
                    id_=episode.get("video_id"),
                    service=self.__class__,
                    title=episode.get("brand_name"),
                    season=int(episode.get("series_number", 0)),
                    number=int(episode.get("episode_number", 0)),
                    name=episode.get("name"),
                    language="en",
                    data=episode,
                )
                for season in seasons
                for episode in season["episodes"]
            ]

        return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        r = self.session.get(
            self.config["endpoints"]["playback"].format(id=title.id),
            headers=self.config["headers"],
        )
        r.raise_for_status()
        data = r.json()

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
        if not self.license or not source_manifest:
            raise ValueError("Failed to get license or manifest")

        manifest = self.trim_duration(source_manifest)
        tracks = DASH.from_text(manifest, source_manifest).to_tracks(title.language)

        for track in tracks.audio:
            role = track.data["dash"]["representation"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        chapters = []
        if title.data.get("credits_cuepoint"):
            chapters = [Chapter(name="Credits", timestamp=title.data.get("credits_cuepoint"))]

        return Chapters(chapters)

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        r = self.session.post(url=self.license, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.content

    # Service specific functions

    @staticmethod
    def parse_title(title: str) -> tuple[str, str]:
        title_re = (
            r"^(?:https?://(?:www\.)?u\.co.uk/shows/)?"
            r"(?P<slug>[a-z0-9-]+)(?:/[a-z0-9-]+/[a-z0-9-]+/(?P<vid>[0-9-]+))?"
        )

        try:
            slug, video = (re.match(title_re, title).group(i) for i in ("slug", "vid"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        return slug, video

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
