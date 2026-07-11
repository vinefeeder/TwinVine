from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import hmac
import json
import re
import sys
import time
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union

import click
import m3u8
from click import Context
from langcodes import Language
from requests import Request
from envied.core.config import config
from envied.core.credential import Credential
from envied.core.downloaders import requests
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Subtitle, Tracks, Video


class TEN(Service):
    """
    \b
    Service code for 10Play streaming service (https://10.com.au/).

    \b
    Version: 1.0.2
    Author: stabbedbybrick
    Authorization: credentials
    Geofence: AU (API and downloads)
    Robustness:
      AES: 1080p, AAC2.0

    \b
    Tips:
        - Input should be complete URL:
          SHOW: https://10.com.au/australian-survivor
          EPISODE: https://10.com.au/australian-survivor/episodes/season-11-australia-v-the-world/episode-9/tpv250831fxatm
          MOVIE: https://10.com.au/a-quiet-place
        - Non-standard programmes (e.g. game shows/sports) have very inconsistent episode number labels. It's recommended to use episode URLs for those. 

    \b
    Notes:
        - 10Play uses transport streams for HLS, meaning the video and audio are a part of the same stream.
          As a result, only videos are listed as tracks. But the audio will be included as well.
        - Since 1080p streams require some manipulation of the manifest, n_m3u8dl_re downloader is required.

    """

    GEOFENCE = ("au",)
    ALIASES = (
        "10play",
        "tenplay",
    )

    @staticmethod
    @click.command(name="TEN", short_help="https://10.com.au/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> TEN:
        return TEN(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        if config.downloader != "n_m3u8dl_re":
            self.log.error(" - Error: n_m3u8dl_re downloader is required for this service.")
            sys.exit(1)

    def search(self) -> Generator[SearchResult, None, None]:
        query = self.endpoints["searchApiEndpoint"] + self.title

        results = self._request("GET", query)

        for result in results:
            clean_title = self._sanitize(result.get("title"))
            yield SearchResult(
                id_=f"https://10.com.au/{clean_title}",
                title=result.get("title"),
                description=result.get("abstractShowDescription"),
                label=result.get("subtitle", "").split("|")[-1].strip(),
                url=f"https://10.com.au/{clean_title}",
            )

    def authenticate(
        self,
        cookies: Optional[MozillaCookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        self.session.headers.update(self.config["headers"])
        self.endpoints = self._request(
            "GET", self.config["endpoints"]["config"], params={"SystemName": "tvos"}
        )

        cache = self.cache.get(f"tokens_{credential.sha1}")

        if cache and not cache.expired:
            self.log.info(" + Using cached Tokens...")
            tokens = cache.data
        elif cache and cache.expired:
            self.log.info(" + Refreshing expired Tokens...")
            payload = {
                "alternativeToken": cache.data["alternativeToken"],
                "refreshToken": cache.data["refreshToken"],
            }
            tokens = self._request(
                "POST", self.endpoints["authConfig"]["refreshToken"], json=payload
            )
            cache.set(tokens, expiration=tokens["expiresIn"])
        else:
            self.log.info(" + Logging in...")
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
                "origin": "https://10.com.au",
                "referer": "https://10.com.au/",
            }
            login = self._request(
                "POST",
                self.config["endpoints"]["auth"],
                headers=headers,
                json={"email": credential.username, "password": credential.password},
            )
            access_token = login.get("jwt", {}).get("accessToken")
            if not access_token:
                raise ValueError(
                    "Failed to authenticate with credentials: " + login.text
                )

            identifier = str(uuid.uuid4())

            payload = {
                "deviceIdentifier": identifier,
                "machine": "Hisense",
                "system": "vidaa",
                "systemVersion": "U6",
                "platform": "vidaa",
                "appVersion": "v1",
                "ipAddress": "string",
            }
            device = self._request(
                "POST", self.endpoints["authConfig"]["generateCode"], json=payload
            )

            code = device.get("code")
            expiry = device.get("expiry")
            if not code or not expiry:
                raise ValueError("Failed to generate device code: " + device.text)

            headers = {
                "accept": "application/json, text/plain, */*",
                "authorization": f"Bearer {access_token}",
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
                "origin": "https://10.com.au",
                "referer": "https://10.com.au/activate",
            }
            activate = self._request(
                "POST",
                self.endpoints["activateApiEndpoint"],
                headers=headers,
                json={"code": code},
            )
            if not activate:
                raise ValueError("Failed to activate device")

            payload = {
                "code": code,
                "deviceIdentifier": identifier,
                "expiry": expiry,
            }
            auth = self._request(
                "POST",
                self.endpoints["authConfig"]["validateCode"],
                json={
                    "code": code,
                    "deviceIdentifier": identifier,
                    "expiry": expiry,
                },
            )
            tokens = auth.get("jwt")
            tokens["identifier"] = identifier

            self.log.info(" + User successfully logged in, TV device activated")

            cache.set(tokens, expiration=tokens.get("expiresIn"))

        self.access_token = tokens.get("alternativeToken")
        self.session.headers.update({"authorization": f"Bearer {self.access_token}"})

    def get_titles(self) -> Union[Movies, Series]:
        url_pattern = re.compile(
            r"^https://10\.com\.au/(?:[a-z0-9-]+)"
            r"(?:/episodes/(?:season-)?(?P<season>[a-z0-9-]+)/(?:episode-)?(?P<episode>[a-z0-9-]+)/(?P<id>[a-z0-9]+))?$"
        )

        match = url_pattern.match(self.title)
        if not match:
            raise ValueError(f"Could not parse ID from title: {self.title}")

        matches = match.groupdict()

        if not matches.get("id"):
            show_id = self._get_html(self.title)
            content = self._shows(show_id)

            if "movie" in content.get("subtitle", "").lower():
                movies = self._movie(content)
                return Movies(movies)

            else:
                episodes = self._series(content)
                return Series(episodes)

        else:
            episodes = self._episode(matches.get("id"))
            return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        playback_url = title.data.get("playbackApiEndpoint")
        if not playback_url:
            raise ValueError("Could not find playback URL for this title")

        params = {
            "device": "Tv",
            "platform": "vidaa",
            "appVersion": "v1",
        }

        r = self.session.get(playback_url, params=params)
        if not r.ok:
            raise ValueError("Failed to get playback data: " + r.text)
        
        
        dai_auth = r.headers.get("X-DAI-AUTH")
        video_id = r.headers.get("x-dai-video-id")
        if dai_auth is not None:
            payload = {"auth-token": dai_auth}
        
        playback_data = r.json()

        video_id = playback_data.get("dai", {}).get("videoId")
        source_id = playback_data.get("dai", {}).get("contentSourceId", "2690006")
        if not video_id or not source_id:
            raise ValueError("Failed to get video ID: " + r.text)
        
        dai_stream = f"https://dai.google.com/ondemand/v1/hls/content/{source_id}/vid/{video_id}/stream"

        stream_data = self._request("POST", dai_stream, data=payload)

        title.data["chapters"] = stream_data.get("time_events_url")
        # program_language = Language.find(stream_data["customFields"].get("program_language", "en"))

        manifest_url = stream_data.get("stream_manifest")
        tracks = HLS.from_url(manifest_url, self.session).to_tracks(language="en")

        tracks = self._add_tracks(tracks)

        for track in tracks:
            track.OnSegmentFilter = lambda x: re.search(r"redirector.googlevideo.com", x.uri)
            track.downloader_args = {"--ad-keyword": "redirector.googlevideo.com"}

            if isinstance(track, Subtitle):
                track.downloader = requests

        # if caption := stream_data.get("subtitles", [])[0].get("webvtt"):
        #     tracks.add(
        #         Subtitle(
        #             id_=hashlib.md5(caption.encode()).hexdigest()[0:6],
        #             url=caption,
        #             codec=Subtitle.Codec.from_mime(caption[-3:]),
        #             language=stream_data.get("subtitles", [])[0].get("language", "en"),
        #         )
        #     )

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        if not title.data.get("chapters"):
            return Chapters()
        
        events = self._request("GET", title.data["chapters"])
        cue_points = events.get("cuepoints")
        if not cue_points:
            return Chapters()

        chapters = []
        for cue in cue_points:
            chapters.append(Chapter(timestamp=float(cue["start_float"])))
            chapters.append(Chapter(timestamp=float(cue["end_float"])))

        return Chapters(chapters)

    # Service specific

    def _head_request(self, url: str) -> int:
        try:
            return self.session.head(url, timeout=10).status_code
        except Exception:
            return 0

    def _check_and_add_track(
        self, best_track: Video, quality_info: dict, source_bitrate: int
    ) -> Video | None:
        playlist_uri = best_track.data["hls"]["playlist"].uri
        playlist_text = self.session.get(playlist_uri).text

        string_to_replace = f"-{source_bitrate}"
        replacement_string = f"-{quality_info['bitrate']}"
        
        lines = []
        for line in playlist_text.splitlines():
            if "redirector.googlevideo.com" in line:
                continue
            
            if string_to_replace in line:
                line = line.replace(string_to_replace, replacement_string)
            
            lines.append(line)

        modified_playlist_text = "\n".join(lines)
        playlist_obj = m3u8.loads(modified_playlist_text)

        if not playlist_obj.segments:
            return None

        first_segment = playlist_obj.segments[0].uri
        if self._head_request(first_segment) == 200:
            playlist_file = config.directories.cache / "TEN" / f"playlist_{quality_info['quality']}.m3u8"
            playlist_obj.dump(playlist_file)

            video = Video(
                id_=f"{best_track.id}-{quality_info['quality']}",
                url=best_track.url,
                height=quality_info["height"],
                width=quality_info["width"],
                bitrate=quality_info["bitrate"],
                language=best_track.language,
                codec=best_track.codec,
                range_=best_track.range,
                fps=best_track.fps,
                descriptor=best_track.descriptor,
                data=best_track.data.copy(),
                from_file=playlist_file,
            )
            return video
        return None

    def _add_tracks(self, tracks: Tracks) -> Tracks:
        if not tracks.videos:
            return tracks

        best_track = max(tracks.videos, key=lambda t: t.height or 0)

        source_bitrate = {
            1080: "5000000",
            720: "3000000",
            540: "1500000",
            360: "750000",
        }.get(best_track.height)

        all_qualities = [
            {"quality": "540p", "bitrate": 1500000, "height": 540, "width": 960},
            {"quality": "720p", "bitrate": 3000000, "height": 720, "width": 1280},
            {"quality": "1080p", "bitrate": 5000000, "height": 1080, "width": 1920},
        ]

        qualities_to_check = [
            q for q in all_qualities if q["height"] > best_track.height
        ]

        if not qualities_to_check:
            return tracks

        with ThreadPoolExecutor(max_workers=len(qualities_to_check)) as executor:
            future_to_track = {
                executor.submit(self._check_and_add_track, best_track, quality, source_bitrate): quality
                for quality in qualities_to_check
            }
            
            for future in concurrent.futures.as_completed(future_to_track):
                new_track = future.result()
                if new_track:
                    tracks.add(new_track)
        
        return tracks


    def _shows(self, show_id: str) -> dict:
        show = self._request("GET", f'{self.endpoints["showsApiEndpoint"]}/{show_id}')

        return show[0] if isinstance(show, list) else show

    def _fetch_episode(self, url: str) -> list:
        return self._request("GET", url)

    def _series(self, content: dict) -> Episode:
        season_list = content.get("seasons")
        if not season_list:
            raise ValueError("Could not find a season list for this title")

        seasons = [
            season.get("menuItems", [])[0].get("apiEndpoint")
            for season in season_list
            if season.get("menuItems", [])
            and season.get("menuItems", [])[0].get("menuTitle", "").lower()
            == "episodes"
        ]

        if not seasons:
            raise ValueError("Could not find a season list for this title")

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(self._fetch_episode, seasons))

        titles = []
        for result in results:
            for episode in result:
                ep_number = episode.get("episode")
                sea_number = episode.get("season")
                titles.append(
                    Episode(
                        id_=episode.get("id"),
                        service=self.__class__,
                        name=episode.get("vodTitle", "").split(" - ")[-1],
                        season=int(sea_number) if sea_number and sea_number.isdigit() else 0,
                        number=int(ep_number) if ep_number and ep_number.isdigit() else 0,
                        title=episode.get("tvShow"),
                        data=episode,
                    )
                )

        return titles

    def _movie(self, data: dict) -> Movie:
        endpoint = next(
            (
                season.get("menuItems", [])[0].get("apiEndpoint")
                for season in data.get("seasons", [])
                if season.get("menuItems", [])
            ),
            None,
        )
        if not endpoint:
            raise ValueError("Could not find an endpoint for this title")

        movie = self._request("GET", endpoint)[0]

        return [
            Movie(
                id_=movie.get("id"),
                service=self.__class__,
                name=movie.get("title"),
                year=movie.get("season"),
                data=movie,
            )
        ]

    def _episode(self, video_id: str) -> Episode:
        data = self._request("GET", f"{self.endpoints['videosApiEndpoint']}/{video_id}")

        ep_number = data.get("episode")
        sea_number = data.get("season")
        return [
            Episode(
                id_=data.get("id"),
                service=self.__class__,
                name=data.get("vodTitle", "").split(" - ")[-1],
                season=int(sea_number) if sea_number and sea_number.isdigit() else 0,
                number=int(ep_number) if ep_number and ep_number.isdigit() else 0,
                title=data.get("tvShow"),
                data=data,
            )
        ]

    def _get_html(self, url: str) -> Optional[str]:
        page = self.session.get(url).text
        pattern = re.compile(r"const showPageData = ({.*?});", re.DOTALL)

        match = pattern.search(page)
        if not match:
            raise ValueError(
                " - Failed to parse HTML. Page Data not found in the source code."
            )

        page_data = match.group(1)

        try:
            data = json.loads(page_data)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(f"Failed to parse JSON: {e}")

        show_id = data.get("video", {}).get("showUrlCode")
        if not show_id:
            raise ValueError(" - showUrlCode not found in the source code.")

        return show_id

    def _signature_header(self, url: str) -> str:
        timestamp = int(time.time())
        message = f"{timestamp}:{url}".encode("utf-8")
        api_key = bytes.fromhex(self.config["api_key"])
        signature = hmac.new(api_key, message, hashlib.sha256).hexdigest()
        return f"{timestamp}_{signature}"

    def _auth_header(self) -> str:
        now_utc = datetime.now(timezone.utc)
        timestamp_str = now_utc.strftime("%Y%m%d%H%M%S")
        encoded_bytes = base64.b64encode(timestamp_str.encode("utf-8"))
        return encoded_bytes.decode("ascii")

    def _request(self, method: str, url: str, **kwargs: Any) -> Any[dict | str]:
        if method == "GET":
            self.session.headers.update(
                {
                    "X-N10-SIG": self._signature_header(url),
                    "tp-acceptfeature": "v1/fw;v1/drm;v2/live",
                    "tp-platform": "UAP",
                }
            )
        elif method == "POST":
            self.session.headers.update({"X-Network-Ten-Auth": self._auth_header()})

        prep = self.session.prepare_request(Request(method, url, **kwargs))

        response = self.session.send(prep)
        if response.status_code not in (200, 201):
            raise ConnectionError(f"{response.text}")

        try:
            return json.loads(response.content)

        except json.JSONDecodeError:
            return True if "true" in response.text else False

    @staticmethod
    def _sanitize(title: str) -> str:
        title = title.lower()
        title = title.replace("&", "and")
        title = re.sub(r"[:;/()]", "", title)
        title = re.sub(r"[ ]", "-", title)
        title = re.sub(r"[\\*!?¿,'\"<>|$#`’]", "", title)
        title = re.sub(rf"[{'.'}]{{2,}}", ".", title)
        title = re.sub(rf"[{'_'}]{{2,}}", "_", title)
        title = re.sub(rf"[{'-'}]{{2,}}", "-", title)
        title = re.sub(rf"[{' '}]{{2,}}", " ", title)
        return title
