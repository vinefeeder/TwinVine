import base64
import json
import os
import re
from http.cookiejar import CookieJar
from typing import Optional, Generator

import click
from envied.core.search_result import SearchResult
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Movie, Movies, Series, Episode, Title_T, Titles_T
from envied.core.tracks import Chapter, Tracks, Subtitle
from envied.core.drm import Widevine
from langcodes import Language


class VIKI(Service):
    """
    Service code for Rakuten Viki (viki.com)
    Version: 1.4.0

    Authorization: Required cookies (_viki_session, device_id).
    Security: FHD @ L3 (Widevine)

    Supports:
      • Movies and TV Series 
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?viki\.com)?/(?:movies|tv)/(?P<id>\d+c)-.+$"
    GEOFENCE = ()
    NO_SUBTITLES = False

    @staticmethod
    @click.command(name="VIKI", short_help="https://viki.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return VIKI(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)

        m = re.match(self.TITLE_RE, title)
        if not m:
            self.search_term = title
            self.title_url = None
            return

        self.container_id = m.group("id")
        self.title_url = title
        self.video_id: Optional[str] = None
        self.api_access_key: Optional[str] = None
        self.drm_license_url: Optional[str] = None

        self.cdm = ctx.obj.cdm
        if self.config is None:
            raise EnvironmentError("Missing service config for VIKI.")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        if not cookies:
            raise PermissionError("VIKI requires a cookie file for authentication.")

        session_cookie = next((c for c in cookies if c.name == "_viki_session"), None)
        device_cookie = next((c for c in cookies if c.name == "device_id"), None)

        if not session_cookie or not device_cookie:
            raise PermissionError("Your cookie file is missing '_viki_session' or 'device_id'.")

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
            "X-Viki-App-Ver": "14.64.0",
            "X-Viki-Device-ID": device_cookie.value,
            "Origin": "https://www.viki.com",
            "Referer": "https://www.viki.com/",
        })
        self.log.info("VIKI authentication cookies loaded successfully.")

    def get_titles(self) -> Titles_T:
        if not self.title_url:
            raise ValueError("No URL provided to process.")

        self.log.debug(f"Scraping page for API access key: {self.title_url}")
        r_page = self.session.get(self.title_url)
        r_page.raise_for_status()

        match = re.search(r'"token":"([^"]+)"', r_page.text)
        if not match:
            raise RuntimeError("Failed to extract API access key from page source.")
        
        self.api_access_key = match.group(1)
        self.log.debug(f"Extracted API access key: {self.api_access_key[:10]}...")

        url = self.config["endpoints"]["container"].format(container_id=self.container_id)
        params = {
            "app": self.config["params"]["app"],
            "token": self.api_access_key,
        }
        r = self.session.get(url, params=params)
        r.raise_for_status()
        data = r.json()

        content_type = data.get("type")
        if content_type == "film":
            return self._parse_movie(data)
        elif content_type == "series":
            return self._parse_series(data)
        else:
            self.log.error(f"Unknown content type '{content_type}' found.")
            return Movies([])

    def _parse_movie(self, data: dict) -> Movies:
        name = data.get("titles", {}).get("en", "Unknown Title")
        year = int(data["created_at"][:4]) if "created_at" in data else None
        description = data.get("descriptions", {}).get("en", "")
        original_lang_code = data.get("origin", {}).get("language", "en")
        self.video_id = data.get("watch_now", {}).get("id")

        if not self.video_id:
            raise ValueError(f"Could not find a playable video ID for container {self.container_id}.")

        return Movies([
            Movie(
                id_=self.container_id,
                service=self.__class__,
                name=name,
                year=year,
                description=description,
                language=Language.get(original_lang_code),
                data=data,
            )
        ])

    def _parse_series(self, data: dict) -> Series:
        """Parse series metadata and fetch episodes."""
        series_name = data.get("titles", {}).get("en", "Unknown Title")
        year = int(data["created_at"][:4]) if "created_at" in data else None
        description = data.get("descriptions", {}).get("en", "")
        original_lang_code = data.get("origin", {}).get("language", "en")
        
        self.log.info(f"Parsing series: {series_name}")
        
        # Fetch episode list IDs
        episodes_url = self.config["endpoints"]["episodes"].format(container_id=self.container_id)
        params = {
            "app": self.config["params"]["app"],
            "token": self.api_access_key,
            "direction": "asc",
            "with_upcoming": "true",
            "sort": "number",
            "blocked": "true",
            "only_ids": "true"
        }
        
        r = self.session.get(episodes_url, params=params)
        r.raise_for_status()
        episodes_data = r.json()
        
        episode_ids = episodes_data.get("response", [])
        self.log.info(f"Found {len(episode_ids)} episodes")
        
        episodes = []
        for idx, ep_id in enumerate(episode_ids, 1):
            # Fetch individual episode metadata
            ep_url = self.config["endpoints"]["episode_meta"].format(video_id=ep_id)
            ep_params = {
                "app": self.config["params"]["app"],
                "token": self.api_access_key,
            }
            
            try:
                r_ep = self.session.get(ep_url, params=ep_params)
                r_ep.raise_for_status()
                ep_data = r_ep.json()
                
                ep_number = ep_data.get("number", idx)
                ep_title = ep_data.get("titles", {}).get("en", "")
                ep_description = ep_data.get("descriptions", {}).get("en", "")
                
                # If no episode title, use generic name
                if not ep_title:
                    ep_title = f"Episode {ep_number}"
                
                # Store the video_id in the data dict
                ep_data["video_id"] = ep_id
                
                self.log.debug(f"Episode {ep_number}: {ep_title} ({ep_id})")
                
                episodes.append(
                    Episode(
                        id_=ep_id,
                        service=self.__class__,
                        title=series_name,  # Series title
                        season=1,  # VIKI typically doesn't separate seasons clearly
                        number=ep_number,
                        name=ep_title,  # Episode title
                        description=ep_description,
                        language=Language.get(original_lang_code),
                        data=ep_data
                    )
                )
            except Exception as e:
                self.log.warning(f"Failed to fetch episode {ep_id}: {e}")
                # Create a basic episode entry even if metadata fetch fails
                episodes.append(
                    Episode(
                        id_=ep_id,
                        service=self.__class__,
                        title=series_name,
                        season=1,
                        number=idx,
                        name=f"Episode {idx}",
                        description="",
                        language=Language.get(original_lang_code),
                        data={"video_id": ep_id}  # Store video_id in data
                    )
                )
        
        # Return Series with just the episodes list
        return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        # For episodes, get the video_id from the data dict
        if isinstance(title, Episode):
            self.video_id = title.data.get("video_id")
            if not self.video_id:
                # Fallback to episode id if video_id not in data
                self.video_id = title.data.get("id")
        elif not self.video_id:
            raise RuntimeError("video_id not set. Call get_titles() first.")

        if not self.video_id:
            raise ValueError("Could not determine video_id for this title")

        self.log.info(f"Getting tracks for video ID: {self.video_id}")
        
        url = self.config["endpoints"]["playback"].format(video_id=self.video_id)
        r = self.session.get(url)
        r.raise_for_status()
        data = r.json()

        # Get the DRM-protected manifest from queue
        manifest_url = None
        for item in data.get("queue", []):
            if item.get("type") == "video" and item.get("format") == "mpd":
                manifest_url = item.get("url")
                break
        
        if not manifest_url:
            raise ValueError("No DRM-protected manifest URL found in queue")
            
        self.log.debug(f"Found DRM-protected manifest URL: {manifest_url}")
        
        # Create headers for manifest download
        manifest_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
            "Accept": "*/*",
            "Accept-Language": "en",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "X-Viki-App-Ver": "14.64.0",
            "X-Viki-Device-ID": self.session.headers.get("X-Viki-Device-ID", ""),
            "Origin": "https://www.viki.com",
            "Referer": "https://www.viki.com/",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }
        
        # Parse tracks from the DRM-protected manifest
        tracks = DASH.from_url(manifest_url, session=self.session).to_tracks(language=title.language)
        
        # Subtitles
        title_language = title.language.language
        subtitles = []
        for sub in data.get("subtitles", []):
            sub_url = sub.get("src")
            lang_code = sub.get("srclang")
            if not sub_url or not lang_code:
                continue
            
            subtitles.append(
                Subtitle(
                    id_=lang_code,
                    url=sub_url,
                    language=Language.get(lang_code),
                    is_original_lang=lang_code == title_language,
                    codec=Subtitle.Codec.WebVTT,
                    name=sub.get("label", lang_code.upper()).split(" (")[0]
                )
            )
        tracks.subtitles = subtitles

        # Store DRM license URL (only dt3) at service level
        drm_b64 = data.get("drm")
        if drm_b64:
            drm_data = json.loads(base64.b64decode(drm_b64))
            self.drm_license_url = drm_data.get("dt3")  # Use dt3 as requested
        else:
            self.log.warning("No DRM info found, assuming unencrypted stream.")
            
        return tracks

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not hasattr(self, 'drm_license_url') or not self.drm_license_url:
            raise ValueError("DRM license URL not available.")
        
        r = self.session.post(
            self.drm_license_url,
            data=challenge,
            headers={"Content-type": "application/octet-stream"}
        )
        r.raise_for_status()
        return r.content

    def search(self) -> Generator[SearchResult, None, None]:
        self.log.warning("Search not yet implemented for VIKI.")
        return
        yield
    
    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []
