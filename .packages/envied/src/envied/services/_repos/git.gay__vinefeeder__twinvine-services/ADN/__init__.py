import base64
import json
import re
import time
import uuid
import subprocess
import tempfile
import shutil
from typing import Generator, Optional, Union, List, Any
from pathlib import Path
from functools import partial

import click
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from langcodes import Language

from envied.core import binaries
from envied.core.config import config
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.session import session
from envied.core.titles import Episode, Series
from envied.core.tracks import Tracks, Chapters, Chapter
from envied.core.tracks.audio import Audio
from envied.core.tracks.video import Video
from envied.core.tracks.audio import Audio
from envied.core.tracks.subtitle import Subtitle


class VideoNoAudio(Video):
    """
    Video track that automatically removes audio after recording.
    Necessary because ADN provides HLS streams with muxed audio.
    """
    
    def download(self, session, prepare_drm, max_workers=None, progress=None, *, cdm=None):
        """Override: download then demux to remove audio."""
        import logging
        log = logging.getLogger('ADN.VideoNoAudio')
        
        # Normal download
        super().download(session, prepare_drm, max_workers, progress, cdm=cdm)
        
        # If no path, download failed
        if not self.path or not self.path.exists():
            return
        
        # Check FFmpeg available
        if not binaries.FFMPEG:
            log.warning("FFmpeg not found, cannot remove audio from video")
            return
        
        # Demux: remove audio
        if progress:
            progress(downloaded="Removing audio")
        
        original_path = self.path
        noaudio_path = original_path.with_stem(f"{original_path.stem}_noaudio")
        
        try:
            log.debug(f"Removing audio from {original_path.name}")
            
            result = subprocess.run(
                [
                    binaries.FFMPEG,
                    '-i', str(original_path),
                    '-vcodec', 'copy',  # Copy video without re-encoding
                    '-an',              # Remove audio
                    '-y',
                    str(noaudio_path)
                ],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                log.error(f"FFmpeg demux failed: {result.stderr}")
                noaudio_path.unlink(missing_ok=True)
                return
            
            if not noaudio_path.exists() or noaudio_path.stat().st_size < 1000:
                log.error("Demuxed video is empty or too small")
                noaudio_path.unlink(missing_ok=True)
                return
            
            # Replace original file
            log.debug(f"Video demuxed successfully: {noaudio_path.stat().st_size} bytes")
            original_path.unlink()
            noaudio_path.rename(original_path)
            
            if progress:
                progress(downloaded="Downloaded")
                
        except subprocess.TimeoutExpired:
            log.error("FFmpeg demux timeout")
            noaudio_path.unlink(missing_ok=True)
        except Exception as e:
            log.error(f"Failed to demux video: {e}")
            noaudio_path.unlink(missing_ok=True)


class AudioExtracted(Audio):
    """
    Audio track already extracted from muxed HLS stream.
    Override download() to copy the file instead of downloading.
    """
    
    def __init__(self, *args, extracted_path: Path, **kwargs):
        # Empty URL to prevent curl from trying to download
        super().__init__(*args, url="", **kwargs)
        self.extracted_path = extracted_path
    
    def download(self, session, prepare_drm, max_workers=None, progress=None, *, cdm=None):
        """Override: copies the extracted file instead of downloading."""
        if not self.extracted_path or not self.extracted_path.exists():
            if progress:
                progress(downloaded="[red]FAILED")
            raise ValueError(f"Extracted audio file not found: {self.extracted_path}")
        
        # Create destination path (same logic as Track.download)
        track_type = self.__class__.__name__
        save_path = config.directories.temp / f"{track_type}_{self.id}.m4a"
        
        if progress:
            progress(downloaded="Copying", total=100, completed=0)
        
        # Copy the extracted file to the final destination
        config.directories.temp.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.extracted_path, save_path)
        
        self.path = save_path
        
        if progress:
            progress(downloaded="Downloaded", completed=100)


class SubtitleEmbedded(Subtitle):
    """
    Subtitle with embedded content (data URI).
    Override download() to write the content directly.
    """
    
    def __init__(self, *args, embedded_content: str, **kwargs):
        # Empty URL to prevent curl from trying to download
        super().__init__(*args, url="", **kwargs)
        self.embedded_content = embedded_content
    
    def download(self, session, prepare_drm, max_workers=None, progress=None, *, cdm=None):
        """Override: writes the embedded content instead of downloading."""
        if not self.embedded_content:
            if progress:
                progress(downloaded="[red]FAILED")
            raise ValueError("No embedded content in subtitle")
        
        # Create destination path
        track_type = "Subtitle"
        save_path = config.directories.temp / f"{track_type}_{self.id}.{self.codec.extension}"
        
        if progress:
            progress(downloaded="Writing", total=100, completed=0)
        
        # Write content
        config.directories.temp.mkdir(parents=True, exist_ok=True)
        save_path.write_text(self.embedded_content, encoding='utf-8')
        
        self.path = save_path
        
        if progress:
            progress(downloaded="Downloaded", completed=100)


