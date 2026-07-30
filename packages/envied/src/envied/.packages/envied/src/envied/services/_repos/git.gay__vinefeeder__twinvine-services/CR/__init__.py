from hashlib import md5
import re
import time
import uuid
from threading import Lock
from typing import Generator, Optional, Union

import click
import jwt
from langcodes import Language

from envied.core.manifests import DASH
from envied.core.manifests.hls import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.session import session
from envied.core.titles import Episode, Series
from envied.core.tracks import Attachment, Chapters, Tracks
from envied.core.tracks.chapter import Chapter
from envied.core.tracks.subtitle import Subtitle


class CR(Service):
    """
    Service code for Crunchyroll streaming service (https://www.crunchyroll.com).

    \b
    Version: 2.1.0
    Author: sp4rk.y & Dex
    Date: 2026-02-28
    Authorization: Credentials
    Robustness:
        Widevine:
            L3: 1080p, AAC2.0
        PlayReady:
            SL3000: 1080p, AAC2.0
            SL2000: 1080p, AAC2.0
    \b
    Tips:
        - Input should be complete URL or series ID or Episode ID
            https://www.crunchyroll.com/series/GRMG8ZQZR/series-name OR GRMG8ZQZR
            https://www.crunchyroll.com/en-US/watch/G2XU02X75/ryomen-sukuna OR G2XU02X75
        - Supports multiple audio and subtitle languages
        - Device ID is cached for consistent authentication across runs

    \b
    Notes:
        - Uses password-based authentication with token caching
        - Manages concurrent stream limits automatically
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?crunchyroll\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:series|watch)/)?(?P<id>[A-Z0-9]+)"
    LICENSE_LOCK = Lock()
    MAX_CONCURRENT_STREAMS = 3
    ACTIVE_STREAMS: list[tuple[str, str]] = []
    
    # Reverse map for audio_locale to match 2 chars languages in a_lang param
    LANG_MAP: dict = {
        "es-LA": "es-419",
        "ar-ME": "ar-SA",
        "de-DE": "de",
        "en-US": "en",
        "es-ES": "es",
        "fr-FR": "fr",
        "hi-IN": "hi",
        "it-IT": "it",
        "ja-JP": "ja",
        "ko-KR": "kr",
        "ru-RU": "ru",
        "zh-CN": "zh",
        "en-GB": "en"
    }
    
    @staticmethod
    @click.command(name="CR", short_help="https://crunchyroll.com")
    @click.argument("title", type=str, required=True)
    @click.option("-hb", "--high-audio-bitrate", is_flag=True, default=False, help="Fetch audio tracks with highest bitrate (192kbps AAC)")

    @click.pass_context
    def cli(ctx, **kwargs) -> "CR":
        return CR(ctx, **kwargs)

    def __init__(self, ctx, title: str, high_audio_bitrate: bool):
        super().__init__(ctx)
        self.title = title
        self.account_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self.token_expiration: Optional[int] = None
        self.anonymous_id = str(uuid.uuid4())
        self.audio_lang = ctx.parent.params.get("a_lang")
        self.high_audio_bitrate = high_audio_bitrate

        device_cache_key = "cr_device_id"
        cached_device = self.cache.get(device_cache_key)

        if cached_device and not cached_device.expired:
            self.device_id = cached_device.data["device_id"]
        else:
            self.device_id = str(uuid.uuid4())
            cached_device.set(
                data={"device_id": self.device_id},
                expiration=60 * 60 * 24 * 365 * 10,
            )

        self.device_name = self.config.get("device", {}).get("name", "SHIELD Android TV")
        self.device_type = self.config.get("device", {}).get("type", "ANDROIDTV")

        self.session.headers.update(self.config.get("headers", {}))
        self.session.headers["etp-anonymous-id"] = self.anonymous_id

    @staticmethod
    def get_session():
        return session("okhttp4")

    @property
    def auth_header(self) -> dict:
        """Return authorization header dict."""
        return {"authorization": f"Bearer {self.access_token}"}

    def ensure_authenticated(self) -> None:
        """Check if token is expired and re-authenticate if needed."""
        if not self.token_expiration:
            cache_key = f"cr_auth_token_{self.credential.sha1 if self.credential else 'default'}"
            cached = self.cache.get(cache_key)

            if cached and not cached.expired:
                self.access_token = cached.data["access_token"]
                self.account_id = cached.data.get("account_id")
                self.token_expiration = cached.data.get("token_expiration")
                self.session.headers.update(self.auth_header)
                self.log.debug("Loaded authentication from cache")
            else:
                self.log.debug("No valid cached token, authenticating")
                self.authenticate(credential=self.credential)
                return

        current_time = int(time.time())
        if current_time >= (self.token_expiration - 60):
            self.log.debug("Authentication token expired or expiring soon, re-authenticating")
            self.authenticate(credential=self.credential)

    def authenticate(self, cookies=None, credential=None) -> None:
        """Authenticate using username and password credentials."""
        super().authenticate(cookies, credential)

        cache_key = f"cr_auth_token_{credential.sha1 if credential else 'default'}"
        cached = self.cache.get(cache_key)

        if cached and not cached.expired:
            self.access_token = cached.data["access_token"]
            self.account_id = cached.data.get("account_id")
            self.token_expiration = cached.data.get("token_expiration")
        else:
            if not credential:
                raise ValueError("Username and password credential required for authentication")

            response = self.session.post(
                url=self.config["endpoints"]["token"],
                headers={
                    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "request-type": "SignIn",
                },
                data={
                    "grant_type": "password",
                    "username": credential.username,
                    "password": credential.password,
                    "scope": "offline_access",
                    "client_id": self.config["client"]["id"],
                    "client_secret": self.config["client"]["secret"],
                    "device_type": self.device_type,
                    "device_id": self.device_id,
                    "device_name": self.device_name,
                },
            )

            if response.status_code != 200:
                self.log.error(f"Login failed: {response.status_code}")
                try:
                    error_data = response.json()
                    error_msg = error_data.get("error", "Unknown error")
                    error_code = error_data.get("code", "")
                    self.log.error(f"Error: {error_msg} ({error_code})")
                except Exception:
                    self.log.error(f"Response: {response.text}")
                response.raise_for_status()

            token_data = response.json()
            self.access_token = token_data["access_token"]
            self.account_id = self.get_account_id()

            try:
                decoded_token = jwt.decode(self.access_token, options={"verify_signature": False})
                self.token_expiration = decoded_token.get("exp")
            except Exception:
                self.token_expiration = int(time.time()) + token_data.get("expires_in", 3600)

            cached.set(
                data={
                    "access_token": self.access_token,
                    "account_id": self.account_id,
                    "token_expiration": self.token_expiration,
                },
                expiration=self.token_expiration
                if isinstance(self.token_expiration, int) and self.token_expiration > int(time.time())
                else 3600,
            )

        self.session.headers.update(self.auth_header)

        if self.ACTIVE_STREAMS:
            self.ACTIVE_STREAMS.clear()

        try:
            self.clear_all_sessions()
        except Exception as e:
            self.log.warning(f"Failed to clear previous sessions: {e}")

    def get_titles(self) -> Union[Series]:
        """Fetch series and episode information."""
        series_id = self.parse_series_id(self.title)

        series_response = self.session.get(
            url=self.config["endpoints"]["series"].format(series_id=series_id),
            params={"locale": self.config["params"]["locale"]},
        ).json()

        if "code" in series_response:
            # Fallback to try with episode id
            episode_response = self.session.get(
                url=self.config["endpoints"]["episodes"].format(episode_id=series_id),
                params={"locale": self.config["params"]["locale"]},
            ).json()

            if "code" in episode_response:
                raise ValueError(f"Series or episode not found: {series_id}")

            # Extract series id from episodes response
            series_id = episode_response["data"][0]["episode_metadata"]["series_id"]

            series_response = self.session.get(
                url=self.config["endpoints"]["series"].format(series_id=series_id),
                params={"locale": self.config["params"]["locale"]},
            ).json()

            if "code" in series_response:
                raise ValueError(f"Series not found from episode metadata: {series_id}")
            
        series_data = (
            series_response.get("data", [{}])[0] if isinstance(series_response.get("data"), list) else series_response
        )
        series_title = series_data.get("title", "Unknown Series")

        seasons_response = self.session.get(
            url=self.config["endpoints"]["seasons"].format(series_id=series_id),
            params={"locale": self.config["params"]["locale"]},
        ).json()

        seasons_data = seasons_response.get("data", [])

        if not seasons_data:
            raise ValueError(f"No seasons found for series: {series_id}")

        all_episode_data = []
        special_episodes = []

        for season in seasons_data:
            season_id = season["id"]
            season_number = season.get("season_number", 0)

            episodes_response = self.session.get(
                url=self.config["endpoints"]["season_episodes"].format(season_id=season_id),
                params={"locale": self.config["params"]["locale"]},
            ).json()

            episodes_data = episodes_response.get("data", [])

            for episode_data in episodes_data:
                episode_number = episode_data.get("episode_number")

                if episode_number is None or isinstance(episode_number, float):
                    special_episodes.append(episode_data)

                all_episode_data.append((episode_data, season_number))

        if not all_episode_data:
            raise ValueError(f"No episodes found for series: {series_id}")

        series_year = None
        if all_episode_data:
            first_episode_data = all_episode_data[0][0]
            first_air_date = first_episode_data.get("episode_air_date")
            if first_air_date:
                series_year = int(first_air_date[:4])

        special_episodes.sort(key=lambda x: x.get("episode_air_date", ""))
        special_episode_numbers = {ep["id"]: idx + 1 for idx, ep in enumerate(special_episodes)}
        episodes = []
        season_episode_counts = {}

        for episode_data, season_number in all_episode_data:
            episode_number = episode_data.get("episode_number")

            if episode_number is None or isinstance(episode_number, float):
                final_season = 0
                final_number = special_episode_numbers[episode_data["id"]]
            else:
                final_season = season_number
                if final_season not in season_episode_counts:
                    season_episode_counts[final_season] = 0

                season_episode_counts[final_season] += 1
                final_number = season_episode_counts[final_season]

            original_language = None
            versions = episode_data.get("versions", [])
            for version in versions:
                if "main" in version.get("roles", []):
                    original_language = version.get("audio_locale")
                    break

            episode = Episode(
                id_=episode_data["id"],
                service=self.__class__,
                title=series_title,
                season=final_season,
                number=final_number,
                name=episode_data.get("title"),
                year=series_year,
                language=original_language,
                description=episode_data.get("description"),
                data=episode_data,
            )
            episodes.append(episode)

        return Series(episodes)

    def set_track_metadata(self, tracks: Tracks, episode_id: str, is_original: bool) -> None:
        """Set metadata for video and audio tracks."""
        for video in tracks.videos:
            video.needs_repack = True
            video.data["episode_id"] = episode_id
            video.is_original_lang = is_original
        for audio in tracks.audio:
            audio.data["episode_id"] = episode_id
            audio.is_original_lang = is_original
            
    def get_tracks(self, title: Episode) -> Tracks:
        """Fetch video, audio, and subtitle tracks for an episode."""
        self.ensure_authenticated()

        episode_id = title.id

        if self.ACTIVE_STREAMS:
            self.ACTIVE_STREAMS.clear()

        self.clear_all_sessions()

        initial_response = self.get_playback_data(episode_id, track_stream=False)
        versions = initial_response.get("versions", [])

        if not versions:
            self.log.warning("No versions found in playback response, using single version")
            versions = [{"audio_locale": initial_response.get("audioLocale", "ja-JP")}]

        tracks = None

        #endpoints_playback = ["playback"]
        
        sdh_list = []
        for idx, version in enumerate(versions):
            audio_locale = version.get("audio_locale")
            version_guid = version.get("guid")
            is_original = version.get("original", False)

            # Verify mapped audio locale to match CR locales
            mapped_locale = self.LANG_MAP.get(audio_locale, audio_locale)
            if not self.audio_lang or "all" in self.audio_lang or "best" in self.audio_lang or ("orig" in self.audio_lang and is_original):
                pass
            elif not audio_locale or (audio_locale not in self.audio_lang and mapped_locale not in self.audio_lang):
                continue

            request_episode_id = version_guid if version_guid else episode_id
            
            #for endpoint in endpoints_playback:
            try:
                if idx == 0 and not version_guid: # and endpoint == "playback":
                    version_response = initial_response
                    version_token = version_response.get("token")
                else:
                    if idx == 1 and not versions[0].get("guid"): # and endpoint == "playback":
                        initial_token = initial_response.get("token")
                        if initial_token:
                            self.close_stream(episode_id, initial_token)

                    try:
                        version_response = self.get_playback_data(request_episode_id, track_stream=False)
                    except ValueError as e:
                        self.log.warning(f"Could not get playback info for audio {audio_locale}: {e}")
                        continue

                    version_token = version_response.get("token")

                hard_subs = version_response.get("hardSubs", {})
                dash_url = None
                root_url = version_response.get("url")
                
                if root_url and  "/clean/" in root_url:
                    dash_url = root_url
                    
                elif "none" in hard_subs:
                    dash_url = hard_subs["none"].get("url")
                    
                elif hard_subs:
                    first_key = list(hard_subs.keys())[0]
                    dash_url = hard_subs[first_key].get("url")

                if not dash_url:
                    self.log.warning(f"No DASH manifest found for audio {audio_locale}, skipping")
                    if version_token:
                        self.close_stream(request_episode_id, version_token)
                    continue
                
                try:

                    version_tracks = (HLS if "m3u8" in dash_url else DASH).from_url(
                        url=dash_url,
                        session=self.session,
                    ).to_tracks(language=audio_locale)
                    
                    # Select high audio bitrate replacing 0 and 1 in mpd url
                    if self.high_audio_bitrate:
                        try:
                            version_tracks.audio.clear();
                            
                            high_bitrate_tracks = (HLS if "m3u8" in dash_url else DASH).from_url(
                                url=dash_url.replace("/0/", "/1/"),
                                session=self.session,
                            ).to_tracks(language=audio_locale)
                            
                            version_tracks.audio = high_bitrate_tracks.audio
                        except Exception as e:
                            self.log.warning(f"Failed to fetch high bitrate audio for {audio_locale}: {e}")
                        
                    if tracks is None:
                        tracks = version_tracks
                        self.set_track_metadata(tracks, request_episode_id, is_original)
                    else:
                        self.set_track_metadata(version_tracks, request_episode_id, is_original)
                        # Add videos 
                        for video in version_tracks.videos:
                            if not any(v.id == video.id for v in tracks.videos):
                                tracks.add(video)
                                
                        # Add audios non-existing in tracks or with higher bitrate
                        for audio in version_tracks.audio:
                            if audio.channels == "1.0":
                                audio.channels = "2.0" # change audio channels for correct channel
                                
                            existing_audio = next((a for a in tracks.audio if a.language == audio.language), None)
                            if existing_audio is None or (hasattr(audio, 'bitrate') and hasattr(existing_audio, 'bitrate') and audio.bitrate > existing_audio.bitrate):
                                tracks.add(audio)
                            elif existing_audio is None:
                                tracks.add(audio)

                except Exception as e:
                    self.log.warning(f"Failed to parse DASH manifest for audio {audio_locale}: {e}")
                    if version_token:
                        self.close_stream(request_episode_id, version_token)
                    continue

                #if endpoint == "playback":
                # Handling subtitles code
                try:
                    lang_audio = [x for x in version_response.get("versions", []) if x["guid"] == version_guid][0]["audio_locale"]
                except (IndexError, KeyError, TypeError):
                    lang_audio = title.language
                    
                captions = version_response.get("captions", {})
                subtitles_data = version_response.get("subtitles", {})
                
                if captions:
                    for subtitle in captions.values():
                        
                        if subtitle["language"] == "none":
                            continue
                        
                        lang = subtitle["language"]
                        subtitle_format = subtitle.get("format", "vtt").lower()
                        codec = Subtitle.Codec.SubStationAlphav4 if subtitle_format in ["ass", "ssa"] else Subtitle.Codec.WebVTT
                        sdh_list.append(lang)
                        tracks.add(
                            Subtitle(
                                id_=md5(subtitle["url"].encode()).hexdigest()[0:6],
                                url=subtitle["url"],
                                codec=codec,
                                language=Language.get(lang),
                                sdh=True,
                            ),
                            warn_only=True,
                        )
                    
                if subtitles_data:
                    for subtitle in subtitles_data.values():
                        if subtitle["language"] == "none":
                            continue
                        
                        lang = subtitle["language"]
                        subtitle_format = subtitle.get("format", "vtt").lower()
                        codec = Subtitle.Codec.SubStationAlphav4 if subtitle_format in ["ass", "ssa"] else Subtitle.Codec.WebVTT
                        tracks.add(
                            Subtitle(
                                id_=md5(subtitle["url"].encode()).hexdigest()[0:6],
                                url=subtitle["url"],
                                codec=codec,
                                language=Language.get(lang),
                                forced=False if (str(lang_audio) == str(title.language)) and (lang not in sdh_list) else True,
                                sdh=False,
                            ),
                            warn_only=True,
                        )

                if version_token:
                    self.close_stream(request_episode_id, version_token)
                    
            # continuar si no se pudo procesar el endpoint
            except Exception as e:
                self.log.warning(f"Error processing version {idx}: {e}")
                continue
            
        if versions and versions[0].get("guid"):
            initial_token = initial_response.get("token")
            if initial_token:
                self.close_stream(episode_id, initial_token)

        if tracks is None:
            raise ValueError(f"Failed to fetch any tracks for episode: {episode_id}")


        images = title.data.get("images", {})
        thumbnails = images.get("thumbnail", [])
        if thumbnails:
            thumb_variants = thumbnails[0] if isinstance(thumbnails[0], list) else [thumbnails[0]]
            if thumb_variants:
                thumb_index = min(7, len(thumb_variants) - 1)
                thumb = thumb_variants[thumb_index]
                if isinstance(thumb, dict) and "source" in thumb:
                    raw_name = f"{title.name or title.title} - S{title.season:02d}E{title.number:02d}"
                    thumbnail_name = self.sanitize_filename(raw_name)
                    tracks.add(Attachment.from_url(url=thumb["source"], name=thumbnail_name))

        return tracks

    def get_playready_license(self, challenge: bytes, title: Episode, track) -> bytes:
        """
        Get PlayReady license for decryption.

        Creates a fresh playback session for each track, gets the license, then immediately
        closes the stream. This prevents hitting the 3 concurrent stream limit.
        CDN authorization is embedded in the manifest URLs, not tied to active sessions.
        """
        self.ensure_authenticated()

        track_episode_id = track.data.get("episode_id", title.id)

        with self.LICENSE_LOCK:
            playback_token = None
            try:
                playback_data = self.get_playback_data(track_episode_id, track_stream=True)
                playback_token = playback_data.get("token")

                if not playback_token:
                    raise ValueError(f"No playback token in response for {track_episode_id}")

                track.data["playback_token"] = playback_token

                license_response = self.session.post(
                    url=self.config["endpoints"]["license_playready"],
                    params={"specConform": "true"},
                    data=challenge,
                    headers={
                        **self.auth_header,
                        "content-type": "application/octet-stream",
                        "accept": "application/octet-stream",
                        "x-cr-content-id": track_episode_id,
                        "x-cr-video-token": playback_token,
                    },
                )

                if license_response.status_code != 200:
                    self.log.error(f"License request failed with status {license_response.status_code}")
                    self.log.error(f"Response: {license_response.text[:500]}")
                    self.close_stream(track_episode_id, playback_token)
                    raise ValueError(f"License request failed: {license_response.status_code}")

                self.close_stream(track_episode_id, playback_token)
                return license_response.content

            except Exception:
                if playback_token:
                    try:
                        self.close_stream(track_episode_id, playback_token)
                    except Exception:
                        pass
                raise
            
    def get_widevine_license(self, challenge: bytes, title: Episode, track) -> bytes:
        """
        Get Widevine license for decryption.

        Creates a fresh playback session for each track, gets the license, then immediately
        closes the stream. This prevents hitting the 3 concurrent stream limit.
        CDN authorization is embedded in the manifest URLs, not tied to active sessions.
        """
        self.ensure_authenticated()

        track_episode_id = track.data.get("episode_id", title.id)

        with self.LICENSE_LOCK:
            playback_token = None
            try:
                playback_data = self.get_playback_data(track_episode_id, track_stream=True)
                playback_token = playback_data.get("token")

                if not playback_token:
                    raise ValueError(f"No playback token in response for {track_episode_id}")

                track.data["playback_token"] = playback_token

                license_response = self.session.post(
                    url=self.config["endpoints"]["license_widevine"],
                    params={"specConform": "true"},
                    data=challenge,
                    headers={
                        **self.auth_header,
                        "content-type": "application/octet-stream",
                        "accept": "application/octet-stream",
                        "x-cr-content-id": track_episode_id,
                        "x-cr-video-token": playback_token,
                    },
                )

                if license_response.status_code != 200:
                    self.log.error(f"License request failed with status {license_response.status_code}")
                    self.log.error(f"Response: {license_response.text[:500]}")
                    self.close_stream(track_episode_id, playback_token)
                    raise ValueError(f"License request failed: {license_response.status_code}")

                self.close_stream(track_episode_id, playback_token)
                return license_response.content

            except Exception:
                if playback_token:
                    try:
                        self.close_stream(track_episode_id, playback_token)
                    except Exception:
                        pass
                raise

    def cleanup_active_streams(self) -> None:
        """
        Close all remaining active streams.
        Called to ensure no streams are left open.
        """
        if self.ACTIVE_STREAMS:
            try:
                self.authenticate()
            except Exception as e:
                self.log.warning(f"Failed to re-authenticate during cleanup: {e}")

            for episode_id, token in list(self.ACTIVE_STREAMS):
                try:
                    self.close_stream(episode_id, token)
                except Exception as e:
                    self.log.warning(f"Failed to close stream {episode_id}: {e}")
                    if (episode_id, token) in self.ACTIVE_STREAMS:
                        self.ACTIVE_STREAMS.remove((episode_id, token))

    def __del__(self) -> None:
        """Cleanup any remaining streams when service is destroyed."""
        try:
            self.cleanup_active_streams()
        except Exception:
            pass

    def get_chapters(self, title: Episode) -> Chapters:
        """Get chapters/skip events for an episode."""
        chapters = Chapters()

        chapter_response = self.session.get(
            url=self.config["endpoints"]["skip_events"].format(episode_id=title.id),
        )

        if chapter_response.status_code == 200:
            try:
                chapter_data = chapter_response.json()
            except Exception as e:
                self.log.warning(f"Failed to parse chapter data: {e}")
                return chapters

            for chapter_type in ["intro", "recap", "credits", "preview"]:
                if chapter_info := chapter_data.get(chapter_type):
                    try:
                        chapters.add(
                            Chapter(
                                timestamp=int(chapter_info["start"] * 1000),
                                name=chapter_info["type"].capitalize(),
                            )
                        )
                    except Exception as e:
                        self.log.debug(f"Failed to add {chapter_type} chapter: {e}")

        return chapters

    def search(self) -> Generator[SearchResult, None, None]:
        """Search for content on Crunchyroll."""
        try:
            response = self.session.get(
                url=self.config["endpoints"]["search"],
                params={
                    "q": self.title,
                    "type": "series",
                    "start": 0,
                    "n": 20,
                    "locale": self.config["params"]["locale"],
                },
            )

            if response.status_code != 200:
                self.log.error(f"Search request failed with status {response.status_code}")
                return

            search_data = response.json()
            for result_group in search_data.get("data", []):
                for series in result_group.get("items", []):
                    series_id = series.get("id")

                    if not series_id:
                        continue

                    title = series.get("title", "Unknown")
                    description = series.get("description", "")
                    year = series.get("series_launch_year")
                    if len(description) > 300:
                        description = description[:300] + "..."

                    url = f"https://www.crunchyroll.com/series/{series_id}"
                    label = f"SERIES ({year})" if year else "SERIES"

                    yield SearchResult(
                        id_=series_id,
                        title=title,
                        label=label,
                        description=description,
                        url=url,
                    )

        except Exception as e:
            self.log.error(f"Search failed: {e}")
            return

    def get_account_id(self) -> str:
        """Fetch and return the account ID."""
        response = self.session.get(url=self.config["endpoints"]["account_me"], headers=self.auth_header)

        if response.status_code != 200:
            self.log.error(f"Failed to get account info: {response.status_code}")
            self.log.error(f"Response: {response.text}")
            response.raise_for_status()

        data = response.json()
        return data["account_id"]

    def close_stream(self, episode_id: str, token: str) -> None:
        """Close an active playback stream to free up concurrent stream slots."""
        should_remove = False
        try:
            response = self.session.delete(
                url=self.config["endpoints"]["playback_delete"].format(episode_id=episode_id, token=token),
                headers=self.auth_header,
            )
            if response.status_code in (200, 204, 403):
                should_remove = True
            else:
                self.log.error(
                    f"Failed to close stream for {episode_id} (status {response.status_code}): {response.text[:200]}"
                )
        except Exception as e:
            self.log.error(f"Error closing stream for {episode_id}: {e}")
        finally:
            if should_remove and (episode_id, token) in self.ACTIVE_STREAMS:
                self.ACTIVE_STREAMS.remove((episode_id, token))

    def get_active_sessions(self) -> list:
        """Get all active streaming sessions for the account."""
        try:
            response = self.session.get(
                url=self.config["endpoints"]["playback_sessions"],
                headers=self.auth_header,
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("items", [])
            else:
                self.log.warning(f"Failed to get active sessions (status {response.status_code})")
                return []
        except Exception as e:
            self.log.warning(f"Error getting active sessions: {e}")
            return []

    def clear_all_sessions(self) -> int:
        """
        Clear all active streaming sessions created during this or previous runs.

        Tries multiple approaches to ensure all streams are closed:
        1. Clear tracked streams with known tokens
        2. Query active sessions API and close all found streams
        3. Try alternate token formats if needed
        """
        cleared = 0

        if self.ACTIVE_STREAMS:
            streams_to_close = self.ACTIVE_STREAMS[:]
            for episode_id, playback_token in streams_to_close:
                try:
                    self.close_stream(episode_id, playback_token)
                    cleared += 1
                except Exception:
                    if (episode_id, playback_token) in self.ACTIVE_STREAMS:
                        self.ACTIVE_STREAMS.remove((episode_id, playback_token))

        sessions = self.get_active_sessions()
        if sessions:
            for session_data in sessions:
                content_id = session_data.get("contentId")
                session_token = session_data.get("token")

                if content_id and session_token:
                    tokens_to_try = (
                        ["11-" + session_token[3:], session_token]
                        if session_token.startswith("08-")
                        else [session_token]
                    )

                    session_closed = False
                    for token in tokens_to_try:
                        try:
                            response = self.session.delete(
                                url=self.config["endpoints"]["playback_delete"].format(
                                    episode_id=content_id, token=token
                                ),
                                headers=self.auth_header,
                            )
                            if response.status_code in (200, 204):
                                cleared += 1
                                session_closed = True
                                break
                            elif response.status_code == 403:
                                session_closed = True
                                break
                        except Exception:
                            pass

                    if not session_closed:
                        self.log.warning(f"Unable to close session {content_id} with any token format")

        return cleared

    def get_playback_data(self, episode_id: str, track_stream: bool = True) -> dict:
        """
        Get playback data for an episode with automatic retry on stream limits.

        Args:
            episode_id: The episode ID to get playback data for
            track_stream: Whether to track this stream in active_streams (False for temporary streams)

        Returns:
            dict: The playback response data

        Raises:
            ValueError: If playback request fails after retry
        """
        self.ensure_authenticated()

        max_retries = 2
        for attempt in range(max_retries + 1):
            response = self.session.get(
                url=self.config["endpoints"]["playback"].format(episode_id=episode_id),
                params={"queue": "0"},
            )

            # Handle 429 response status code before json parsing
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                self.log.warning(f"Rate limited (429). Waiting {retry_after}s before retry... (attempt {attempt + 1}/{max_retries + 1})")
                if attempt < max_retries:
                    cleared = self.clear_all_sessions()
                    time.sleep(retry_after)
                    continue
                raise ValueError(f"Rate limit exceeded for episode: {episode_id}")

            response = response.json()
            
            if "code" in response:
                error_code = response.get("code", "")
                error_msg = response.get("message", response.get("error", "Unknown error"))

                if error_code == "TOO_MANY_ACTIVE_STREAMS" and attempt < max_retries:
                    self.log.warning(f"Hit stream limit: {error_msg}")
                    cleared = self.clear_all_sessions()

                    if cleared == 0 and attempt == 0:
                        wait_time = 30
                        self.log.warning(
                            f"Found orphaned sessions from previous run. Waiting {wait_time}s for them to expire..."
                        )
                        time.sleep(wait_time)

                    continue

                self.log.error(f"Playback API error: {error_msg}")
                self.log.debug(f"Full response: {response}")
                raise ValueError(f"Could not get playback info for episode: {episode_id} - {error_msg}")

            playback_token = response.get("token")
            if playback_token and track_stream:
                self.ACTIVE_STREAMS.append((episode_id, playback_token))

            return response

        raise ValueError(f"Failed to get playback data for episode: {episode_id}")

    def parse_series_id(self, title_input: str) -> str:
        """Parse series ID from URL or direct ID input."""
        match = re.match(self.TITLE_RE, title_input, re.IGNORECASE)
        if not match:
            raise ValueError(f"Could not parse series ID from: {title_input}")
        return match.group("id")
    
    @staticmethod
    def sanitize_filename(name):
        return re.sub(r'[<>:"/\\|?*¿¡]', '', name).strip()