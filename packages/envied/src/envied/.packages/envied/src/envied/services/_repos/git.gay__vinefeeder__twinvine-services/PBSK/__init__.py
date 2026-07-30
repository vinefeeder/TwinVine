from __future__ import annotations
import json
import re
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Optional, Union
import click
from langcodes import Language
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH, HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Tracks


class PBSK(Service):
    """
    Service code for the PBS Kids streaming service (https://pbskids.org)
    www.nostalgic.cc
    Authorization: None
    Security: FHD@L3
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?pbskids\.org)?(?P<path>/videos/watch/[^?#\s]+)"
    GEOFENCE = ("US",)

    @staticmethod
    @click.command(name="PBSK", short_help="https://pbskids.org")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return PBSK(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        self.title = title
        self._license_url: Optional[str] = None

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

    def search(self) -> Generator[SearchResult, None, None]:
        return
        yield

    def get_titles(self) -> Titles_T:
        match = re.match(self.TITLE_RE, self.title.strip())
        if not match:
            raise ValueError(f"Unrecognized URL: {self.title!r}")

        path = match.group("path").rstrip("/")
        page_url = f"https://pbskids.org{path}"

        self.log.info("Detecting buildId")
        resp = self.session.get(page_url)
        resp.raise_for_status()

        nd_match = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            resp.text,
            re.DOTALL,
        )
        if not nd_match:
            raise RuntimeError("__NEXT_DATA__ not found.")

        next_data = json.loads(nd_match.group(1))
        build_id = next_data.get("buildId")
        if not build_id:
            raise RuntimeError("buildId not found.")
        self.log.debug(f"BuildID: {build_id}")

        api_url = f"https://pbskids.org/_next/data/{build_id}/en-US{path}.json"
        self.log.info(f"Fetching JSON from: {api_url}")
        resp = self.session.get(api_url)
        resp.raise_for_status()
        data = resp.json()

        video_data = data["pageProps"]["videoData"]
        asset = video_data["mediaManagerAsset"]

        video_id = str(video_data["id"])
        video_type = video_data.get("videoType", "short")
        title_text = video_data.get("title") or asset.get("title", "Unknown")
        description = asset.get("description_short") or asset.get("description_long", "")
        season_number = asset.get("season_number")
        episode_number = asset.get("episode_number")

        show_title: Optional[str] = None
        for prop in video_data.get("properties", []):
            show_title = prop.get("title")
            if show_title:
                break

        title_data = {
            "asset": asset,
            "drm_enabled": bool(asset.get("drm_enabled", False)),
        }

        if (
            video_type == "fullEpisode"
            and show_title
            and season_number is not None
            and episode_number is not None
        ):
            return Series(
                [
                    Episode(
                        id_=video_id,
                        service=self.__class__,
                        title=show_title,
                        season=season_number,
                        number=episode_number,
                        name=title_text,
                        description=description,
                        year=None,
                        language=Language.get("en"),
                        data=title_data,
                    )
                ]
            )
        else:
            return Movies(
                [
                    Movie(
                        id_=video_id,
                        service=self.__class__,
                        name=title_text,
                        description=description,
                        year=None,
                        language=Language.get("en"),
                        data=title_data,
                    )
                ]
            )

    def get_tracks(self, title: Title_T) -> Tracks:
        asset = title.data["asset"]
        drm_enabled = title.data["drm_enabled"]
        videos = asset.get("videos", [])

        if drm_enabled:
            return self._get_drm_tracks(title, videos)
        else:
            return self._get_clear_tracks(title, videos)

    def _resolve_redirect(self, url: str) -> str:
        resp = self.session.head(url, allow_redirects=True)
        return str(resp.url)

    @staticmethod
    def _get_profile_height(profile: str) -> int:
        match = re.search(r"-(\d+)p(?:-|$)", profile)
        return int(match.group(1)) if match else -1

    def _pick_best_stream(
        self,
        videos: list[dict],
        prefix: str,
        suffix: str = "",
        required_key: Optional[str] = None,
    ) -> Optional[dict]:
        candidates: list[dict] = []
        seen_urls: set[str] = set()

        for video in videos:
            profile = str(video.get("profile") or "")
            url = video.get("url", "")
            if not profile.startswith(prefix):
                continue
            if suffix and not profile.endswith(suffix):
                continue
            if required_key and not video.get(required_key):
                continue
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            candidates.append(video)

        if not candidates:
            return None

        return max(candidates, key=lambda v: self._get_profile_height(str(v.get("profile") or "")))

    def _get_clear_tracks(self, title: Title_T, videos: list[dict]) -> Tracks:
        video = self._pick_best_stream(videos, prefix="hls-")
        if not video:
            raise RuntimeError("No HLS stream found.")

        stream_url = self._resolve_redirect(video["url"])
        self.log.debug(f"HLS stream URL: {stream_url}")

        return HLS.from_url(url=stream_url, session=self.session).to_tracks(language=title.language)

    def _get_drm_tracks(self, title: Title_T, videos: list[dict]) -> Tracks:
        video = self._pick_best_stream(videos, prefix="dash-", suffix="-drm", required_key="widevine_license")
        if not video:
            raise RuntimeError("No Widevine DASH stream found.")

        self._license_url = video.get("widevine_license")
        if not self._license_url:
            raise RuntimeError("No Widevine license URL detected from DASH stream.")

        stream_url = self._resolve_redirect(video["url"])
        self.log.debug(f"DASH stream URL: {stream_url}")
        self.log.debug(f"Widevine license URL: {self._license_url}")

        return DASH.from_url(url=stream_url, session=self.session).to_tracks(language=title.language)

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []

    def get_widevine_service_certificate(self, **_: any) -> Optional[str]:
        if self.config:
            return self.config.get("certificate")
        return None

    def get_widevine_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        if not self._license_url:
            raise RuntimeError("Widevine license URL is not set.")

        resp = self.session.post(
            url=self._license_url,
            data=challenge,
            headers={"Content-Type": "application/octet-stream"},
        )
        resp.raise_for_status()
        return resp.content