class ADN(Service):
    """
    Service code for Animation Digital Network (ADN).

    \b
    Version: 3.2.1 (FINAL - Full multi-audio/subtitle support with custom Track classes)
    Authorization: Credentials
    Robustness:
        Video: Clear HLS (Highest Quality)
        Audio: Pre-extracted from muxed streams with AudioExtracted class
        Subs: AES-128 Encrypted JSON -> ASS format with SubtitleEmbedded class
    
    Technical Solution:
    - ADN provides HLS streams with muxed video+audio (not separable)
    - AudioExtracted: Extracts audio in get_tracks(), copies during download()
    - SubtitleEmbedded: Decrypts and converts to ASS, writes during download()
    - Result: MKV with 1 video + multiple audio tracks + subtitles
    
    Custom Track Classes:
    - AudioExtracted: Bypasses curl file:// limitation with direct file copy
    - SubtitleEmbedded: Bypasses requests data: limitation with direct write
    Made by: guilara_tv
    """

    

    RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCbQrCJBRmaXM4gJidDmcpWDssg
numHinCLHAgS4buMtdH7dEGGEUfBofLzoEdt1jqcrCDT6YNhM0aFCqbLOPFtx9cg
/X2G/G5bPVu8cuFM0L+ehp8s6izK1kjx3OOPH/kWzvstM5tkqgJkNyNEvHdeJl6
KhS+IFEqwvZqgbBpKuwIDAQAB
-----END PUBLIC KEY-----"""

    TITLE_RE = r"^(?:https?://(?:www\.)?animationdigitalnetwork\.com/video/[^/]+/)?(?P<id>\d+)"

    @staticmethod
    def get_session():
        return session("okhttp4")

    @staticmethod
    @click.command(
        name="ADN", 
        short_help="https://animationdigitalnetwork.com",
        help=(
            "Downloads series or movies from ADN.\n\n"
            "TITLE: Series URL or ID (eg. 1125).\n\n"
            "SELECTION SYSTEM:\n"
            "  - Simple: '-e 1-5' (episodes 1 to 5)\n"
            "  - Seasons: '-e S2' or '-e S02' (all season 2) or '-e S2E1-12'\n"
            "  - Mixed: '-e 1,3,S2E5' or '-e 1,3,S02E05'\n"
            "  - Bonus: '-e NC1,OAV1'"
        )
    )
    @click.argument("title", type=str, required=True)
    @click.option(
        "-e", "--episode", "select", type=str,
        help="Selection: numbers, ranges (5-10), seasons (S1, S2) or combined (S1E5)."
    )
    @click.option(
        "--but", is_flag=True,
        help="Invert selection: download everything EXCEPT episodes specified with -e."
    )
    @click.option(
        "--all", "all_eps", is_flag=True,
        help="Ignore all restrictions and download the entire series."
    )
    @click.pass_context
    def cli(ctx, **kwargs) -> "ADN":
        return ADN(ctx, **kwargs)

    def __init__(self, ctx, title: str, select: Optional[str] = None, but: bool = False, all_eps: bool = False):
        self.title = title
        self.select_str = select
        self.but = but
        self.all_eps = all_eps
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiration: Optional[int] = None
        
        super().__init__(ctx)

        self.locale = self.config.get("params", {}).get("locale", "fr")
        self.session.headers.update(self.config.get("headers", {}))
        self.session.headers["x-target-distribution"] = self.locale


    @staticmethod
    def _timecode_to_ms(tc: str) -> int:
        """Convert HH:MM:SS timecode to milliseconds."""
        parts = tc.split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
        return (hours * 3600 + minutes * 60 + seconds) * 1000

    @property
    def auth_header(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "X-Access-Token": self.access_token
        }

    def ensure_authenticated(self) -> None:
        """Checks the token and refreshes if necessary."""
        current_time = int(time.time())

        if self.access_token and self.token_expiration and current_time < (self.token_expiration - 60):
            return

        cache_key = f"adn_auth_{self.credential.sha1 if self.credential else 'default'}"
        cached = self.cache.get(cache_key)

        if cached and not cached.expired:
            self.access_token = cached.data["access_token"]
            self.refresh_token = cached.data["refresh_token"]
            self.token_expiration = cached.data["token_expiration"]
            self.session.headers.update(self.auth_header)
            self.log.debug("Loaded authentication from cache")
        else:
            self.authenticate(credential=self.credential)

    def authenticate(self, cookies=None, credential=None) -> None:
        super().authenticate(cookies, credential)

        if self.refresh_token:
            try:
                self._do_refresh()
                return
            except Exception:
                self.log.warning("Refresh failed, proceeding to full login")

        if not credential:
            raise ValueError("Credentials required for ADN")

        response = self.session.post(
            url=self.config["endpoints"]["login"],
            json={
                "username": credential.username,
                "password": credential.password,
                "source": "Web"
            }
        )

        if response.status_code != 200:
            self.log.error(f"Login failed: {response.status_code} - {response.text}")
            response.raise_for_status()

        self._save_tokens(response.json())

    def _do_refresh(self):
        response = self.session.post(
            url=self.config["endpoints"]["refresh"],
            json={"refreshToken": self.refresh_token},
            headers=self.auth_header
        )
        if response.status_code != 200:
            raise ValueError("Token refresh failed")
        self._save_tokens(response.json())

    def _save_tokens(self, data: dict):
        self.access_token = data["accessToken"]
        self.refresh_token = data["refreshToken"]
        expires_in = data.get("expires_in", 3600)
        self.token_expiration = int(time.time()) + expires_in
        self.session.headers.update(self.auth_header)

    def _parse_select(self, ep_id: str, short_number: str, season_num: int, relative_number: Optional[int] = None) -> bool:
            """Returns True if the episode should be included."""
            if self.all_eps or not self.select_str:
                return True

            # Preparing possible identifiers for this episode
            # We test: "30353" (id), "1" (number), "S02E01" (full format), "S02" (entire season)
            candidates = [
                str(ep_id),
                str(short_number).lstrip("0"),
                f"S{season_num:02d}E{int(short_number):02d}" if str(short_number).isdigit() else "",
                f"S{season_num:02d}"
            ]
            
            # Add the relative match (e.g. S03E06 for episode 30 which is the 6th ep in S3)
            if relative_number is not None:
                candidates.append(f"S{season_num:02d}E{relative_number:02d}")
            
            parts = re.split(r'[ ,]+', self.select_str.strip().upper())
            selection: set[str] = set()

            for part in parts:
                if '-' in part:
                    start_p, end_p = part.split('-', 1)
                    # Handle ranges S02E01-S02E04
                    m_start = re.match(r'^S(\d+)E(\d+)$', start_p)
                    m_end = re.match(r'^S(\d+)E(\d+)$', end_p)
                    
                    if m_start and m_end:
                        s_start, e_start = map(int, m_start.groups())
                        s_end, e_end = map(int, m_end.groups())
                        if s_start == s_end: # Same season
                            for i in range(e_start, e_end + 1):
                                selection.add(f"S{s_start:02d}E{i:02d}")
                        continue
                    
                    # Classic ranges (1-10)
                    nums = re.findall(r'\d+', part)
                    if len(nums) >= 2:
                        for i in range(int(nums[0]), int(nums[1]) + 1):
                            selection.add(str(i))
                else:
                    selection.add(part.lstrip("0"))

            included = any(c in selection for c in candidates if c)
            return not included if self.but else included

    def get_titles(self) -> Series:
            """Retrieves episodes with the actual title of the series."""
            show_id = self.parse_show_id(self.title)
            
            # 1. Fetch the overall show info first to get the proper title
            show_url = self.config["endpoints"]["show"].format(show_id=show_id)
            show_res = self.session.get(show_url).json()
            
            # We extract the series title (e.g. "Demon Slave")
            # This title will be used as the unique folder name
            series_title = show_res["videos"][0]["show"]["title"] if show_res.get("videos") else "ADN Show"

            # 2. Fetch the season structure afterwards
            url_seasons = self.config["endpoints"].get("seasons")
            if not url_seasons:
                url_seasons = "https://gw.api.animationdigitalnetwork.com/video/show/{show_id}/seasons?maxAgeCategory=18&order=asc"
                
            res = self.session.get(url_seasons.format(show_id=show_id)).json()

            if not res.get("seasons"):
                self.log.error(f"No seasons found for ID {show_id}")
                return Series([])

            episodes = []
            for season_data in res["seasons"]:
                s_val = str(season_data.get("season", "1"))
                season_num = int(s_val) if s_val.isdigit() else 1
                
                for idx, vid in enumerate(season_data.get("videos", []), 1):
                    video_id = str(vid["id"])
                    
                    # Clean the episode number (keep only digits)
                    num_match = re.search(r'\d+', str(vid.get("number", "0")))
                    short_number = num_match.group() if num_match else "0"

                    # Selection logic (SxxEyy) - Relative and absolute support
                    if not self._parse_select(video_id, short_number, season_num, relative_number=idx):
                        continue

                    # Create the episode
                    episodes.append(Episode(
                        id_=video_id,
                        service=self.__class__,
                        title=series_title,     # Folder: "Demon Slave"
                        season=season_num,      # Season: 2
                        number=idx,             # Force relative number (e.g. 30 becomes 6 in S3)
                        name=vid.get("name") or "", # Name: "The big reunion..."
                        data=vid
                    ))

            episodes.sort(key=lambda x: (x.season, x.number))
            return Series(episodes)

    def get_discovery(self, n: int = 12) -> list[dict]:
        """
        Fetch the latest releases (Series) via the catalog API.
        """
        self.ensure_authenticated()
        
        try:
            url = self.config["endpoints"]["search"]
            response = self.session.get(
                url,
                params={
                    "maxAgeCategory": 18,
                    "order": "new",
                    "limit": n
                }
            )
            
            if response.status_code != 200:
                self.log.error(f"Catalog fetch failed: {response.status_code}")
                return []
                
            data = response.json()
            return data.get("shows", [])
            
        except Exception as e:
            self.log.error(f"Error fetching discovery: {e}")
            return []

    def get_latest_releases_calendar(self, n: int = 12) -> list[dict]:
        """
        Fetch the latest releases (Episodes) via the calendar API.
        Returns recent episodes with their actual release dates.
        """
        from datetime import datetime, timedelta
        import requests
        
        # self.ensure_authenticated()
        
        try:
            results = []
            seen_episodes = set()
            
            # Use a fresh session to avoid auth issues if calendar is public
            cal_session = requests.Session()
            cal_session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            })
            
            # Fetch calendar for the last 7 days including today
            today = datetime.now()
            
            for day_offset in range(15):
                date = today - timedelta(days=day_offset)
                date_str = date.strftime("%Y-%m-%d")
                
                # Calendar endpoint
                calendar_url = f"https://gw.api.animationdigitalnetwork.com/video/calendar?maxAgeCategory=18&date={date_str}"
                
                try:
                    res = cal_session.get(calendar_url, timeout=5)
                    if res.status_code != 200: continue
                    
                    data = res.json()
                    # Handle dict response (API returns {"videos": [...]})
                    if isinstance(data, dict):
                        data = data.get("videos", [])
                        
                    for video in data:
                        show = video.get("show", {})
                        show_id = str(show.get("id", ""))
                        ep_id = str(video.get("id", ""))
                        
                        if (show_id, ep_id) in seen_episodes: continue
                        seen_episodes.add((show_id, ep_id))
                        
                        results.append(video)
                except:
                     pass
            
            # Sort by releaseDate
            results.sort(key=lambda x: x.get("releaseDate", ""), reverse=True)
            return results[:n]
            
        except Exception as e:
            self.log.error(f"Error fetching calendar discovery: {e}")
            return []

    def get_latest_episode(self, show_id: str) -> Episode | None:
        """
        Retrieves the absolute latest episode of a series without filtering.
        Used by the GUI to display 'Latest' info.
        """
        try:
            # 1. Fetch seasons
            url_seasons = self.config["endpoints"].get("seasons")
            if not url_seasons:
                url_seasons = "https://gw.api.animationdigitalnetwork.com/video/show/{show_id}/seasons?maxAgeCategory=18&order=asc"
                
            res = self.session.get(url_seasons.format(show_id=show_id))
            if res.status_code != 200:
                return None
                
            data = res.json()
            if not data.get("seasons"):
                return None

            episodes = []
            for season_data in data["seasons"]:
                s_val = str(season_data.get("season", "1"))
                season_num = int(s_val) if s_val.isdigit() else 1
                
                for vid in season_data.get("videos", []):
                    # Simplified parsing similar to get_titles
                    num_match = re.search(r'\d+', str(vid.get("number", "0")))
                    short_number = int(num_match.group()) if num_match else 0
                    
                    episodes.append(Episode(
                        id_=str(vid["id"]),
                        service=self.__class__,
                        title="", # We don't need the series title here for simple display
                        season=season_num,
                        number=short_number,
                        name=vid.get("name") or "",
                        data=vid
                    ))

            if not episodes:
                return None
                
            # Sort: Season desc, Number desc to find absolute latest?
            # Or Season asc, Number asc and take [-1]
            episodes.sort(key=lambda x: (x.season, x.number))
            return episodes[-1]
            
        except Exception as e:
            self.log.error(f"Error in get_latest_episode: {e}")
            return None

    def get_tracks(self, title: Episode) -> Tracks:
        """
        Fetches tracks by pre-extracting audio.
        Audio is extracted now and will be copied during download().
        """
        self.ensure_authenticated()
        vid_id = title.id

        # Player configuration
        config_url = self.config["endpoints"]["player_config"].format(video_id=vid_id)
        config_res = self.session.get(config_url).json()

        player_opts = config_res["player"]["options"]
        if not player_opts["user"]["hasAccess"]:
            raise PermissionError("No access to this video (Premium required?)")

        # Player token
        refresh_url = player_opts["user"].get("refreshTokenUrl") or self.config["endpoints"]["player_refresh"]
        token_res = self.session.post(
            refresh_url,
            headers={"X-Player-Refresh-Token": player_opts["user"]["refreshToken"]}
        ).json()

        player_token = token_res["token"]
        links_url = player_opts["video"].get("url") or self.config["endpoints"]["player_links"].format(video_id=vid_id)

        # RSA Encryption
        rand_key = uuid.uuid4().hex[:16]
        payload = json.dumps({"k": rand_key, "t": player_token}).encode('utf-8')

        public_key = serialization.load_pem_public_key(
            self.RSA_PUBLIC_KEY.encode('utf-8'),
            backend=default_backend()
        )

        encrypted = public_key.encrypt(payload, padding.PKCS1v15())
        auth_header_val = base64.b64encode(encrypted).decode('utf-8')

        # Fetching links
        links_res = self.session.get(
            links_url,
            params={"freeWithAds": "true", "adaptive": "true", "withMetadata": "true", "source": "Web"},
            headers={"X-Player-Token": auth_header_val}
        ).json()

        tracks = Tracks()
        streaming_links = links_res.get("links", {}).get("streaming", {})

        # Language map
        lang_map = {
            "vf": "fr",
            "vostf": "ja",
            "vde": "de",
            "vostde": "ja",
        }

        # Priority: VOSTF (original) for the main video
        priority_order = ["vostf", "vf", "vde", "vostde"]
        available_streams = {k: v for k, v in streaming_links.items() if k in lang_map}
        
        sorted_streams = sorted(
            available_streams.keys(),
            key=lambda x: priority_order.index(x) if x in priority_order else 999
        )

        if not sorted_streams:
            raise ValueError("No supported streams found")

        # Main video (VOSTF or first available)
        primary_stream = sorted_streams[0]
        primary_lang = lang_map[primary_stream]
        
        self.log.info(f"Primary video stream: {primary_stream} ({primary_lang})")
        
        video_track = self._get_video_track(
            streaming_links[primary_stream],
            primary_stream,
            primary_lang,
            is_original=(primary_stream in ["vostf", "vostde"])
        )
        
        if video_track:
            tracks.add(video_track)
            self.log.info(f"Video track added: {video_track.width}x{video_track.height}")

        # Extract audio for all available languages
        for stream_type in sorted_streams:
            audio_lang = lang_map[stream_type]
            is_original = stream_type in ["vostf", "vostde"]
            
            self.log.info(f"Processing audio for: {stream_type} ({audio_lang})")
            
            audio_track = self._extract_audio_track(
                streaming_links[stream_type],
                stream_type,
                audio_lang,
                is_original,
                title
            )
            
            if audio_track:
                tracks.add(audio_track, warn_only=True)
                self.log.info(f"Audio track added: {audio_lang}")

        # Store chapter data for get_chapters()
        if "video" in links_res:
            title.data["chapter_data"] = links_res["video"]
            self.log.debug(f"Stored chapter data: intro={links_res['video'].get('tcIntroStart')}, ending={links_res['video'].get('tcEndingStart')}")

        # Subtitles
        self._process_subtitles(links_res, rand_key, title, tracks)

        if not tracks.videos:
            raise ValueError("No video tracks were successfully added")

        return tracks

    def _get_video_track(self, stream_data: dict, stream_type: str, lang: str, is_original: bool):
        """Fetches the main video track (without audio)."""
        try:
            m3u8_url = self._resolve_stream_url(stream_data, stream_type)
            if not m3u8_url:
                return None

            hls_manifest = HLS.from_url(url=m3u8_url, session=self.session)
            hls_tracks = hls_manifest.to_tracks(language=lang)

            if not hls_tracks.videos:
                self.log.warning(f"No video tracks found for {stream_type}")
                return None

            # Best quality
            best_video = max(
                hls_tracks.videos,
                key=lambda v: (v.height or 0, v.width or 0, v.bitrate or 0)
            )

            # Convert to VideoNoAudio to demux automatically
            video_no_audio = VideoNoAudio(
                id_=best_video.id,
                url=best_video.url,
                codec=best_video.codec,
                language=Language.get(lang),
                is_original_lang=is_original,
                bitrate=best_video.bitrate,
                descriptor=best_video.descriptor,
                width=best_video.width,
                height=best_video.height,
                fps=best_video.fps,
                range_=best_video.range,
                data=best_video.data,
            )
            
            video_no_audio.data["stream_type"] = stream_type
            
            return video_no_audio

        except Exception as e:
            self.log.error(f"Failed to get video track for {stream_type}: {e}")
            return None

    def _extract_audio_track(self, stream_data: dict, stream_type: str, lang: str, is_original: bool, title: Episode):
        """
        Extracts audio and returns an AudioExtracted.
        Audio is extracted NOW and will be copied during download().
        """
        if not binaries.FFMPEG:
            self.log.warning("FFmpeg not found, cannot extract audio")
            return None

        try:
            m3u8_url = self._resolve_stream_url(stream_data, stream_type, prioritize_auto=True)
            if not m3u8_url:
                return None

            # Create an ADN temp directory inside envied.s temp
            adn_temp = config.directories.temp / "adn_audio_extracts"
            adn_temp.mkdir(parents=True, exist_ok=True)
            
            # Unique filename based on video_id + language
            audio_filename = f"audio_{title.id}_{stream_type}.m4a"
            audio_path = adn_temp / audio_filename

            # If already extracted, reuse it
            if audio_path.exists() and audio_path.stat().st_size > 1000:
                self.log.debug(f"Reusing existing extracted audio: {audio_path}")
            else:

                # Parse M3U8 to find best audio
                best_m3u8_url = m3u8_url
                try:
                    import m3u8
                    variant_m3u8 = m3u8.load(m3u8_url)
                    
                    audio_playlists = []
                    # Check for alternative audio in media
                    for media in variant_m3u8.media:
                        if media.type == "AUDIO" and media.uri:
                             audio_playlists.append(media.uri)

                    # If no media, check strict playlists (variants)
                    if not audio_playlists and variant_m3u8.playlists:
                        # Sort by bandwidth descending
                        sorted_playlists = sorted(variant_m3u8.playlists, key=lambda x: x.stream_info.bandwidth or 0, reverse=True)
                        audio_playlists = [p.uri for p in sorted_playlists]

                    if audio_playlists:
                        # Construct absolute URL if needed
                        from urllib.parse import urljoin
                        best_audio_uri = audio_playlists[0]
                        if not best_audio_uri.startswith("http"):
                           best_m3u8_url = urljoin(m3u8_url, best_audio_uri)
                        else:
                           best_m3u8_url = best_audio_uri
                        self.log.info(f"Selected best audio stream: {best_m3u8_url}")

                except Exception as e:
                    self.log.warning(f"Failed to parse M3U8 for best audio, using default: {e}")

                # Extract with FFmpeg
                result = subprocess.run(
                    [
                        binaries.FFMPEG,
                        '-i', best_m3u8_url,
                        '-vn',
                        '-acodec', 'copy',
                        '-y',
                        str(audio_path)
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300
                )

                if result.returncode != 0:
                    self.log.error(f"FFmpeg failed for {stream_type}: {result.stderr}")
                    audio_path.unlink(missing_ok=True)
                    return None

                if not audio_path.exists() or audio_path.stat().st_size < 1000:
                    self.log.error(f"Extracted audio is invalid for {stream_type}")
                    audio_path.unlink(missing_ok=True)
                    return None

            # Detect actual bitrate
            detected_bitrate = 128000
            try:
                from pymediainfo import MediaInfo
                media_info = MediaInfo.parse(audio_path)
                if media_info.audio_tracks:
                    track = media_info.audio_tracks[0]
                    if track.bit_rate:
                        detected_bitrate = int(track.bit_rate)
                    elif track.other_bit_rate:
                        # Fallback for some formats
                        try:
                            # other_bit_rate is typically list like ['128 kb/s']
                            raw = track.other_bit_rate[0]
                            detected_bitrate = int(re.sub(r'[^\d]', '', raw)) * 1000
                        except:
                            pass
                self.log.debug(f"Detected audio bitrate: {detected_bitrate}")
            except Exception as e:
                self.log.warning(f"Failed to detect bitrate: {e}")

            # Create AudioExtracted with the pre-extracted file
            audio_track = AudioExtracted(
                id_=f"audio-{stream_type}-{lang}",
                extracted_path=audio_path,
                codec=Audio.Codec.AAC,
                language=Language.get(lang),
                is_original_lang=is_original,
                bitrate=detected_bitrate,
                channels=2.0,
            )
            
            return audio_track

        except subprocess.TimeoutExpired:
            self.log.error(f"FFmpeg timeout for {stream_type}")
            return None
        except Exception as e:
            self.log.error(f"Failed to extract audio for {stream_type}: {e}")
            return None

    def _resolve_stream_url(self, stream_data: dict, stream_type: str, prioritize_auto: bool = False) -> Optional[str]:
        """Resolves the stream URL."""
        if prioritize_auto:
             preferred_keys = ["auto", "fhd", "hd", "sd", "mobile"]
        else:
             preferred_keys = ["fhd", "hd", "auto", "sd", "mobile"]

        m3u8_url = None
        for key in preferred_keys:
            if key in stream_data and stream_data[key]:
                m3u8_url = stream_data[key]
                break

        if not m3u8_url:
            return None

        try:
            resp = self.session.get(m3u8_url, timeout=12)
            if resp.status_code != 200:
                return None

            content_type = resp.headers.get("Content-Type", "")
            resp_text = resp.text.strip()

            if "application/json" in content_type or resp_text.startswith("{"):
                try:
                    json_data = resp.json()
                    real_location = json_data.get("location")
                    if real_location:
                        return real_location
                except json.JSONDecodeError:
                    pass

            return m3u8_url

        except Exception as e:
            self.log.error(f"Failed to resolve URL for {stream_type}: {e}")
            return None

    def _process_subtitles(self, links_res: dict, rand_key: str, title: Episode, tracks: Tracks):
        """Processes subtitles."""
        subs_root = links_res.get("links", {}).get("subtitles", {})
        if "all" not in subs_root:
            self.log.debug("No subtitles available")
            return

        aes_key_bytes = bytes.fromhex(rand_key + '7fac1178830cfe0c')

        try:
            sub_loc_res = self.session.get(subs_root["all"]).json()
            encrypted_sub_res = self.session.get(sub_loc_res["location"]).text

            self.log.debug(f"Encrypted subtitle length: {len(encrypted_sub_res)}")

            iv_b64 = encrypted_sub_res[:24]
            payload_b64 = encrypted_sub_res[24:]

            iv = base64.b64decode(iv_b64)
            ciphertext = base64.b64decode(payload_b64)

            self.log.debug(f"IV length: {len(iv)}, Ciphertext length: {len(ciphertext)}")

            cipher = Cipher(algorithms.AES(aes_key_bytes), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted_padded = decryptor.update(ciphertext) + decryptor.finalize()

            # ALWAYS remove PKCS7 padding (Python doesn't do it automatically)
            pad_len = decrypted_padded[-1]
            if not (1 <= pad_len <= 16):
                self.log.error(f"Invalid PKCS7 padding length: {pad_len}")
                return
            
            # Ensure all padding bytes have the same value
            padding = decrypted_padded[-pad_len:]
            if not all(b == pad_len for b in padding):
                self.log.error(f"Invalid PKCS7 padding bytes")
                return
            
            decrypted_json = decrypted_padded[:-pad_len].decode('utf-8')
            self.log.debug(f"Decrypted JSON length: {len(decrypted_json)}")

            
            subs_data = json.loads(decrypted_json)

            
            if not isinstance(subs_data, dict):
                self.log.error(f"subs_data is not a dict! Type: {type(subs_data)}")
                return
            
            if len(subs_data) == 0:
                self.log.warning("subs_data is empty!")
                return
            
            # Debug each key
            for key in subs_data.keys():
                value = subs_data[key]
                if isinstance(value, list) and len(value) > 0:
                    self.log.debug(f"    First item type: {type(value[0])}")
                    self.log.debug(f"    First item keys: {value[0].keys() if isinstance(value[0], dict) else 'NOT A DICT'}")
                    self.log.debug(f"    First item sample: {str(value[0])[:200]}")
            processed_langs = set()
            
            for sub_lang_key, cues in subs_data.items():
                
                if not isinstance(cues, list):
                    self.log.warning(f"Cues for {sub_lang_key} is not a list! Type: {type(cues)}")
                    continue
                
                if len(cues) == 0:
                    self.log.debug(f"No subtitles for {sub_lang_key} (normal for dubbed versions)")
                    continue
                
                self.log.debug(f"  Cues count: {len(cues)}")
                self.log.debug(f"  First cue: {cues[0]}")
                
                if "vf" in sub_lang_key.lower():
                    target_lang = "fr"
                    is_forced = True
                elif "vostf" in sub_lang_key.lower():
                    target_lang = "fr"
                    is_forced = False
                elif "vde" in sub_lang_key.lower():
                    target_lang = "de"
                    is_forced = True
                elif "vostde" in sub_lang_key.lower():
                    target_lang = "de"
                    is_forced = False
                else:
                    self.log.debug(f"Skipping subtitle language: {sub_lang_key}")
                    continue

                if (target_lang, is_forced) in processed_langs:
                    self.log.debug(f"Already processed {target_lang} (forced={is_forced}), skipping")
                    continue
                
                processed_langs.add((target_lang, is_forced))

                # Convert to ASS
                ass_content = self._json_to_ass(cues, title.title, title.number)
                
                # Check if ASS file has content
                event_count = ass_content.count("Dialogue:")
                self.log.debug(f"Generated ASS with {event_count} dialogue events")
                
                if event_count == 0:
                    self.log.warning(f"ASS file has no dialogue events!")
                    self.log.warning(f"First cue was: {cues[0] if cues else 'EMPTY LIST'}")
                
                # Create SubtitleEmbedded directly with ASS content
                subtitle = SubtitleEmbedded(
                    id_=f"sub-{target_lang}-{sub_lang_key}",
                    embedded_content=ass_content,  # ASS content directly
                    codec=Subtitle.Codec.SubStationAlphav4,
                    language=Language.get(target_lang),
                    forced=is_forced,
                    sdh=False,
                )
                
                tracks.add(subtitle, warn_only=True)
                self.log.info(f"Subtitle added: {target_lang} ({event_count} events)")

        except json.JSONDecodeError as e:
            self.log.error(f"Failed to decode JSON: {e}")
            self.log.error(f"Decrypted data (first 500 chars): {decrypted_json[:500] if 'decrypted_json' in locals() else 'NOT DECRYPTED'}")
        except Exception as e:
            self.log.error(f"Failed to process subtitles: {e}")
            import traceback
            self.log.debug(traceback.format_exc())

    def get_chapters(self, title: Episode) -> Chapters:
        """
        Creates chapters from ADN timecodes.
        - If tcIntroStart exists:
            - If tcIntroStart != "00:00:00": add "Prologue" at 00:00:00
            - Add "Opening" at tcIntroStart
            - Add "Episode" at tcIntroEnd
        - Otherwise: add "Episode" at 00:00:00
        - If tcEndingStart exists:
            - Add "Ending Start" at tcEndingStart
            - Add "Ending End" at tcEndingEnd
        """
        chapters = Chapters()
        
        # Retrieve chapter data stored in get_tracks()
        chapter_data = title.data.get("chapter_data", {})
        if not chapter_data:
            self.log.debug("No chapter data available")
            return chapters
        
        tc_intro_start = chapter_data.get("tcIntroStart")
        tc_intro_end = chapter_data.get("tcIntroEnd")
        tc_ending_start = chapter_data.get("tcEndingStart")
        tc_ending_end = chapter_data.get("tcEndingEnd")
        
        self.log.debug(f"Chapter timecodes: intro={tc_intro_start}->{tc_intro_end}, ending={tc_ending_start}->{tc_ending_end}")
        
        try:
            chapter_num = 1
            
            if tc_intro_start:
                # If intro does not start at 00:00:00, add a prologue (Chapter 1)
                if tc_intro_start != "00:00:00":
                    chapters.add(Chapter(
                        timestamp=0,
                        name=f"Chapter {chapter_num}"
                    ))
                    self.log.debug(f"Added Chapter {chapter_num} at 00:00:00")
                    chapter_num += 1
                
                # Opening
                chapters.add(Chapter(
                    timestamp=self._timecode_to_ms(tc_intro_start),
                    name="Opening"
                ))
                self.log.debug(f"Added Opening at {tc_intro_start}")
                
                # Episode (after intro)
                if tc_intro_end:
                    chapters.add(Chapter(
                        timestamp=self._timecode_to_ms(tc_intro_end),
                        name=f"Chapter {chapter_num}"
                    ))
                    self.log.debug(f"Added Chapter {chapter_num} at {tc_intro_end}")
                    chapter_num += 1
            else:
                # No intro, episode starts at 00:00:00
                chapters.add(Chapter(
                    timestamp=0,
                    name=f"Chapter {chapter_num}"
                ))
                self.log.debug(f"Added Chapter {chapter_num} at 00:00:00 (no intro)")
                chapter_num += 1
            
            # Ending
            if tc_ending_start:
                chapters.add(Chapter(
                    timestamp=self._timecode_to_ms(tc_ending_start),
                    name="Ending"
                ))
                self.log.debug(f"Added Ending at {tc_ending_start}")
                
                if tc_ending_end:
                    # Check if the remaining chapter has a significant duration (> 10s)
                    # to avoid micro-chapters of 2s, while keeping actual post-credits scenes.
                    
                    tc_end_ms = self._timecode_to_ms(tc_ending_end)
                    total_duration_s = title.data.get("duration", 0)
                    total_duration_ms = int(total_duration_s * 1000)
                    
                    # If duration is unknown or > 10 seconds remaining
                    should_add = True
                    if total_duration_ms > 0:
                        remaining_ms = total_duration_ms - tc_end_ms
                        if remaining_ms < 10000: # Less than 10s
                            should_add = False
                            self.log.debug(f"Skipping post-ending chapter (only {remaining_ms}ms remaining)")
                    
                    if should_add:
                        chapters.add(Chapter(
                            timestamp=tc_end_ms,
                            name=f"Chapter {chapter_num}"
                        ))
                        self.log.debug(f"Added Chapter {chapter_num} at {tc_ending_end}")
                        chapter_num += 1
            
            self.log.info(f"✓ Created {len(chapters)} chapters")
            
        except Exception as e:
            self.log.error(f"Failed to create chapters: {e}")
            import traceback
            self.log.debug(traceback.format_exc())
        
        return chapters

    def search(self) -> Generator[SearchResult, None, None]:
        res = self.session.get(
            self.config["endpoints"]["search"],
            params={"search": self.title, "limit": 20, "offset": 0}
        ).json()

        for show in res.get("shows", []):
            yield SearchResult(
                id_=str(show["id"]),
                title=show["title"],
                label=show["type"],
                description=show.get("summary", "")[:300],
                url=f"https://animationdigitalnetwork.com/video/{show['id']}",
                image=show.get("image")
            )

    def parse_show_id(self, input_str: str) -> str:
        if input_str.isdigit():
            return input_str
        match = re.match(self.TITLE_RE, input_str)
        if match:
            return match.group("id")
        raise ValueError(f"Invalid ADN Show ID/URL: {input_str}")

    def _json_to_ass(self, cues: List[dict], title: str, ep_num: Union[int, str]) -> str:
        """Converts JSON subtitles to ASS."""
        header = """[Script Info]
