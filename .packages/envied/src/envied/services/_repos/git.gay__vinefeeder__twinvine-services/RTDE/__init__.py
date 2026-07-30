import base64
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Optional, Union
from urllib.parse import parse_qs, urlparse

import click
from langcodes import Language

from envied.core.config import config
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.session import session as create_curl_session
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Subtitle, Tracks


class RTDE(Service):
    """
    Service for RTL+ Germany (https://plus.rtl.de)

    \b
    Author: Claude
    Authorization: None (free) / Credential (premium)
    Robustness: HD@L3
    \b
    """

    ALIASES = ("RTDE", "RTLPlus", "rtl+", "rtlplus")
    GEOFENCE = ("de",)

    TITLE_RE = r"https?://(?:www\.)?plus\.rtl\.de/video-tv/(?:shows|filme|serien)/.*?-(\d+)"

    GRAPHQL_URL = "https://cdn.gateway.now-plus-prod.aws-cbc.cloud/graphql"
    PLAYOUT_URL = "https://stus.player.streamingtech.de/watch-playout-variants"
    AUTH_URL = "https://auth.rtl.de/auth/realms/rtlplus/protocol/openid-connect/token"

    GRAPHQL_HASHES = {
        "Format": "d112638c0184ab5698af7b69532dfe2f12973f7af9cb137b9f70278130b1eafa",
        "SeasonWithFormatAndEpisodes": "cc0fbbe17143f549a35efa6f8665ceb9b1cfae44b590f0b2381a9a304304c584",
        "Episode": "87dbde15a0d269b11606f5ff458d555e98eb493bb4fb6ddc150d812d5e9a9cf8",
        "EpisodeDetail": "2e5ef142c79f8620e8e93c8f21b31a463b16d89a557f7f5f0c4a7e063be96a8a",
    }

    @staticmethod
    def get_session():
        return create_curl_session()

    @staticmethod
    @click.command(name="RTLPlus", short_help="https://plus.rtl.de")
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Specify if content is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return RTDE(ctx, **kwargs)

    def __init__(self, ctx, title: str, movie: bool = False):
        self.title = title
        self.movie = movie
        super().__init__(ctx)

        m = re.search(self.TITLE_RE, title)
        if m:
            self.format_id = m.group(1)
        else:
            # Assume raw ID was passed
            self.format_id = title.rstrip("/").split("-")[-1]
            if not self.format_id.isdigit():
                self.format_id = title

        self.device_id = str(uuid.uuid4())
        self.auth_token = None
        self.license_url = None
        self.pr_license_url = None

        self.cache_file = Path(config.directories.cache / "RTDE" / "token.json")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

        if credential:
            self.auth_token = self._login(credential)
            self.log.info("Authenticated with credentials")
        else:
            cached = self._load_cached_token()
            if cached:
                self.auth_token = cached
                self.log.info("Using cached auth token")
            else:
                self.auth_token = self._get_anonymous_token()
                self.log.info("Using anonymous token (free content only)")

        self.session.headers.update({
            "Authorization": f"Bearer {self.auth_token}",
            "x-auth-token": self.auth_token,
            "x-device-id": self.device_id,
            "x-device-name": "Mac OS Chrome",
            "x-device-type": "web",
            "rtlplus-client-id": "rci:rtlplus:web",
            "rtlplus-client-version": "2026.2.17.0",
        })

    def _load_cached_token(self) -> Optional[str]:
        if not self.cache_file.exists():
            return None
        try:
            data = json.loads(self.cache_file.read_text())
            token = data.get("token")
            if token and self._is_token_valid(token):
                return token
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    def _save_token(self, token: str):
        self.cache_file.write_text(json.dumps({"token": token}, indent=2))

    @staticmethod
    def _is_token_valid(token: str) -> bool:
        try:
            payload = token.split(".")[1] + "=="
            data = json.loads(base64.urlsafe_b64decode(payload))
            return data.get("exp", 0) > time.time() + 60
        except Exception:
            return False

    def _get_anonymous_token(self) -> str:
        r = self.session.post(
            self.AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": "anonymous-user",
                "client_secret": "4bfeb73f-1c4a-4e9f-a7fa-96aa1ad3d94c",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        self._save_token(token)
        return token

    def _login(self, credential: Credential) -> str:
        # Authorization Code Flow with PKCE
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()

        state = str(uuid.uuid4())
        nonce = str(uuid.uuid4())

        # Step 1: GET authorization page
        auth_endpoint = "https://auth.rtl.de/auth/realms/rtlplus/protocol/openid-connect/auth"
        r = self.session.get(auth_endpoint, params={
            "client_id": "rtlplus-web",
            "redirect_uri": "https://plus.rtl.de/",
            "state": state,
            "response_mode": "query",
            "response_type": "code",
            "scope": "openid email profile",
            "nonce": nonce,
            "prompt": "login",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }, allow_redirects=True)
        if not r.ok:
            raise ConnectionError(f"Auth page failed: {r.status_code}")

        # Parse form action URL and hidden fields
        html = r.text
        action_match = re.search(r'action="([^"]+)"', html)
        if not action_match:
            raise ConnectionError("Could not find login form action URL")
        action_url = action_match.group(1).replace("&amp;", "&")

        # Step 2: POST login credentials
        r = self.session.post(
            action_url,
            data={
                "username": credential.username,
                "password": credential.password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )

        # Follow redirects manually to capture the authorization code
        while r.status_code in (301, 302, 303):
            location = r.headers.get("Location", "")
            parsed = urlparse(location)
            params = parse_qs(parsed.query)

            if "code" in params:
                auth_code = params["code"][0]
                break

            r = self.session.get(location, allow_redirects=False)
        else:
            raise ConnectionError(f"Login failed: no authorization code in redirect (HTTP {r.status_code})")

        # Step 3: Exchange code for tokens
        r = self.session.post(
            self.AUTH_URL,
            data={
                "code": auth_code,
                "grant_type": "authorization_code",
                "client_id": "rtlplus-web",
                "redirect_uri": "https://plus.rtl.de/",
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not r.ok:
            raise ConnectionError(f"Token exchange failed: {r.status_code} {r.text[:200]}")

        token = r.json()["access_token"]
        self._save_token(token)
        return token

    def _graphql(self, operation: str, variables: dict) -> dict:
        r = self.session.get(
            self.GRAPHQL_URL,
            params={
                "operationName": operation,
                "variables": json.dumps(variables),
                "extensions": json.dumps({
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": self.GRAPHQL_HASHES[operation],
                    }
                }),
            },
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "referer": "https://plus.rtl.de/",
                "origin": "https://plus.rtl.de",
            },
        )
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise ValueError(f"GraphQL error: {data['errors']}")
        return data["data"]

    def get_titles(self) -> Titles_T:
        rrn = f"rrn:watch:videohub:format:{self.format_id}"
        data = self._graphql("Format", {"id": rrn})
        fmt = data["format"]

        title_name = fmt["title"]
        year = fmt.get("productionYear", "")

        if self.movie:
            # For movies, the format itself contains a single episode
            episodes = fmt.get("episodes", [])
            if episodes:
                ep = episodes[0]
                ep_id = ep["id"].split(":")[-1]
            else:
                ep_id = self.format_id

            return Movies([
                Movie(
                    id_=ep_id,
                    name=title_name,
                    year=year,
                    language="de",
                    service=self.__class__,
                    data={"rrn": f"rrn:watch:videohub:episode:{ep_id}", "format": fmt},
                )
            ])

        # Series: iterate all seasons
        seasons = fmt.get("seasons", [])
        all_episodes = []

        for season in seasons:
            season_id = season["id"]
            season_ordinal = season.get("ordinal", 0)
            season_title_override = season.get("titleOverride")
            num_episodes = season.get("numberOfEpisodes", 0)

            offset = 0
            while offset < num_episodes:
                result = self._graphql("SeasonWithFormatAndEpisodes", {
                    "seasonId": season_id,
                    "offset": offset,
                    "limit": 50,
                })
                eps = result.get("season", {}).get("episodes", [])
                if not eps:
                    break

                for ep in eps:
                    ep_id = ep["id"].split(":")[-1]
                    ep_number = ep.get("number", 0)
                    ep_title = ep.get("title", "")

                    all_episodes.append(Episode(
                        id_=ep_id,
                        service=self.__class__,
                        title=title_name,
                        year=year,
                        season=season_ordinal,
                        number=ep_number,
                        name=ep_title,
                        language="de",
                        data={
                            "rrn": ep["id"],
                            "tier": ep.get("tier", "FREE"),
                            "season_title": season_title_override,
                        },
                    ))

                offset += len(eps)

        return Series(all_episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        rrn = title.data.get("rrn", f"rrn:watch:videohub:episode:{title.id}")

        r = self.session.get(
            f"{self.PLAYOUT_URL}/{rrn}",
            params={"platform": "web"},
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://plus.rtl.de",
                "referer": "https://plus.rtl.de/",
            },
        )
        r.raise_for_status()
        variants = r.json()

        # Prefer HD DASH variant, fall back to SD
        dash_variant = None
        for name in ("dashhd", "dashsd"):
            dash_variant = next((v for v in variants if v["name"] == name), None)
            if dash_variant:
                break

        if not dash_variant:
            raise ValueError("No DASH variant found in playout response")

        # Get MPD URL (prefer MAIN source)
        sources = dash_variant.get("sources", [])
        mpd_url = None
        subtitles_data = []
        for src in sources:
            if src.get("priority") == "MAIN" or not mpd_url:
                mpd_url = src["url"]
                subtitles_data = src.get("subtitles", [])

        if not mpd_url:
            raise ValueError("No MPD URL found in DASH variant")

        self.log.debug(f"MPD URL: {mpd_url}")

        # Store license URLs
        for lic in dash_variant.get("licenses", []):
            lic_type = lic.get("type", "")
            lic_url = lic.get("uri", {}).get("href", "")
            if lic_type == "WIDEVINE":
                self.license_url = lic_url
            elif lic_type == "PLAYREADY":
                self.pr_license_url = lic_url

        # Parse DASH manifest
        tracks = DASH.from_url(url=mpd_url, session=self.session).to_tracks(language=Language.get("de"))

        # Add subtitles from playout response
        for sub in (subtitles_data or []):
            sub_url = sub.get("url", "")
            lang_code = sub.get("languageCode", "deu")
            output_set = sub.get("outputSet", "")

            if output_set == "WEBVTT_TEXT":
                codec = Subtitle.Codec.WebVTT
            elif output_set == "TTML_XML":
                codec = Subtitle.Codec.TimedTextMarkupLang
            else:
                continue

            tracks.add(Subtitle(
                id_=hashlib.md5(sub_url.encode()).hexdigest()[:6],
                url=sub_url,
                codec=codec,
                language=lang_code,
                is_original_lang=True,
            ))

        return tracks

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Union[bytes, str]:
        if not self.license_url:
            raise ValueError("No Widevine license URL available")

        r = self.session.post(
            self.license_url,
            data=challenge,
            headers={
                "x-auth-token": self.auth_token,
                "x-device-id": self.device_id,
                "x-device-name": "Mac OS Chrome",
                "origin": "https://plus.rtl.de",
                "referer": "https://plus.rtl.de/",
            },
        )
        if not r.ok:
            raise ConnectionError(f"License request failed: HTTP {r.status_code} {r.text[:200]}")
        return r.content

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Union[bytes, str]:
        if not self.pr_license_url:
            raise ValueError("No PlayReady license URL available")

        if isinstance(challenge, bytes):
            challenge = challenge.decode("utf-8", errors="replace")
        # Strip anything before the SOAP envelope
        soap_idx = challenge.find("<soap:Envelope")
        if soap_idx >= 0:
            challenge = challenge[soap_idx:]

        r = self.session.post(
            self.pr_license_url,
            data=challenge,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "x-auth-token": self.auth_token,
                "x-device-id": self.device_id,
                "x-device-name": "Mac OS Chrome",
                "origin": "https://plus.rtl.de",
                "referer": "https://plus.rtl.de/",
            },
        )
        if not r.ok:
            raise ConnectionError(f"PlayReady license request failed: HTTP {r.status_code} {r.text[:200]}")
        return r.content

    def get_widevine_service_certificate(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        if not self.license_url:
            return None

        r = self.session.post(
            self.license_url,
            data=challenge,
            headers={
                "x-auth-token": self.auth_token,
                "x-device-id": self.device_id,
                "x-device-name": "Mac OS Chrome",
                "origin": "https://plus.rtl.de",
                "referer": "https://plus.rtl.de/",
            },
        )
        if not r.ok:
            return None
        return r.content

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()
