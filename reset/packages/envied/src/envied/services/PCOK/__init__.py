from __future__ import annotations
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Optional
from urllib.parse import urlparse
import click
from langcodes import Language
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapters, Tracks, Video


class PCOK(Service):
    """
    Service code for Peacock TV (https://peacocktv.com)
    
    Author: n0stal6ic
    Authorization: Cookies, Credentials
    Geofence: US
    """

    ALIASES = ("PCOK", "peacock", "peacocktv")
    GEOFENCE = ("US",)
    TITLE_RE = [
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset)?(?P<id>/movies/[a-z0-9_./-]+/[a-f0-9-]{36})",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset)?(?P<id>/tv/[a-z0-9_./-]+/[a-f0-9-]{36})",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset)?(?P<id>/tv/[a-z0-9_./-]+/\d+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset)?(?P<id>/news/[a-z0-9_./-]+/[a-f0-9-]{36})",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset)?(?P<id>/-/[a-z0-9_./-]+/\d+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/stream-tv/)?(?P<id>[a-z0-9-]+)$",
    ]

    @staticmethod
    @click.command(name="PCOK", short_help="https://peacocktv.com")
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return PCOK(ctx, **kwargs)

    def __init__(self, ctx, title: str, movie: bool):
        super().__init__(ctx)

        self.title = title
        self.movie = movie

        range_param = ctx.parent.params.get("range_")
        self.range = range_param[0].name if range_param else "SDR"

        vcodec_param = ctx.parent.params.get("vcodec")
        self.vcodec = vcodec_param if vcodec_param else "H264"

        self.profile_name = ctx.parent.params.get("profile") or "default"

        prof_key = self.config["client"].get("profile", "tv")
        profiles = self.config.get("profiles", {})
        if prof_key not in profiles:
            raise ValueError(f"Unknown device profile {prof_key!r}. Valid: {list(profiles)}")
        self.prof = profiles[prof_key]
        self.hmac_key: bytes = self.prof["hmac_key"].encode()

        try:
            from pyplayready.cdm import Cdm as PlayReadyCdm
            self.use_playready: bool = isinstance(ctx.obj.cdm, PlayReadyCdm)
        except ImportError:
            self.use_playready = False

        self.tokens: Optional[dict] = None
        self.license_url: Optional[str] = None

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.session.headers.update({"Origin": "https://www.peacocktv.com"})

        if credential and credential.username and credential.password:
            self.log.info("Authenticating with email/password credentials.")
            self._login(credential.username, credential.password)
        elif not cookies:
            raise EnvironmentError("Peacock requires cookies or credential.")

        self.log.info("Fetching authorization tokens.")
        self.tokens = self._get_tokens()
        self.log.info("Verifying tokens.")
        if not self._verify_tokens():
            raise EnvironmentError("Token verification failed.")

    def _login(self, email: str, password: str) -> None:
        r = self.session.post(
            url=self.config["endpoints"]["login"],
            data={"userIdentifier": email, "password": password},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-SkyOTT-Proposition": self.config["client"]["proposition"],
                "X-SkyOTT-Provider": self.config["client"]["provider"],
                "X-SkyOTT-Territory": self.config["client"]["territory"],
            },
        )
        if r.status_code not in (200, 201):
            try:
                code = (
                    r.json()
                    .get("properties", {})
                    .get("errors", {})
                    .get("categoryErrors", [{}])[0]
                    .get("code", "unknown")
                )
                raise EnvironmentError(f"Login failed: {code}")
            except (ValueError, KeyError, IndexError):
                raise EnvironmentError(f"Login failed with HTTP {r.status_code}.")

    def _sky_headers(self, extra: Optional[dict] = None) -> dict:
        h = {
            "X-SkyOTT-Device": self.prof["device"],
            "X-SkyOTT-Platform": self.prof["platform"],
            "X-SkyOTT-Proposition": self.config["client"]["proposition"],
            "X-SkyOTT-Provider": self.config["client"]["provider"],
            "X-SkyOTT-Territory": self.config["client"]["territory"],
        }
        if extra:
            h.update(extra)
        return h

    def _md5_headers(self, headers: dict) -> str:
        lines = sorted(
            f"{k.lower()}: {v}"
            for k, v in headers.items()
            if k.lower().startswith("x-skyott-")
        )
        text = "\n".join(lines) + ("\n" if lines else "")
        return hashlib.md5(text.encode()).hexdigest()

    @staticmethod
    def _md5_body(body: str | bytes) -> str:
        if isinstance(body, str):
            body = body.encode()
        return hashlib.md5(body).hexdigest()

    def _sign(self, method: str, path: str, headers: dict, body: str | bytes = b"") -> str:
        ts = str(int(time.time()))
        sdk = self.prof["client_sdk"]
        msg = (
            "\n".join([
                method.upper(),
                path,
                "",
                sdk,
                "1.0",
                self._md5_headers(headers),
                ts,
                self._md5_body(body),
            ])
            + "\n"
        )
        digest = hmac.new(self.hmac_key, msg.encode(), hashlib.sha1).digest()
        sig = base64.b64encode(digest).decode()
        return f'SkyOTT client="{sdk}",signature="{sig}",timestamp="{ts}",version="1.0"'

    def _get_tokens(self) -> dict:
        prof_key = self.config["client"].get("profile", "tv")
        cache_key = f"tokens_{self.profile_name}_{prof_key}"
        cache = self.cache.get(cache_key)

        if cache and cache.data:
            expiry = cache.data.get("tokenExpiryTime")
            if expiry:
                try:
                    if datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%S.%fZ") > datetime.utcnow():
                        self.log.debug("Using cached tokens.")
                        return cache.data
                except ValueError:
                    pass

        sky_h = self._sky_headers()

        persona_id: Optional[str] = None
        try:
            r = self.session.get(
                url=self.config["endpoints"]["personas"],
                headers={
                    **sky_h,
                    "Accept": "application/vnd.persona.v1+json",
                    "X-SkyOTT-TokenType": self.config["client"]["auth_scheme"],
                },
            )
            if r.ok:
                personas = r.json().get("personas", [])
                if personas:
                    persona_id = personas[0]["personaId"]
        except Exception as e:
            self.log.debug(f"Persona fetch skipped: {e}")

        auth_block: dict = {
            "authScheme": self.config["client"]["auth_scheme"],
            "provider": self.config["client"]["provider"],
            "providerTerritory": self.config["client"]["territory"],
            "proposition": self.config["client"]["proposition"],
        }
        if persona_id:
            auth_block["personaId"] = persona_id

        body = json.dumps(
            {
                "auth": auth_block,
                "device": {
                    "type": self.prof["device"],
                    "platform": self.prof["platform"],
                    "id": self.config["client"].get("device_id", "PC"),
                    "drmDeviceId": self.config["client"].get("drm_device_id", "UNKNOWN"),
                },
            },
            separators=(",", ":"),
        )

        r = self.session.post(
            url=self.config["endpoints"]["tokens"],
            data=body,
            headers={
                **sky_h,
                "Accept": "application/vnd.tokens.v1+json",
                "Content-Type": "application/vnd.tokens.v1+json",
                "X-Sky-Signature": self._sign("POST", "/auth/tokens", sky_h, body),
            },
        )
        r.raise_for_status()
        tokens = r.json()

        if "description" in tokens and "userToken" not in tokens:
            raise EnvironmentError(f"Token fetch failed: {tokens['description']}")

        if cache:
            cache.set(data=tokens)

        tokens["_fresh"] = True
        return tokens

    def _verify_tokens(self) -> bool:
        if self.tokens.pop("_fresh", False):
            return True
        sky_h = self._sky_headers({"X-SkyOTT-UserToken": self.tokens["userToken"]})
        try:
            r = self.session.get(
                url=self.config["endpoints"]["me"],
                headers={
                    **sky_h,
                    "Accept": "application/vnd.userinfo.v2+json",
                    "X-Sky-Signature": self._sign("GET", "/auth/users/me", sky_h),
                },
            )
            return r.ok
        except Exception:
            return False

    def get_titles(self) -> Titles_T:
        title_id = self.title
        for pattern in self.TITLE_RE:
            m = re.search(pattern, self.title)
            if m:
                title_id = m.group("id")
                break

        if "/" not in title_id:
            r = self.session.get(f"https://www.peacocktv.com/stream-tv/{title_id}")
            m = re.search(r"/watch/asset(/[^'\"?#\s]+)", r.text)
            if m:
                title_id = m.group(1)
            else:
                raise ValueError(f"Could not resolve slug: {title_id!r}")

        if not title_id.startswith("/"):
            title_id = f"/{title_id}"

        if title_id.startswith("/movies/"):
            self.movie = True

        sky_h = self._sky_headers()
        res = self.session.get(
            url=self.config["endpoints"]["node"],
            params={"slug": title_id, "represent": "(items(items))"},
            headers={
                **sky_h,
                "Accept": "*",
                "Referer": f"https://www.peacocktv.com/watch/asset{title_id}",
                "X-SkyOTT-Language": "en",
            },
        ).json()

        if self.movie:
            return Movies([
                Movie(
                    id_=title_id,
                    service=self.__class__,
                    name=res["attributes"]["title"],
                    year=res["attributes"].get("year"),
                    data=res,
                )
            ])

        episodes = [
            ep
            for season in res.get("relationships", {}).get("items", {}).get("data", [])
            for ep in season.get("relationships", {}).get("items", {}).get("data", [])
        ]

        return Series([
            Episode(
                id_=title_id,
                service=self.__class__,
                title=res["attributes"]["title"],
                season=ep["attributes"].get("seasonNumber"),
                number=ep["attributes"].get("episodeNumber"),
                name=ep["attributes"].get("title"),
                year=ep["attributes"].get("year"),
                data=ep,
            )
            for ep in episodes
        ])

    def get_tracks(self, title: Title_T) -> Tracks:
        attrs = title.data["attributes"]
        formats = attrs.get("formats", {})

        want_uhd = self.vcodec == "H265"
        if want_uhd and "UHD" in formats:
            content_id = formats["UHD"]["contentId"]
        elif "HD" in formats:
            content_id = formats["HD"]["contentId"]
        else:
            content_id = next(iter(formats.values()), {}).get("contentId", "")

        variant_id = attrs.get("providerVariantId", "")

        if self.range == "HDR10":
            colour_spaces = ["HDR10"]
        elif self.range == "DV":
            colour_spaces = ["DolbyVision"]
        else:
            colour_spaces = ["SDR"]

        primary_drm = "PLAYREADY" if self.use_playready else "WIDEVINE"

        capabilities = [
            {
                "protection": primary_drm,
                "container": "ISOBMFF",
                "transport": "DASH",
                "acodec": "AAC",
                "vcodec": self.vcodec,
            }
        ]

        sky_h = self._sky_headers({"X-SkyOTT-UserToken": self.tokens["userToken"]})
        body = json.dumps(
            {
                "device": {
                    "capabilities": capabilities,
                    "maxVideoFormat": "UHD" if want_uhd else "HD",
                    "supportedColourSpaces": colour_spaces,
                    "model": self.prof["platform"],
                    "hdcpEnabled": "true",
                },
                "client": {"thirdParties": ["FREEWHEEL", "YOSPACE"]},
                "contentId": content_id,
                "providerVariantId": variant_id,
                "parentalControlPin": "null",
                "personaParentalControlRating": 9,
            },
            separators=(",", ":"),
        )

        r = self.session.post(
            url=self.config["endpoints"]["vod"],
            data=body,
            headers={
                **sky_h,
                "Accept": "application/vnd.playvod.v1+json",
                "Content-Type": "application/vnd.playvod.v1+json",
                "X-Sky-Signature": self._sign("POST", "/video/playouts/vod", sky_h, body),
            },
        )
        manifest = r.json()

        if "errorCode" in manifest:
            raise ValueError(
                f"Playout error: {manifest.get('description', 'unknown')} [{manifest['errorCode']}]"
            )

        self.license_url = manifest["protection"]["licenceAcquisitionUrl"]

        endpoints = manifest["asset"]["endpoints"]
        dash_url = next(
            (e["url"] for e in endpoints if e.get("cdn", "").upper() == "FASTLY"),
            endpoints[0]["url"] if endpoints else None,
        )
        if not dash_url:
            raise ValueError("No DASH endpoint in playout response.")

        tracks = DASH.from_url(url=dash_url, session=self.session).to_tracks(language=Language.get("en"))

        for video in tracks.videos:
            if colour_spaces == ["HDR10"]:
                video.range = Video.Range.HDR10
            elif colour_spaces == ["DolbyVision"]:
                video.range = Video.Range.DV
            else:
                video.range = Video.Range.SDR

        for audio in tracks.audio:
            if audio.language.territory == "AD":
                audio.language.territory = None

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def _license_request(self, challenge: bytes) -> bytes:
        path = urlparse(self.license_url).path
        r = self.session.post(
            url=self.license_url,
            data=challenge,
            headers={
                "X-Sky-Signature": self._sign("POST", path, {}, challenge),
            },
        )
        r.raise_for_status()
        return r.content

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        if not self.license_url:
            return None
        return self._license_request(challenge)

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        if not self.license_url:
            return None
        return self._license_request(challenge)