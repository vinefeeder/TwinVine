from __future__ import annotations

import base64
import concurrent.futures
import json
import re
import time
import uuid
from collections.abc import Generator
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional
from urllib.parse import urlparse

import click
import jwt
from click import Context
from langcodes import Language
from lxml import etree
from rich.prompt import Prompt
from envied.core import __version__
from envied.core.cdm.detect import is_playready_cdm
from envied.core.config import config
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Tracks


class TVNZ(Service):
    """
    \b
    Service code for TVNZ streaming service (https://www.tvnz.co.nz).

    \b
    Version: 2.0.3
    Author: stabbedbybrick
    Authorization: Credentials (email + OTP)
    Robustness:
      Widevine:
        L3: 1080p, DDP5.1
      PlayReady:
        SL2000: 1080p, DDP5.1

    \b
    Tips:
        - Input can be comlete URL or path:
          SHOW: /tvseries/the-rookie
          EPISODE: /player/tvepisode/the-rookie-1
          MOVIE: /movie/stand-by-me
          EVENT: /player/event/bula-fc-v-auckland-fc
          HIGHLIGHT: /player/sporthighlight/sheep-dog-trials
          CLIP: /player/newsclip/drone-captures-slip-that-caused-evacuations-blocked-auckland-road

    \b
    Notes:
        - TVNZ has moved to an OTP-only login system, with no username/password and no cookies.
          On first run with a new profile, the OTP code will be sent to the email address listed in the config
          and you will be prompted to enter it. This is only needed once, subsequent logins will use the cached tokens.
        - Since there are no passwords, simply set the password as 'none' in the config so Unshackle
          doesn't trip on incorrect formats: 'username:password' -> 'username:none'.
    """

    GEOFENCE = ("nz",)
    TITLE_RE = r"^(?:https?://(?:www\.)?tvnz\.co\.nz)?/(?:player/)?(tvseries|tvepisode|movie|event|sporthighlight|newsclip|sportclip)/([^/]+)$"

    @staticmethod
    @click.command(name="TVNZ", short_help="https://tvnz.co.nz", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> TVNZ:
        return TVNZ(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        super().__init__(ctx)
        self.title = title

        self.drm_system = "playready" if is_playready_cdm(ctx.obj.cdm) else "widevine"

        self.profile = ctx.parent.params.get("profile") or "default"
        self.session.headers.update(self.config["headers"])

        # handle cache and OTP input before calling authenticate() to avoid glitchy terminal
        self.credential = self.get_credentials(self.__class__.__name__, self.profile)
        if not self.credential:
            self.log.error(f" - No credentials found for profile: {self.profile}")
            exit(1)

        self.cached_tokens = self.cache.get(f"tokens_{self.credential.sha1}")
        if not self.cached_tokens:
            self.log.info(" - No cached user tokens found, setting up new login...")
            self._create_otp(self.credential.username)

            self.log.info(" + OTP code was sent to your email address")
            self.otp_input = Prompt.ask("\tEnter OTP code")

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "mode": "detail",
            "st": "published",
            "term": self.title,
            "pageNumber": "1",
            "pageSize": "50",
            "reg": "nz",
            "dt": "androidtv",
            "client": "tvnz-tvnz-androidtv",
            "pf": "Regular",
            "allowpg": "true",
        }

        response = self.session.get(self.config["endpoints"]["search"], params=params).json()
        if response.get("header", {}).get("message", "").lower() != "success":
            self.log.error(f"Failed to get search results for '{self.title}'")
            return

        results = response.get("data", [])

        for result in results:
            content_type = result.get("cty")
            content_id = result.get("nu")
            title = result.get("lon", [{"n": ""}])[0].get("n")
            synopsis = result.get("losd", [{"n": ""}])[0].get("n")
            yield SearchResult(
                id_=f"https://tvnz.co.nz/{content_type}/{content_id}",
                title=title,
                description=synopsis,
                label=content_type,
                url=f"https://tvnz.co.nz/{content_type}/{content_id}",
            )

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        if not self.cached_tokens:
            if not self.otp_input:
                raise ValueError("OTP code not provided")

            otp = self.otp_input.replace(" ", "")
            device_id = str(uuid.uuid4())
            confirmation = self._confirm_otp(self.credential.username, otp, device_id)
            if not (params := confirmation.get("params", [])):
                raise ValueError("OTP response is missing auth params")

            tokens = {p.get("paramName"): p.get("paramValue") for p in params}
            tokens["contactID"] = confirmation.get("contactID")
            tokens["deviceID"] = device_id
            self.cached_tokens.set(tokens, expiration=int(tokens["expiresIn"]) - 3600)

        else:
            if not self.cached_tokens.expired:
                self.log.info(" + Using cached user tokens")
                tokens = self.cached_tokens.data
            else:
                self.log.info(" + Refreshing cached user tokens")
                tokens = self.cached_tokens.data.copy()
                refreshed_data = self._refresh_user_tokens(self.cached_tokens.data)
                tokens.update(refreshed_data)
                self.cached_tokens.set(tokens, expiration=int(tokens["expiresIn"]) - 3600)

        self.access_token = tokens["accessToken"]
        self.device_id = tokens["deviceID"]
        self.contact_id = tokens["contactID"]

        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

        self.xauthorization, _ = self._get_entitlements(self.contact_id)
        self.oauth_token = self._get_oauth_token()
        self.secret = self._register_app(self.oauth_token, self.xauthorization, self.device_id)

    def get_titles(self) -> Movies | Series:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(f"Invalid title: {self.title}")

        content_type, _ = match.groups()
        title_path = urlparse(self.title).path.replace("/player/", "/")

        if content_type == "tvseries":
            return self._get_series(title_path)
        elif content_type == "tvepisode":
            return self._get_episode(title_path)
        elif content_type == "movie":
            return self._get_movie(title_path)
        elif content_type in ("event", "sporthighlight", "newsclip", "sportclip"):
            return self._get_single(title_path)
        else:
            raise ValueError(f"Unsupported content type: {content_type}")

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        device_token = self._get_device_token(self.secret, self.device_id)

        headers = {
            "authorization": f"Bearer {self.oauth_token}",
            "x-authorization": f"{self.xauthorization}",
            "x-device-id": f"{device_token}",
        }

        json_data = {
            "deviceName": "lgwebostv" if self.drm_system == "playready" else "androidtv",
            "deviceId": self.device_id,
            "deviceManufacturer": "Android TV",
            "deviceModelName": "Android TV",
            "deviceOs": "Android",
            "deviceOsVersion": "10",
            "contentId": title.id,
            "mediaFormat": "dash",
            "contentTypeId": "vod",
            "catalogType": title.data.get("cty"),
            "drm": self.drm_system,
            "delivery": "streaming",
            "quality": "high",
            "disableSsai": "true",
            "supportedResolution": "UHD",
            "supportedAudioCodecs": "mp4a",
            "supportedVideoCodecs": "avc,hevc,av01",
            "supportedMaxWVSecurityLevel": "L1",
        }

        response = self.session.post(
            url=self.config["endpoints"]["authorize"],
            headers=headers,
            json=json_data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("header", {}).get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to authorize playback: {data}")

        title.data["license_url"] = data.get("data", {}).get("licenseUrl")
        title.data["markers"] = title.data.get("mar")

        source_manifest = data.get("data", {}).get("contentUrl").split("?")[0]

        # Temporary workaround for non-development branch
        if __version__ < "5.0.0":
            manifest = self._modify_transfer(source_manifest)
            tracks = DASH.from_text(url=source_manifest, text=manifest).to_tracks(language=title.language)

        else:
            tracks = DASH.from_url(source_manifest, self.session).to_tracks(language=title.language)

        for track in tracks.audio:
            role = track.data["dash"]["adaptation_set"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        if not (markers := title.data.get("markers")):
            return Chapters()

        chapters = []
        for marker in markers:
            if marker.get("t", "").lower() == "postplay":
                chapters.append(Chapter(name="Credits", timestamp=marker.get("m_st") * 1000))
            else:
                chapters.append(Chapter(name=marker.get("t"), timestamp=marker.get("m_st") * 1000))
                chapters.append(Chapter(timestamp=marker.get("m_ed")))

        if not any(c.timestamp == "00:00:00.000" for c in chapters):
            chapters.append(Chapter(timestamp=0))

        return sorted(chapters, key=lambda x: x.timestamp)

    def get_widevine_service_certificate(
        self, *, challenge: bytes, title: Episode | Movie, track: Any
    ) -> bytes | str | None:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (license_url := title.data.get("license_url")):
            return None

        headers = {"authorization": "Bearer {}".format(self.oauth_token)}
        r = self.session.post(url=license_url, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content

    def get_playready_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (license_url := title.data.get("license_url")):
            return None

        headers = {"authorization": "Bearer {}".format(self.oauth_token)}
        r = self.session.post(url=license_url, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content

    # Service-specific methods

    def _fetch_season_episodes(self, series_id: str, season_id: str) -> list[Episode]:
        try:
            response = self.session.get(
                url=self.config["endpoints"]["episodes"].format(series_id=series_id),
                params={
                    "seasonId": season_id,
                    "pageNumber": "1",
                    "pageSize": "99",
                    "sortBy": "epnum",
                    "sortOrder": "asc",
                    "reg": "nz",
                    "dt": "androidtv",
                    "client": "tvnz-tvnz-androidtv",
                    "pf": "Regular",
                    "allowpg": "true",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            if not (episodes := data.get("data")):
                self.log.error(f"Failed to get episodes for season {season_id}")
                return []

            return [
                Episode(
                    id_=episode.get("nu"),
                    service=self.__class__,
                    title=episode.get("lostl")[0].get("n"),
                    season=episode.get("snum"),
                    number=episode.get("epnum"),
                    name=episode.get("lodn")[0].get("n"),
                    # year=episode.get("oadt", "").split("-")[0],
                    language=Language.find(episode.get("aud_lg", ["English"])[0]).to_alpha3(),
                    data=episode,
                )
                for episode in episodes
            ]
        except Exception as e:
            self.log.error(f"Failed to fetch season {season_id}: {e}")
            return []

    def _get_series(self, title_path: str) -> Series:
        response = self.session.get(
            url=self.config["endpoints"]["catalog"] + title_path,
            params={
                "reg": "nz",
                "dt": "androidtv",
                "client": "tvnz-tvnz-androidtv",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (series_id := data.get("data", {}).get("id")):
            raise ValueError(f"Failed to get series ID: {data}")

        response = self.session.get(
            url=self.config["endpoints"]["seasons"].format(series_id=series_id),
            params={
                "pageNumber": "1",
                "pageSize": "99",
                "sortBy": "asc",
                "sortOrder": "desc",
                "reg": "nz",
                "dt": "androidtv",
                "client": "tvnz-tvnz-androidtv",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        seasons = [x.get("id") for x in data["data"]]
        if not seasons:
            raise ValueError(f"Failed to get seasons: {data}")

        all_episodes = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(self._fetch_season_episodes, series_id, season) for season in seasons]
            for future in futures:
                all_episodes.extend(future.result())

        return Series(all_episodes)

    def _get_movie(self, title_path: str) -> Movies:
        response = self.session.get(
            url=self.config["endpoints"]["catalog"] + title_path,
            params={
                "reg": "nz",
                "dt": "androidtv",
                "client": "tvnz-tvnz-androidtv",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (video := data.get("data")):
            raise ValueError(f"Failed to get movie: {data}")

        movies = [
            Movie(
                id_=video.get("nu"),
                service=self.__class__,
                name=video.get("lodn", [{"n": ""}])[0].get("n"),
                year=None,
                language=Language.find(video.get("aud_lg", ["English"])[0]).to_alpha3(),
                data=video,
            )
        ]

        return Movies(movies)

    def _get_episode(self, title_path: str) -> Series:
        response = self.session.get(
            url=self.config["endpoints"]["catalog"] + title_path,
            params={
                "reg": "nz",
                "dt": "androidtv",
                "client": "tvnz-tvnz-androidtv",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (video := data.get("data")):
            raise ValueError(f"Failed to get episode: {data}")

        episodes = [
            Episode(
                id_=video.get("nu"),
                service=self.__class__,
                title=video.get("lostl")[0].get("n"),
                season=video.get("snum"),
                number=video.get("epnum"),
                name=video.get("lodn", [{"n": ""}])[0].get("n"),
                # year=video.get("oadt", "").split("-")[0],
                language=Language.find(video.get("aud_lg", ["English"])[0]).to_alpha3(),
                data=video,
            )
        ]

        return Series(episodes)

    def _get_single(self, title_path: str) -> Movies:
        response = self.session.get(
            url=self.config["endpoints"]["catalog"] + title_path,
            params={
                "reg": "nz",
                "dt": "androidtv",
                "client": "tvnz-tvnz-androidtv",
                "pf": "Regular",
                "allowpg": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (video := data.get("data")):
            raise ValueError(f"Failed to get episode: {data}")

        events = [
            Movie(
                id_=video.get("nu"),
                service=self.__class__,
                name=video.get("lodn", [{"n": ""}])[0].get("n"),
                year=video.get("r"),
                language=Language.find(video.get("aud_lg", ["English"])[0]).to_alpha3(),
                data=video,
            )
        ]

        return Movies(events)

    @staticmethod
    def _get_device_token(secret_b64: str, device_id: str) -> str:
        secret_bytes = base64.b64decode(secret_b64)

        payload = {
            "deviceId": device_id,
            "aud": "playback-auth-service",
            "iat": int(time.time()),
            "exp": int(time.time()) + 30,
        }

        device_token = jwt.encode(payload, secret_bytes, algorithm="HS256")
        return device_token

    def _get_entitlements(self, contact_id: str):
        json_data = {
            "GetEntitlementsRequestMessage": {
                "contactID": contact_id,
                **self.config["contact"],
                "returnUpgradableFlag": "true",
                "returnProductAttributes": "true",
            },
        }

        response = self.session.post(
            url=self.config["endpoints"]["entitlements"],
            json=json_data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("GetEntitlementsResponseMessage").get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to get entitlements: {data}")

        token = data.get("GetEntitlementsResponseMessage").get("ovatToken")
        expiry = data.get("GetEntitlementsResponseMessage").get("ovatTokenExpiry")
        if not token:
            raise ValueError(f"Failed to get entitlements: {data}")

        return token, expiry

    def _get_oauth_token(self) -> str:
        data = {
            **self.config["androidtv_client"],
            "grant_type": "client_credentials",
            "audience": "edge-service",
            "scope": "offline openid",
        }

        response = self.session.post(
            url=self.config["endpoints"]["oauth"],
            data=data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not (token := data.get("access_token")):
            raise ValueError("Failed to get OAuth token")

        return token

    def _register_app(self, oauth_token: str, xauth: str, device_id: str) -> str:
        headers = {
            "authorization": f"Bearer {oauth_token}",
            "x-authorization": f"{xauth}",
        }

        response = self.session.post(
            url=self.config["endpoints"]["register"],
            headers=headers,
            data=json.dumps({"uniqueId": device_id}),
            timeout=30,
        )
        response.raise_for_status()
        registration = response.json()

        if not (secret := registration.get("data", {}).get("secret")):
            raise ValueError(f"Failed to register app: {registration}")

        return secret

    def _refresh_user_tokens(self, tokens: dict) -> tuple[dict, int]:
        json_data = {
            "RefreshTokenRequestMessage": {
                **self.config["contact"],
                "refreshToken": tokens.get("refreshToken"),
            },
        }

        response = self.session.post(
            url=self.config["endpoints"]["refresh"],
            json=json_data,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("RefreshTokenResponseMessage", {}).get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to refresh user tokens: {data}")

        return data.get("RefreshTokenResponseMessage")

    def _create_otp(self, email: str) -> None:
        json_data = {
            "CreateOTPRequestMessage": {
                **self.config["contact"],
                "email": email,
            },
        }
        response = self.session.post("https://rest-prod-tvnz.evergentpd.com/tvnz/createOTP", json=json_data).json()
        response_message = response.get("CreateOTPResponseMessage", {})
        if not response_message.get("isUserExist", False):
            raise Exception(f"User with email {email} not found. Please check your credentials.")
        if response_message.get("status", "").lower() != "success":
            raise Exception(f"Failed to create OTP: {response}")

        return

    def _confirm_otp(self, email: str, otp: str, device_id: str) -> dict:
        json_data = {
            "ConfirmOTPRequestMessage": {
                **self.config["contact"],
                "email": email,
                "canCreateAccount": True,
                "checkDeviceLimit": True,
                "dmaId": "001",
                "otp": otp,
                "isGenerateJWT": True,
                "isPrivacyPoliciesAccepted": True,
                "isTAndCAccepted": True,
                "deviceDetails": {
                    "deviceType": "Android TV",
                    "deviceName": "androidtv",
                    "modelNo": "Android TV",
                    "appType": "Android",
                    "serialNo": device_id,
                },
            },
        }

        response = self.session.post("https://rest-prod-tvnz.evergentpd.com/tvnz/confirmOTP", json=json_data).json()
        response_message = response.get("ConfirmOTPResponseMessage", {})
        if response_message.get("status", "").lower() != "success":
            raise Exception(f"Failed to confirm OTP: {response}")

        return response.get("ConfirmOTPResponseMessage", {})

    @staticmethod
    def get_credentials(service: str, profile: Optional[str]) -> Optional[Credential]:
        """We need this method here to avoid circular imports."""
        credentials = config.credentials.get(service)
        if credentials:
            if isinstance(credentials, dict):
                if profile:
                    credentials = credentials.get(profile) or credentials.get("default")
                else:
                    credentials = credentials.get("default")
            if credentials:
                if isinstance(credentials, list):
                    return Credential(*credentials)
                return Credential.loads(credentials)  # type: ignore

    def _modify_transfer(self, source_manifest: str) -> str:
        """Change transfer type to "2" until dev branch is merged."""
        manifest = DASH.from_url(source_manifest, self.session).manifest
        periods = manifest.findall("Period")
        for period in periods:
            for adaptation_set in period.findall("AdaptationSet"):
                for prop in adaptation_set.findall("SupplementalProperty"):
                    if prop is not None and prop.get("schemeIdUri") == "urn:mpeg:mpegB:cicp:TransferCharacteristics":
                        prop.set("value", "2")

        return etree.tostring(manifest, encoding="unicode")
