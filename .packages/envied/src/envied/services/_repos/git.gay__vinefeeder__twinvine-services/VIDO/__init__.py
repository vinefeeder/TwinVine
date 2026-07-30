import re
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from hashlib import md5
from typing import Optional, Union
from http.cookiejar import CookieJar
from langcodes import Language

import click

from envied.core.credential import Credential
from envied.core.manifests import HLS, DASH
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Tracks, Subtitle
from envied.core.constants import AnyTrack
from datetime import datetime, timezone


class VIDO(Service):
    """
    Vidio.com service, Series and Movies, login required.
    Version: 2.3.0

    Supports URLs like:
      • https://www.vidio.com/premier/2978/giligilis (Series)
      • https://www.vidio.com/watch/7454613-marantau-short-movie (Movie)

    Security: HD@L3 (Widevine DRM when available)
    """

    TITLE_RE = r"^https?://(?:www\.)?vidio\.com/(?:premier|series|watch)/(?P<id>\d+)"
    GEOFENCE = ("ID",)

    @staticmethod
    @click.command(name="VIDO", short_help="https://vidio.com (login required)")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return VIDO(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)

        match = re.match(self.TITLE_RE, title)
        if not match:
            raise ValueError(f"Unsupported or invalid Vidio URL: {title}")
        self.content_id = match.group("id")
        
        self.is_movie = "watch" in title

        # Static app identifiers from Android traffic
        self.API_AUTH = "laZOmogezono5ogekaso5oz4Mezimew1"
        self.USER_AGENT = "vidioandroid/7.14.6-e4d1de87f2 (3191683)"
        self.API_APP_INFO = "android/15/7.14.6-e4d1de87f2-3191683"
        self.VISITOR_ID = str(uuid.uuid4())

        # Auth state
        self._email = None
        self._user_token = None
        self._access_token = None
        
        # DRM state
        self.license_url = None
        self.custom_data = None
        self.cdm = ctx.obj.cdm

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        if not credential or not credential.username or not credential.password:
            raise ValueError("Vidio requires email and password login.")

        self._email = credential.username
        password = credential.password

        cache_key = f"auth_tokens_{self._email}"
        cache = self.cache.get(cache_key)

        # Check if valid tokens are already in the cache
        if cache and not cache.expired:
            self.log.info("Using cached authentication tokens")
            cached_data = cache.data
            self._user_token = cached_data.get("user_token")
            self._access_token = cached_data.get("access_token")
            if self._user_token and self._access_token:
                return

        # If no valid cache, proceed with login
        self.log.info("Authenticating with username and password")
        headers = {
            "referer": "android-app://com.vidio.android",
            "x-api-platform": "app-android",
            "x-api-auth": self.API_AUTH,
            "user-agent": self.USER_AGENT,
            "x-api-app-info": self.API_APP_INFO,
            "accept-language": "en",
            "content-type": "application/x-www-form-urlencoded",
            "x-visitor-id": self.VISITOR_ID,
        }

        data = f"login={self._email}&password={password}"
        r = self.session.post("https://api.vidio.com/api/login", headers=headers, data=data)
        r.raise_for_status()

        auth_data = r.json()
        self._user_token = auth_data["auth"]["authentication_token"]
        self._access_token = auth_data["auth_tokens"]["access_token"]
        self.log.info(f"Authenticated as {self._email}")

        try:
            expires_at_str = auth_data["auth_tokens"]["access_token_expires_at"]
            expires_at_dt = datetime.fromisoformat(expires_at_str)
            now_utc = datetime.now(timezone.utc)
            expiration_in_seconds = max(0, int((expires_at_dt - now_utc).total_seconds()))
            self.log.info(f"Token expires in {expiration_in_seconds / 60:.2f} minutes.")
        except (KeyError, ValueError) as e:
            self.log.warning(f"Could not parse token expiration: {e}. Defaulting to 1 hour.")
            expiration_in_seconds = 3600

        cache.set({
            "user_token": self._user_token,
            "access_token": self._access_token
        }, expiration=expiration_in_seconds)

    def _headers(self):
        if not self._user_token or not self._access_token:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return {
            "referer": "android-app://com.vidio.android",
            "x-api-platform": "app-android",
            "x-api-auth": self.API_AUTH,
            "user-agent": self.USER_AGENT,
            "x-api-app-info": self.API_APP_INFO,
            "x-visitor-id": self.VISITOR_ID,
            "x-user-email": self._email,
            "x-user-token": self._user_token,
            "x-authorization": self._access_token,
            "accept-language": "en",
            "accept": "application/json",
            "accept-charset": "UTF-8",
            "content-type": "application/vnd.api+json",
        }

    def _extract_subtitles_from_mpd(self, mpd_url: str) -> list[Subtitle]:
        """
        Manually parse the MPD to extract subtitle tracks.
        Handles plain VTT format (for free content).
        """
        subtitles = []
        
        try:
            r = self.session.get(mpd_url)
            r.raise_for_status()
            mpd_content = r.text
            
            # Get base URL for resolving relative paths
            base_url = mpd_url.rsplit('/', 1)[0] + '/'
            
            # Remove namespace for easier parsing
            mpd_content_clean = re.sub(r'\sxmlns="[^"]+"', '', mpd_content)
            root = ET.fromstring(mpd_content_clean)
            
            for adaptation_set in root.findall('.//AdaptationSet'):
                content_type = adaptation_set.get('contentType', '')
                
                if content_type != 'text':
                    continue
                
                lang = adaptation_set.get('lang', 'und')
                
                for rep in adaptation_set.findall('Representation'):
                    mime_type = rep.get('mimeType', '')
                    
                    # Handle plain VTT (free content)
                    if mime_type == 'text/vtt':
                        segment_list = rep.find('SegmentList')
                        if segment_list is not None:
                            for segment_url in segment_list.findall('SegmentURL'):
                                media = segment_url.get('media')
                                if media:
                                    full_url = urljoin(base_url, media)
                                    
                                    # Determine if auto-generated
                                    is_auto = '-auto' in lang
                                    clean_lang = lang.replace('-auto', '')
                                    
                                    subtitle = Subtitle(
                                        id_=md5(full_url.encode()).hexdigest()[0:16],
                                        url=full_url,
                                        codec=Subtitle.Codec.WebVTT,
                                        language=Language.get(clean_lang),
                                        forced=False,
                                        sdh=False,
                                    )
                                    
                                    subtitles.append(subtitle)
                                    self.log.debug(f"Found VTT subtitle: {lang} -> {full_url}")
            
        except Exception as e:
            self.log.warning(f"Failed to extract subtitles from MPD: {e}")
        
        return subtitles

    def get_titles(self) -> Titles_T:
        headers = self._headers()

        if self.is_movie:
            r = self.session.get(f"https://api.vidio.com/api/videos/{self.content_id}/detail", headers=headers)
            r.raise_for_status()
            video_data = r.json()["video"]
            year = None
            if video_data.get("publish_date"):
                try:
                    year = int(video_data["publish_date"][:4])
                except (ValueError, TypeError):
                    pass
            return Movies([
                Movie(
                    id_=video_data["id"],
                    service=self.__class__,
                    name=video_data["title"],
                    description=video_data.get("description", ""),
                    year=year,
                    language=Language.get("id"),
                    data=video_data,
                )
            ])
        else:
            r = self.session.get(f"https://api.vidio.com/content_profiles/{self.content_id}", headers=headers)
            r.raise_for_status()
            root = r.json()["data"]
            series_title = root["attributes"]["title"]

            r_playlists = self.session.get(
                f"https://api.vidio.com/content_profiles/{self.content_id}/playlists",
                headers=headers
            )
            r_playlists.raise_for_status()
            playlists_data = r_playlists.json()

            # Use metadata to identify season playlists
            season_playlist_ids = set()
            if "meta" in playlists_data and "playlist_group" in playlists_data["meta"]:
                for group in playlists_data["meta"]["playlist_group"]:
                    if group.get("type") == "season":
                        season_playlist_ids.update(group.get("playlist_ids", []))

            season_playlists = []
            for pl in playlists_data["data"]:
                playlist_id = int(pl["id"])
                name = pl["attributes"]["name"].lower()
                
                if season_playlist_ids:
                    if playlist_id in season_playlist_ids:
                        season_playlists.append(pl)
                else:
                    if ("season" in name or name == "episode" or name == "episodes") and \
                       "trailer" not in name and "extra" not in name:
                        season_playlists.append(pl)

            if not season_playlists:
                raise ValueError("No season playlists found for this series.")

            def extract_season_number(pl):
                name = pl["attributes"]["name"]
                match = re.search(r"season\s*(\d+)", name, re.IGNORECASE)
                if match:
                    return int(match.group(1))
                elif name.lower() in ["season", "episodes", "episode"]:
                    return 1
                else:
                    return 0

            season_playlists.sort(key=extract_season_number)

            all_episodes = []

            for playlist in season_playlists:
                playlist_id = playlist["id"]
                season_number = extract_season_number(playlist)
                
                if season_number == 0:
                    season_number = 1
                
                self.log.debug(f"Processing playlist '{playlist['attributes']['name']}' as Season {season_number}")

                page = 1
                while True:
                    r_eps = self.session.get(
                        f"https://api.vidio.com/content_profiles/{self.content_id}/playlists/{playlist_id}/videos",
                        params={
                            "page[number]": page,
                            "page[size]": 20,
                            "sort": "order",
                            "included": "upcoming_videos"
                        },
                        headers=headers,
                    )
                    r_eps.raise_for_status()
                    page_data = r_eps.json()

                    for raw_ep in page_data["data"]:
                        attrs = raw_ep["attributes"]
                        ep_number = len([e for e in all_episodes if e.season == season_number]) + 1
                        all_episodes.append(
                            Episode(
                                id_=int(raw_ep["id"]),
                                service=self.__class__,
                                title=series_title,
                                season=season_number,
                                number=ep_number,
                                name=attrs["title"],
                                description=attrs.get("description", ""),
                                language=Language.get("id"),
                                data=raw_ep,
                            )
                        )

                    if not page_data["links"].get("next"):
                        break
                    page += 1

            if not all_episodes:
                raise ValueError("No episodes found in any season.")

            return Series(all_episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        headers = self._headers()
        headers.update({
            "x-device-brand": "samsung",
            "x-device-model": "SM-A525F",
            "x-device-form-factor": "phone",
            "x-device-soc": "Qualcomm SM7125",
            "x-device-os": "Android 15 (API 35)",
            "x-device-android-mpc": "0",
            "x-device-cpu-arch": "arm64-v8a",
            "x-device-platform": "android",
            "x-app-version": "7.14.6-e4d1de87f2-3191683",
        })

        video_id = str(title.id)
        url = f"https://api.vidio.com/api/stream/v1/video_data/{video_id}?initialize=true"

        r = self.session.get(url, headers=headers)
        r.raise_for_status()
        stream = r.json()

        if not isinstance(stream, dict):
            raise ValueError("Vidio returned invalid stream data.")

        # Extract DRM info
        custom_data = stream.get("custom_data") or {}
        license_servers = stream.get("license_servers") or {}
        widevine_data = custom_data.get("widevine") if isinstance(custom_data, dict) else None
        license_url = license_servers.get("drm_license_url") if isinstance(license_servers, dict) else None
        
        # Get stream URLs, check all possible HLS and DASH fields
        # HLS URLs (prefer in this order)
        hls_url = (
            stream.get("stream_hls_url") or 
            stream.get("stream_token_hls_url") or 
            stream.get("stream_token_url")  # This is also HLS (m3u8)
        )
        
        # DASH URLs 
        dash_url = stream.get("stream_dash_url") or stream.get("stream_token_dash_url")
        
        has_drm = widevine_data and license_url and dash_url and isinstance(widevine_data, str)
        
        if has_drm:
            # DRM content: must use DASH
            self.log.info("Widevine DRM detected, using DASH")
            self.custom_data = widevine_data
            self.license_url = license_url
            tracks = DASH.from_url(dash_url, session=self.session).to_tracks(language=title.language)
            
        elif hls_url:
            # Non-DRM: prefer HLS (H.264, proper frame_rate metadata)
            self.log.info("No DRM detected, using HLS")
            self.custom_data = None
            self.license_url = None
            tracks = HLS.from_url(hls_url, session=self.session).to_tracks(language=title.language)
            
            # Clear HLS subtitles (they're segmented and incompatible)
            if tracks.subtitles:
                self.log.debug("Clearing HLS subtitles (incompatible format)")
                tracks.subtitles.clear()
            
            # Get subtitles from DASH manifest (plain VTT) if available
            if dash_url:
                self.log.debug("Extracting subtitles from DASH manifest")
                manual_subs = self._extract_subtitles_from_mpd(dash_url)
                if manual_subs:
                    for sub in manual_subs:
                        tracks.add(sub)
                    self.log.info(f"Added {len(manual_subs)} subtitle tracks from DASH")
                    
        elif dash_url:
            # Fallback to DASH only if no HLS available
            self.log.warning("No HLS available, using DASH (VP9 codec - may have issues)")
            self.custom_data = None
            self.license_url = None
            tracks = DASH.from_url(dash_url, session=self.session).to_tracks(language=title.language)
            
            # Try manual subtitle extraction for non-DRM DASH
            if not tracks.subtitles:
                manual_subs = self._extract_subtitles_from_mpd(dash_url)
                if manual_subs:
                    for sub in manual_subs:
                        tracks.add(sub)
        else:
            raise ValueError("No playable stream (DASH or HLS) available.")

        self.log.info(f"Found {len(tracks.videos)} video tracks, {len(tracks.audio)} audio tracks, {len(tracks.subtitles)} subtitle tracks")
        
        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []

    def search(self):
        raise NotImplementedError("Search not implemented for Vidio.")

    def get_widevine_service_certificate(self, **_) -> Union[bytes, str, None]:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.license_url or not self.custom_data:
            raise ValueError("DRM license info missing.")

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
            "Referer": "https://www.vidio.com/",
            "Origin": "https://www.vidio.com",
            "pallycon-customdata-v2": self.custom_data,
            "Content-Type": "application/octet-stream",
        }

        self.log.debug(f"Requesting Widevine license from: {self.license_url}")
        response = self.session.post(
            self.license_url,
            data=challenge,
            headers=headers
        )

        if not response.ok:
            error_summary = response.text[:200] if response.text else "No response body"
            raise Exception(f"License request failed ({response.status_code}): {error_summary}")

        return response.content

