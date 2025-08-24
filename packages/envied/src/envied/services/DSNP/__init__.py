
from __future__ import annotations

import re
import sys
import uuid
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional, Union

import click
from click import Context
from requests import Request

#from envied.core.downloaders import n_m3u8dl_re
from envied.core.credential import Credential
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Tracks, Video
from envied.core.utils.collections import as_list

from . import queries


class DSNP(Service):
    """
    \b
    Service code for DisneyPlus streaming service (https://www.disneyplus.com).

    \b
    Authorization: Credentials
    Robustness:
        Widevine:
            L1: 2160p, 1080p
            L3: 720p
        PlayReady:
            SL3: 2160p, 1080p

    \b
    Tips:
        - Input should be only the entity ID for both series and movies:
            MOVIE: entity-99e15d53-926e-4074-b9f4-6524d10c8bed
            SERIES: entity-30429ad6-dd12-41bf-924e-19131fa66bb5
        - Use the --lang LANG_RANGE option to request non-english tracks
        - CDM level dictates playback quality (L3 == 720p, L1 == 1080p, 2160p)

    \b
    Notes:
        - On first run, the program will look for the first account profile that doesn't
          have kids mode or pin protection enabled. If none are found, the program will exit.
        - The profile will be cached and re-used until cache is cleared.

    """

    @staticmethod
    @click.command(name="DSNP", short_help="https://www.disneyplus.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> DSNP:
        return DSNP(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)
        self.cdm = ctx.obj.cdm
        self.playback_data = {}

        vcodec = ctx.parent.params.get("vcodec")
        range = ctx.parent.params.get("range_")

        self.range = range[0].name if range else "SDR"
        self.vcodec = "H265" if vcodec and vcodec == Video.Codec.HEVC else "H264"
        if self.range != "SDR" and self.vcodec != "H265":
            self.vcodec = "H265"

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        self.session.headers.update(self.config["HEADERS"])
        self.session.headers.update({"x-bamsdk-transaction-id": str(uuid.uuid4())})
        self.prd_config = self.session.get(self.config["CONFIG_URL"]).json()

        self._cache = self.cache.get(f"tokens_{credential.sha1}")
        if self._cache:
            self.log.info(" + Refreshing Tokens")
            profile = self.refresh_token(self._cache.data["token"]["refreshToken"])
            self._cache.set(profile, expiration=profile["token"]["expiresIn"] - 30)
            token = self._cache.data["token"]["accessToken"]
            self.session.headers.update({"Authorization": "Bearer {}".format(token)})
            self.active_session = self.account()["activeSession"]
        else:
            self.log.info(" + Setting up new profile...")
            token = self.register_device()
            status = self.check_email(credential.username, token)
            if status.lower() == "register":
                raise ValueError("Account is not registered. Please register first.")
            elif status.lower() == "otp":
                self.log.error(" - Account requires passcode for login.")
                sys.exit(1)

            else:
                tokens = self.login(credential.username, credential.password, token)
                self.session.headers.update({"Authorization": "Bearer {}".format(tokens["accessToken"])})
                account = self.account()
                profile_id = next(
                    (
                        x.get("id")
                        for x in account["account"]["profiles"]
                        if not x["attributes"]["kidsModeEnabled"]
                        and not x["attributes"]["parentalControls"]["isPinProtected"]
                    ),
                    None,
                )
                if not profile_id:
                    self.log.error(
                        " - Missing profile - you need at least one profile with kids mode and pin protection disabled"
                    )
                    sys.exit(1)

                set_profile = self.switch_profile(profile_id)
                profile = self.refresh_token(set_profile["token"]["refreshToken"])
                self._cache.set(profile, expiration=profile["token"]["expiresIn"] - 30)
                token = self._cache.data["token"]["accessToken"]
                self.session.headers.update({"Authorization": "Bearer {}".format(token)})
                self.active_session = self.account()["activeSession"]

            self.log.info(" + Acquired tokens...")

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "query": self.title,
        }
        endpoint = self.href(
            self.prd_config["services"]["explore"]["client"]["endpoints"]["search"]["href"],
            version=self.config["EXPLORE_VERSION"],
        )
        data = self._request("GET", endpoint, params=params)["data"]["page"]
        if not data.get("containers"):
            return

        results = data["containers"][0]["items"]

        for result in results:
            entity = "entity-" + result.get("id")
            yield SearchResult(
                id_=entity,
                title=result["visuals"].get("title"),
                description=result["visuals"]["description"].get("brief"),
                label=result["visuals"]["metastringParts"].get("releaseYearRange", {}).get("startYear"),
                url=f"https://www.disneyplus.com/browse/{entity}",
            )

    def get_titles(self) -> Union[Movies, Series]:
        if not self.title.startswith("entity"):
            raise ValueError("Invalid input - Use only entity IDs.")

        content = self.get_deeplink(self.title)
        _type = content["data"]["deeplink"]["actions"][0]["contentType"]

        if _type == "movie":
            movie = self._movie(self.title)
            return Movies(movie)

        elif _type == "series":
            episodes = self._show(self.title)
            return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        resource_id = title.data.get("resourceId")
        content_id = title.data["partnerFeed"].get("dmcContentId")
        content = self.get_video(content_id)
        playback = content["video"]["mediaMetadata"]["playbackUrls"][0]["href"]

        token = self._refresh()

        headers = {
            "accept": "application/vnd.media-service+json; version=5",
            "authorization": token,
            "x-dss-feature-filtering": "true",
        }

        payload = {
            "playbackId": resource_id,
            "playback": {
                "attributes": {
                    "codecs": {
                        "supportsMultiCodecMaster": False,
                    },
                    "protocol": "HTTPS",
                    # "ads": "",
                    "frameRates": [60],
                    "assetInsertionStrategy": "SGAI",
                    "playbackInitializationContext": "ONLINE",
                },
            },
        }

        video_ranges = []
        audio_types = []

        audio_types.append("ATMOS")
        audio_types.append("DTS_X")

        if not self.cdm.security_level == 3 and self.range == "DV":
            video_ranges.append("DOLBY_VISION")

        if not self.cdm.security_level == 3 and self.range == "HDR10":
            video_ranges.append("HDR10")

        if self.vcodec == "H265":
            payload["playback"]["attributes"]["codecs"] = {"video": ["h264", "h265"]}

        if audio_types:
            payload["playback"]["attributes"]["audioTypes"] = audio_types

        if video_ranges:
            payload["playback"]["attributes"]["videoRanges"] = video_ranges

        if self.cdm.security_level == 3:
            payload["playback"]["attributes"]["resolution"] = {"max": ["1280x720"]}

        scenario = "ctr-regular" if self.cdm.security_level == 3 else "ctr-high"
        endpoint = playback.format(scenario=scenario)

        res = self._request("POST", endpoint, payload=payload, headers=headers)
        self.playback_data[title.id] = self._request(
            "POST", f"https://disney.playback.edge.bamgrid.com/v7/playback/{scenario}", payload=payload, headers=headers
        )

        manifest = res["stream"]["complete"][0]["url"]

        tracks = HLS.from_url(url=manifest, session=self.session).to_tracks(language=title.language)
        for audio in tracks.audio:
            bitrate = re.search(
                r"(?<=r/composite_)\d+|\d+(?=_complete.m3u8)",
                as_list(audio.url)[0],
            )
            audio.bitrate = int(bitrate.group()) * 1000
            if audio.bitrate == 1000_000:
                # DSNP lies about the Atmos bitrate
                audio.bitrate = 768_000

        for track in tracks:
            if track not in tracks.attachments:
                track.downloader = "N_m3u8DL-RE"
                track.needs_repack = True

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        """
        Extract chapter information from the title data if available.
        Returns chapter markers for intro, credits, and scenes.
        """
        chapters = Chapters()

        try:
            # First try to get chapters from the new API via playback data
            if title.id in self.playback_data and "stream" in self.playback_data[title.id]:
                playback_res = self.playback_data[title.id]

                # Check for editorial markers in playback data
                if "editorial" in playback_res.get("stream", {}):
                    editorial = playback_res["stream"]["editorial"]

                    # Add "Start" chapter if not already present
                    if not any(item.get("offsetMillis") == 0 for item in editorial):
                        chapters.add(Chapter(timestamp=0, name="Start"))

                    # Map editorial labels to chapter names
                    mapping = {
                        "recap_start": "Recap",
                        "FFER": "Recap",  # First Frame Episode Recap
                        "recap_end": "Scene",
                        "LFER": "Scene",  # Last Frame Episode Recap
                        "intro_start": "Title Sequence",
                        "intro_end": "Scene",
                        "FFEI": "Title Sequence",  # First Frame Episode Intro
                        "LFEI": "Scene",  # Last Frame Episode Intro
                        "FFCB": None,  # First Frame Credits Bumper
                        "LFCB": "Scene",  # Last Frame Credits Bumper
                        "FFEC": "End Credits",  # First Frame End Credits
                        "LFEC": None,  # Last Frame End Credits
                        "up_next": None,
                    }

                    # Sort by timestamp to ensure proper scene numbering
                    editorial.sort(key=lambda x: x.get("offsetMillis", 0))

                    # Track chapters we've already added by timestamp to avoid duplicates
                    seen_timestamps = set()
                    scene_count = 0

                    for marker in editorial:
                        if "label" in marker and "offsetMillis" in marker:
                            timestamp = marker["offsetMillis"]
                            name = mapping.get(marker["label"])

                            # Skip if no mapping or already processed timestamp
                            if not name or timestamp in seen_timestamps:
                                continue

                            # Mark this timestamp as seen
                            seen_timestamps.add(timestamp)

                            if name == "Scene":
                                scene_count += 1
                                name = f"Scene {scene_count}"

                            chapters.add(Chapter(timestamp=timestamp, name=name))

                    # If we found chapters in the playback data, return them
                    if chapters:
                        return chapters

            # If no chapters found in playback data, try the original method
            content_id = title.data["partnerFeed"].get("dmcContentId")
            content = self.get_video(content_id)

            # Check for chapter/milestone data
            video_info = content.get("video", {}).get("milestone", {})

            if not video_info:
                return chapters

            # Mapping of milestone types to chapter names
            mapping = {
                "recap_start": "Recap",
                "recap_end": "Scene",
                "intro_start": "Title Sequence",
                "intro_end": "Scene",
                "FFEI": "Title Sequence",  # First Frame Episode Intro
                "LFEI": "Scene",  # Last Frame Episode Intro
                "FFCB": None,  # First Frame Credits Bumper
                "LFCB": "Scene",  # Last Frame Credits Bumper
                "FFEC": "End Credits",  # First Frame End Credits
                "LFEC": None,  # Last Frame End Credits
                "up_next": None,
            }

            # Flatten the milestone data and sort by start time
            flattened = []
            for chapter_type, items in video_info.items():
                for entry in items:
                    if "milestoneTime" in entry and entry["milestoneTime"]:
                        start = entry["milestoneTime"][0]["startMillis"]
                        flattened.append({"type": chapter_type, "start": start})

            flattened.sort(key=lambda x: x["start"])

            # Create chapters
            chapter_list = []
            scene_count = 0
            for f in flattened:
                name = mapping.get(f["type"])
                if not name:
                    continue

                if name == "Scene":
                    scene_count += 1
                    name = f"Scene {scene_count}"

                chapter_list.append(Chapter(timestamp=f["start"], name=name))

            # Add a "Start" chapter at 0 if we have end credits
            if "FFEC" in video_info and not any(ch.timestamp == 0 for ch in chapter_list):
                chapter_list.insert(0, Chapter(timestamp=0, name="Start"))

            # Remove duplicates (same time and name)
            prev_time, prev_name = None, None

            for ch in chapter_list:
                # Convert timestamp to milliseconds for comparison
                if isinstance(ch.timestamp, str):
                    ts_parts = ch.timestamp.split(":")
                    hour, minute, second = int(ts_parts[0]), int(ts_parts[1]), float(ts_parts[2])
                    ts_ms = (hour * 3600 + minute * 60 + second) * 1000
                else:
                    ts_ms = ch.timestamp

                if prev_time is None or (ts_ms != prev_time and ch.name != prev_name):
                    chapters.add(ch)
                    prev_time, prev_name = ts_ms, ch.name

            return chapters

        except Exception as e:
            self.log.warning(f"Failed to extract chapters: {e}")
            return chapters

    def get_playready_license(self, *, challenge: bytes, title, track) -> bytes:
        headers = {
            "Authorization": f"Bearer {self._cache.data['token']['accessToken']}",
            "Content-Type": "application/octet-stream",
        }
        r = self.session.post(url=self.config["PLAYREADY_LICENSE"], headers=headers, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.content

    def get_widevine_license(self, *, challenge: bytes, title, track) -> None:
        headers = {
            "Authorization": f"Bearer {self._cache.data['token']['accessToken']}",
            "Content-Type": "application/octet-stream",
        }
        r = self.session.post(url=self.config["LICENSE"], headers=headers, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.content

    # Service specific functions

    def _show(self, title: str) -> Episode:
        page = self.get_page(title)
        container = next(x for x in page["containers"] if x.get("type") == "episodes")
        season_ids = [x.get("id") for x in container["seasons"] if x.get("type") == "season"]

        episodes = []
        for season in season_ids:
            endpoint = self.href(
                self.prd_config["services"]["explore"]["client"]["endpoints"]["getSeason"]["href"],
                version=self.config["EXPLORE_VERSION"],
                seasonId=season,
            )
            data = self.session.get(endpoint, params={'limit': 999}).json()["data"]["season"]["items"]
            episodes.extend(data)

        return [
            Episode(
                id_=episode.get("id"),
                service=self.__class__,
                title=episode["visuals"].get("title"),
                year=episode["visuals"]["metastringParts"].get("releaseYearRange", {}).get("startYear"),
                season=int(episode["visuals"].get("seasonNumber", 0)),
                number=int(episode["visuals"].get("episodeNumber", 0)),
                name=episode["visuals"].get("episodeTitle"),
                language=self.get_original_lang(next(x for x in episode["actions"] if x.get("type") == "playback").get("availId")),
                data=next(x for x in episode["actions"] if x.get("type") == "playback"),
            )
            for episode in episodes
            if episode.get("type") == "view"
        ]

    def _movie(self, title: str) -> Movie:
        movie = self.get_page(title)

        playback_action = next(x for x in movie["actions"] if x.get("type") == "playback")
        original_lang = self.get_original_lang(playback_action.get("availId"))

        return [
            Movie(
                id_=movie.get("id"),
                service=self.__class__,
                name=movie["visuals"].get("title"),
                year=movie["visuals"]["metastringParts"].get("releaseYearRange", {}).get("startYear"),
                language=original_lang,
                data=playback_action,
            )
        ]

    def get_original_lang(self, availId):
        try:
            title_lang = self.session.get(f'https://disney.api.edge.bamgrid.com/explore/v1.6/playerExperience/{availId}').json()
            original_lang = title_lang["data"]["playerExperience"]["targetLanguage"]
        except Exception:
            original_lang = "en"
        return original_lang

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict = None,
        headers: dict = None,
        payload: dict = None,
    ) -> Any[dict | str]:
        _headers = {**self.session.headers, **(headers or {})}

        prep = self.session.prepare_request(Request(method, endpoint, headers=_headers, params=params, json=payload))
        response = self.session.send(prep)

        try:
            data = response.json()

            if data.get("errors"):
                code = data["errors"][0]["extensions"].get("code")

                if "token.service.unauthorized.client" in code:
                    raise ConnectionError("Unauthorized Client/IP: " + code)
                if "idp.error.identity.bad-credentials" in code:
                    raise ConnectionError("Bad Credentials: " + code)
                else:
                    raise ConnectionError(data["errors"])

            return data

        except Exception:
            raise ConnectionError("Request failed: {}".format(response.content))

    def get_page(self, title):
        params = {
            "disableSmartFocus": "true",
            "limit": 999,
            "enhancedContainersLimit": 0,
        }
        endpoint = self.href(
            self.prd_config["services"]["explore"]["client"]["endpoints"]["getPage"]["href"],
            version=self.config["EXPLORE_VERSION"],
            pageId=title,
        )
        return self._request("GET", endpoint, params=params)["data"]["page"]

    def get_video(self, content_id: str) -> dict:
        endpoint = self.href(
            self.prd_config["services"]["content"]["client"]["endpoints"]["getDmcVideo"]["href"], contentId=content_id
        )
        return self._request("GET", endpoint)["data"]["DmcVideo"]

    def get_deeplink(self, ref_id: str) -> str:
        params = {
            "refId": ref_id,
            "refIdType": "deeplinkId",
        }
        endpoint = "https://disney.content.edge.bamgrid.com/explore/v1.0/deeplink"
        return self._request("GET", endpoint, params=params)

    def series_bundle(self, series_id: str) -> dict:
        endpoint = self.href(
            self.prd_config["services"]["content"]["client"]["endpoints"]["getDmcSeriesBundle"]["href"],
            encodedSeriesId=series_id,
        )

        return self.session.get(endpoint).json()["data"]["DmcSeriesBundle"]

    def refresh_token(self, refresh_token: str):
        payload = {
            "operationName": "refreshToken",
            "variables": {
                "input": {
                    "refreshToken": refresh_token,
                },
            },
            "query": queries.REFRESH_TOKEN,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["refreshToken"]["href"]
        data = self._request("POST", endpoint, payload=payload, headers={"authorization": self.config["API_KEY"]})
        return data["extensions"]["sdk"]

    def _refresh(self):
        if not self._cache.expired:
            return self._cache.data["token"]["accessToken"]

        profile = self.refresh_token(self._cache.data["token"]["refreshToken"])
        self._cache.set(profile, expiration=profile["token"]["expiresIn"] - 30)
        return self._cache.data["token"]["accessToken"]

    def register_device(self) -> dict:
        payload = {
            "variables": {
                "registerDevice": {
                    "applicationRuntime": self.config["APPLICATION_RUNTIME"],
                    "attributes": {
                        "operatingSystem": "Android",
                        "operatingSystemVersion": "8.1.0",
                    },
                    "deviceFamily": self.config["DEVICE_FAMILY"],
                    "deviceLanguage": "en",
                    "deviceProfile": self.config["DEVICE_PROFILE"],
                }
            },
            "query": queries.REGISTER_DEVICE,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["registerDevice"]["href"]
        data = self._request("POST", endpoint, payload=payload, headers={"authorization": self.config["API_KEY"]})
        return data["extensions"]["sdk"]["token"]["accessToken"]

    def login(self, email: str, password: str, token: str) -> dict:
        payload = {
            "operationName": "loginTv",
            "variables": {
                "input": {
                    "email": email,
                    "password": password,
                },
            },
            "query": queries.LOGIN,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        data = self._request("POST", endpoint, payload=payload, headers={"authorization": token})
        return data["extensions"]["sdk"]["token"]

    def href(self, href, **kwargs) -> str:
        _args = {
            "apiVersion": "{apiVersion}",
            "region": self.active_session["location"]["countryCode"],
            "impliedMaturityRating": 1850,
            "kidsModeEnabled": "false",
            "appLanguage": "en-US",
            "partner": "disney",
        }
        _args.update(**kwargs)

        href = href.format(**_args)

        # [3.0, 3.1, 3.2, 5.0, 3.3, 5.1, 6.0, 5.2, 6.1]
        api_version = "6.1"
        if "/search/" in href:
            api_version = "5.1"

        return href.format(apiVersion=api_version)

    def check_email(self, email: str, token: str) -> str:
        payload = {
            "operationName": "Check",
            "variables": {
                "email": email,
            },
            "query": queries.CHECK_EMAIL,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        data = self._request("POST", endpoint, payload=payload, headers={"authorization": token})
        return data["data"]["check"]["operations"][0]

    def account(self) -> dict:
        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]

        payload = {
            "operationName": "EntitledGraphMeQuery",
            "variables": {},
            "query": queries.ENTITLEMENTS,
        }

        data = self._request("POST", endpoint, payload=payload)
        return data["data"]["me"]

    def switch_profile(self, profile_id: str) -> dict:
        payload = {
            "operationName": "switchProfile",
            "variables": {
                "input": {
                    "profileId": profile_id,
                },
            },
            "query": queries.SWITCH_PROFILE,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        data = self._request("POST", endpoint, payload=payload)
        return data["extensions"]["sdk"]
