from __future__ import annotations

import hashlib
import re
import sys
import uuid
from collections.abc import Generator
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional

import click
from langcodes import Language
from pyplayready.cdm import Cdm as PlayReadyCdm
from envied.core.credential import Credential
from envied.core.downloaders import requests
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Audio, Chapter, Chapters, Subtitle, Tracks


class TUBI(Service):
    """
    Service code for TubiTV streaming service (https://tubitv.com/)

    \b
    Version: 1.0.8
    Author: stabbedbybrick
    Authorization: Cookies (Optional)
    Geofence: Locked to whatever region the user is in (API only)
    Robustness:
        Widevine:
            L3: 1080p, AAC2.0
        PlayReady:
            SL2000: 1080p, AAC2.0

    \b
    Tips:
        - Input can be complete title URL or just the path:
            /series/300001423/gotham
            /tv-shows/200024793/s01-e01-pilot
            /movies/589279/the-outsiders

    \b
    Notes:
        - Authorization is currently only required for search, not for content.
        - If 1080p exists, it's typically only available as HEVC/H.265.
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?tubitv\.com?)?/(?:[a-z]{2}-[a-z]{2}/)?(?P<type>movies|series|tv-shows)/(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="TUBI", short_help="https://tubitv.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return TUBI(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.title = title

        cdm = ctx.obj.cdm
        self.drm_system = "playready" if isinstance(cdm, PlayReadyCdm) else "widevine"

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.auth_token = None
        if cookies is not None:
            self.auth_token = next((cookie.value for cookie in cookies if cookie.name == "at"), None)
            self.session.headers.update({"Authorization": f"Bearer {self.auth_token}"})

    def search(self) -> Generator[SearchResult, None, None]:
        if not self.auth_token:
            self.log.error(" - Error: Search requires authentication/cookies.")
            return
        
        params = {
            "search": self.title,
            "include_linear": "false",
            "include_channels": "false",
            "is_kids_mode": "false",
        }

        r = self.session.get(self.config["endpoints"]["search"], params=params)
        r.raise_for_status()
        results = r.json()
        
        search_results = [x for x in results["contents"].values()]

        for result in search_results:
            label = "series" if result["type"] == "s" else "movies" if result["type"] == "v" else result["type"]
            title = (
                result.get("title", "")
                .lower()
                .replace(" ", "-")
                .replace(":", "")
                .replace("(", "")
                .replace(")", "")
                .replace(".", "")
                .replace("'", "-")
            )
            yield SearchResult(
                id_=f"https://tubitv.com/{label}/{result.get('id')}/{title}",
                title=result.get("title"),
                description=result.get("description"),
                label=label,
                url=f"https://tubitv.com/{label}/{result.get('id')}/{title}",
            )

    def get_titles(self) -> Titles_T:
        try:
            kind, content_id = (re.match(self.TITLE_RE, self.title).group(i) for i in ("type", "id"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        params = {
            "app_id": "tubitv",
            "platform": "web", # web, android, androidtv
            "device_id": str(uuid.uuid4()),
            "content_id": content_id,
            "limit_resolutions[]": [
                "h264_1080p",
                "h265_1080p",
            ],
            "video_resources[]": [
                "dash_widevine_nonclearlead",
                "dash_playready_psshv0",
                "dash",
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
        if not (resources := title.data.get("video_resources")):
            self.log.error(" - Failed to obtain video resources. Check geography settings.")
            self.log.info(f"Title is available in: {title.data.get('country')}")
            sys.exit(1)

        tracks = Tracks()

        for codec in ["h264", "h265"]:
            codec_matches = [x for x in resources if codec in x.get("codec", "").lower()]

            resource = next(iter(sorted(codec_matches, key=lambda x: (
                self.drm_system not in x.get("type", ""),
                "dash" not in x.get("type", "")
            ))), None)
            if not resource:
                self.log.warning(f" - Could not find a {codec} video resource for this title")
                continue
        
            manifest = resource.get("manifest", {}).get("url")
            
            title.data["license_url"] = resource.get("license_server", {}).get("url")

            tracks.add(DASH.from_url(url=manifest, session=self.session).to_tracks(language=title.language))

        for track in tracks:
            if isinstance(track, Audio):
                role = track.data["dash"]["adaptation_set"].find("Role")
                if role is not None and role.get("value").lower() in ["description", "alternative", "alternate"]:
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
        if not (cue_points := title.data.get("credit_cuepoints")):
            return Chapters()
        
        chapters = []
        if cue_points.get("recap_start"):
            chapters.append(Chapter(name="Recap", timestamp=float(cue_points["recap_start"])))
        if cue_points.get("intro_start") and cue_points.get("intro_end"):
            chapters.append(Chapter(name="Intro", timestamp=float(cue_points["intro_start"])))
            chapters.append(Chapter(timestamp=float(cue_points["intro_end"])))
        if cue_points.get("early_credits_start"):
            chapters.append(Chapter(name="Early Credits", timestamp=float(cue_points["early_credits_start"])))
        if cue_points.get("postlude"):
            chapters.append(Chapter(name="End Credits", timestamp=float(cue_points["postlude"])))

        if not any(c.timestamp == "00:00:00.000" for c in chapters):
            chapters.append(Chapter(timestamp=0))

        return sorted(chapters, key=lambda x: x.timestamp)

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (license_url := title.data.get("license_url")):
            return None

        r = self.session.post(url=license_url, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        return r.content
    
    def get_playready_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (license_url := title.data.get("license_url")):
            return None

        r = self.session.post(url=license_url, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        return r.content