ScriptType: v4.00+
WrapStyle: 0
Collisions: Normal
PlayResX: 1920
PlayResY: 1080
Timer: 0.0000
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Trebuchet MS,66,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,3,2,75,75,75,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        pos_align_map = {"start": 1, "end": 3}
        line_align_map = {"middle": 8, "end": 4}

        def format_time(seconds: float) -> str:
            """Exact ADN format: HH:MM:SS.CC (2-digit centiseconds)"""
            secs = int(seconds)
            centiseconds = round((seconds - secs) * 100)
            
            hours = secs // 3600
            minutes = (secs % 3600) // 60
            remaining_seconds = secs % 60
            
            # 2-digit padding for EVERYTHING (including hours)
            return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}.{centiseconds:02d}"

        for cue in cues:
            start_time = cue.get("startTime", 0)
            end_time = cue.get("endTime", 0)
            text = cue.get("text", "")
            
            # Skip if text is empty
            if not text or not text.strip():
                continue

            # EXACT cleanup corresponding to ADN code
            text = text.replace(' \\N', '\\N')  # remove space before \\N at end
            if text.endswith('\\N'):
                text = text[:-2]  # remove \\N at end
            text = text.replace('\r', '')
            text = text.replace('\n', '\\N')
            text = re.sub(r'\\N +', r'\\N', text)  # \\N followed by spaces
            text = re.sub(r' +\\N', r'\\N', text)  # spaces followed by \\N
            text = re.sub(r'(\\N)+', r'\\N', text)  # multiple \\N
            text = re.sub(r'<b[^>]*>([^<]*)</b>', r'{\\b1}\1{\\b0}', text)
            text = re.sub(r'<i[^>]*>([^<]*)</i>', r'{\\i1}\1{\\i0}', text)
            text = re.sub(r'<u[^>]*>([^<]*)</u>', r'{\\u1}\1{\\u0}', text)
            text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            text = re.sub(r'<[^>]>', '', text)  # remove any remaining single tags
            if text.endswith('\\N'):
                text = text[:-2]
            text = text.rstrip()  # remove trailing spaces
            
            # Skip after cleanup if empty
            if not text.strip():
                continue

            p_align = pos_align_map.get(cue.get("positionAlign"), 2)
            l_align = line_align_map.get(cue.get("lineAlign", ""), 0)
            align_val = p_align + l_align

            start = format_time(start_time)
            end = format_time(end_time)
            
            style_mod = f"{{\\a{align_val}}}" if align_val != 2 else ""
            events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{style_mod}{text}")

        self.log.debug(f"Converted {len(events)} subtitle events from {len(cues)} cues")
        
        if not events:
            self.log.warning(f"No subtitle events generated - all cues were empty or invalid (total cues: {len(cues)})")
        
        return header + "\n".join(events)
