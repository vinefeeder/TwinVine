import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Optional, Union

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
    Service code for NBC's Peacock streaming service (https://peacocktv.com).
    Version: 1.0.0

    Authorization: Cookies
    Security: UHD@-- FHD@SL|L3

    Tips: - The library of contents can be viewed without logging in at https://www.peacocktv.com/stream/tv
            See the footer for links to movies, news, etc. A US IP is required to view.
    """

    ALIASES = ("PCOK", "peacock")
    GEOFENCE = ("US",)
    TITLE_RE = [
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>movies/[a-z0-9/./-]+/[a-f0-9-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>tv/[a-z0-9/./-]+/[a-f0-9-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>tv/[a-z0-9-/.]+/\d+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>news/[a-z0-9/./-]+/[a-f0-9-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>news/[a-z0-9-/.]+/\d+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>-/[a-z0-9-/.]+/\d+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/stream-tv/)?(?P<id>[a-z0-9-/.]+)",
    ]

    @staticmethod
    @click.command(name="PCOK", short_help="https://peacocktv.com")
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return PCOK(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        super().__init__(ctx)

        self.title = title
        self.movie = movie
        self.cdm = ctx.obj.cdm

        range_param = ctx.parent.params.get("range_")
        self.range = range_param[0].name if range_param else "SDR"

        vcodec_param = ctx.parent.params.get("vcodec")
        self.vcodec = vcodec_param if vcodec_param else "H264"

        if self.config is None:
            raise Exception("Config is missing!")

        profile_name = ctx.parent.params.get("profile")
        if profile_name is None:
            profile_name = "default"
        self.profile = profile_name

        self.hmac_key = None
        self.tokens = None
        self.license_api = None
        self.license_bt = None

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")

        self.session.headers.update({"Origin": "https://www.peacocktv.com"})
        self.log.info("Getting Peacock Client configuration")

        if self.config["client"]["platform"] != "PC":
            self.service_config = self.session.get(
                url=self.config["endpoints"]["config"].format(
                    territory=self.config["client"]["territory"],
                    provider=self.config["client"]["provider"],
                    proposition=self.config["client"]["proposition"],
                    device=self.config["client"]["platform"],
                    version=self.config["client"]["config_version"],
                )
            ).json()

        self.hmac_key = bytes(self.config["security"]["signature_hmac_key_v4"], "utf-8")
        self.log.info("Getting Authorization Tokens")
        self.tokens = self.get_tokens()
        self.log.info("Verifying Authorization Tokens")
        if not self.verify_tokens():
            raise EnvironmentError("Failed! Cookies might be outdated.")

    def get_titles(self) -> Titles_T:
        # Parse title from various URL formats
        import re

        title_id = self.title
        for pattern in self.TITLE_RE:
            match = re.search(pattern, self.title)
            if match:
                title_id = match.group("id")
                break

        # Handle stream-tv redirects
        if "/" not in title_id:
            r = self.session.get(self.config["endpoints"]["stream_tv"].format(title_id=title_id))
            match = re.search(r"/watch/asset(/[^']+)", r.text)
            if match:
                title_id = match.group(1)
            else:
                raise ValueError("Title ID not found or invalid")

        if not title_id.startswith("/"):
            title_id = f"/{title_id}"

        if title_id.startswith("/movies/"):
            self.movie = True

        res = self.session.get(
            url=self.config["endpoints"]["node"],
            params={
                "slug": title_id,
                "represent": "(items(items))"
            },
            headers={
                "Accept": "*",
                "Referer": f"https://www.peacocktv.com/watch/asset{title_id}",
                "X-SkyOTT-Device": self.config["client"]["device"],
                "X-SkyOTT-Platform": self.config["client"]["platform"],
                "X-SkyOTT-Proposition": self.config["client"]["proposition"],
                "X-SkyOTT-Provider": self.config["client"]["provider"],
                "X-SkyOTT-Territory": self.config["client"]["territory"],
                "X-SkyOTT-Language": "en"
            }
        ).json()

        if self.movie:
            return Movies([
                Movie(
                    id_=title_id,
                    service=self.__class__,
                    name=res["attributes"]["title"],
                    year=res["attributes"]["year"],
                    data=res,
                )
            ])
        else:
            episodes = []
            for season in res["relationships"]["items"]["data"]:
                for episode in season["relationships"]["items"]["data"]:
                    episodes.append(episode)

            episode_titles = []
            for x in episodes:
                episode_titles.append(
                    Episode(
                        id_=title_id,
                        service=self.__class__,
                        title=res["attributes"]["title"],
                        season=x["attributes"].get("seasonNumber"),
                        number=x["attributes"].get("episodeNumber"),
                        name=x["attributes"].get("title"),
                        year=x["attributes"].get("year"),
                        data=x
                    )
                )
            return Series(episode_titles)

    def get_tracks(self, title: Title_T) -> Tracks:
        supported_colour_spaces = ["SDR"]

        if self.range == "HDR10":
            self.log.info("Switched dynamic range to HDR10")
            supported_colour_spaces = ["HDR10"]
        elif self.range == "DV":
            self.log.info("Switched dynamic range to DV")
            supported_colour_spaces = ["DolbyVision"]

        content_id = title.data["attributes"]["formats"]["HD"]["contentId"]
        variant_id = title.data["attributes"]["providerVariantId"]

        sky_headers = {
            "X-SkyOTT-Agent": ".".join([
                self.config["client"]["proposition"].lower(),
                self.config["client"]["device"].lower(),
                self.config["client"]["platform"].lower()
            ]),
            "X-SkyOTT-PinOverride": "false",
            "X-SkyOTT-Provider": self.config["client"]["provider"],
            "X-SkyOTT-Territory": self.config["client"]["territory"],
            "X-SkyOTT-UserToken": self.tokens["userToken"]
        }

        body = json.dumps({
            "device": {
                "capabilities": [
                    {
                        "protection": "PLAYREADY",
                        "container": "ISOBMFF",
                        "transport": "DASH",
                        "acodec": "AAC",
                        "vcodec": "H265" if self.vcodec == "H265" else "H264",
                    },
                    {
                        "protection": "WIDEVINE",
                        "container": "ISOBMFF",
                        "transport": "DASH",
                        "acodec": "AAC",
                        "vcodec": "H265" if self.vcodec == "H265" else "H264",
                    }
                ],
                "maxVideoFormat": "UHD" if self.vcodec == "H265" else "HD",
                "supportedColourSpaces": supported_colour_spaces,
                "model": self.config["client"]["platform"],
                "hdcpEnabled": "true"
            },
            "client": {
                "thirdParties": ["FREEWHEEL", "YOSPACE"]
            },
            "contentId": content_id,
            "providerVariantId": variant_id,
            "parentalControlPin": "null"
        }, separators=(",", ":"))

        manifest = self.session.post(
            url=self.config["endpoints"]["vod"],
            data=body,
            headers=dict(**sky_headers, **{
                "Accept": "application/vnd.playvod.v1+json",
                "Content-Type": "application/vnd.playvod.v1+json",
                "X-Sky-Signature": self.create_signature_header(
                    method="POST",
                    path="/video/playouts/vod",
                    sky_headers=sky_headers,
                    body=body,
                    timestamp=int(time.time())
                )
            })
        ).json()

        if "errorCode" in manifest:
            raise ValueError(f"An error occurred: {manifest['description']} [{manifest['errorCode']}]")

        self.license_api = manifest["protection"]["licenceAcquisitionUrl"]
        self.license_bt = manifest["protection"]["licenceToken"]

        tracks = DASH.from_url(
            url=manifest["asset"]["endpoints"][0]["url"],
            session=self.session
        ).to_tracks(language=Language.get("en"))

        # Set HDR attributes
        for video in tracks.videos:
            if supported_colour_spaces == ["HDR10"]:
                video.range = Video.Range.HDR10
            elif supported_colour_spaces == ["DolbyVision"]:
                video.range = Video.Range.DV
            else:
                video.range = Video.Range.SDR

        # Fix audio description language
        for track in tracks.audio:
            if track.language.territory == "AD":
                track.language.territory = None

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        """Get chapters for the title. Peacock doesn't typically provide chapter data."""
        return Chapters([])

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        """Retrieve a PlayReady license for a given track."""
        if not self.license_api:
            return None

        response = self.session.post(
            url=self.license_api,
            headers={
                "Accept": "*",
                "X-Sky-Signature": self.create_signature_header(
                    method="POST",
                    path="/" + self.license_api.split("://", 2)[1].split("/", 1)[1],
                    sky_headers={},
                    body="",
                    timestamp=int(time.time())
                )
            },
            data=challenge
        )
        response.raise_for_status()
        return response.content

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        """Retrieve a Widevine license for a given track."""
        if not self.license_api:
            return None
            
        response = self.session.post(
            url=self.license_api,
            headers={
                "Accept": "*",
                "X-Sky-Signature": self.create_signature_header(
                    method="POST",
                    path="/" + self.license_api.split("://", 1)[1].split("/", 1)[1],
                    sky_headers={},
                    body="",
                    timestamp=int(time.time())
                )
            },
            data=challenge
        )
        response.raise_for_status()
        return response.content

    @staticmethod
    def calculate_sky_header_md5(headers):
        if len(headers.items()) > 0:
            headers_str = "\n".join(f"{x[0].lower()}: {x[1]}" for x in headers.items()) + "\n"
        else:
            headers_str = "{}"
        return str(hashlib.md5(headers_str.encode()).hexdigest())

    @staticmethod
    def calculate_body_md5(body):
        return str(hashlib.md5(body.encode()).hexdigest())

    def calculate_signature(self, msg):
        digest = hmac.new(self.hmac_key, bytes(msg, "utf-8"), hashlib.sha1).digest()
        return str(base64.b64encode(digest), "utf-8")

    def create_signature_header(self, method, path, sky_headers, body, timestamp):
        data = "\n".join([
            method.upper(),
            path,
            "",
            self.config["client"]["client_sdk"],
            "1.0",
            self.calculate_sky_header_md5(sky_headers),
            str(timestamp),
            self.calculate_body_md5(body)
        ]) + "\n"

        signature_hmac = self.calculate_signature(data)

        return self.config["security"]["signature_format"].format(
            client=self.config["client"]["client_sdk"],
            signature=signature_hmac,
            timestamp=timestamp
        )

    def get_tokens(self):
        # Try to get cached tokens
        cache = self.cache.get(f"tokens_{self.profile}_{self.config['client']['id']}")

        if cache and cache.data.get("tokenExpiryTime"):
            tokens_expiration = cache.data.get("tokenExpiryTime")
            if datetime.strptime(tokens_expiration, "%Y-%m-%dT%H:%M:%S.%fZ") > datetime.now():
                return cache.data

        # Get all SkyOTT headers
        sky_headers = {
            "X-SkyOTT-Agent": ".".join([
                self.config["client"]["proposition"],
                self.config["client"]["device"],
                self.config["client"]["platform"]
            ]).lower(),
            "X-SkyOTT-Device": self.config["client"]["device"],
            "X-SkyOTT-Platform": self.config["client"]["platform"],
            "X-SkyOTT-Proposition": self.config["client"]["proposition"],
            "X-SkyOTT-Provider": self.config["client"]["provider"],
            "X-SkyOTT-Territory": self.config["client"]["territory"]
        }

        try:
            # Call personas endpoint to get the accounts personaId
            personas = self.session.get(
                url=self.config["endpoints"]["personas"],
                headers=dict(**sky_headers, **{
                    "Accept": "application/vnd.persona.v1+json",
                    "Content-Type": "application/vnd.persona.v1+json",
                    "X-SkyOTT-TokenType": self.config["client"]["auth_scheme"]
                })
            ).json()
        except Exception as e:
            raise EnvironmentError(f"Unable to get persona ID: {e}")

        persona = personas["personas"][0]["personaId"]

        # Craft the body data
        body = json.dumps({
            "auth": {
                "authScheme": self.config["client"]["auth_scheme"],
                "authIssuer": self.config["client"]["auth_issuer"],
                "provider": self.config["client"]["provider"],
                "providerTerritory": self.config["client"]["territory"],
                "proposition": self.config["client"]["proposition"],
                "personaId": persona
            },
            "device": {
                "type": self.config["client"]["device"],
                "platform": self.config["client"]["platform"],
                "id": self.config["client"]["id"],
                "drmDeviceId": self.config["client"]["drm_device_id"]
            }
        }, separators=(",", ":"))

        # Get the tokens
        tokens = self.session.post(
            url=self.config["endpoints"]["tokens"],
            headers=dict(**sky_headers, **{
                "Accept": "application/vnd.tokens.v1+json",
                "Content-Type": "application/vnd.tokens.v1+json",
                "X-Sky-Signature": self.create_signature_header(
                    method="POST",
                    path="/auth/tokens",
                    sky_headers=sky_headers,
                    body=body,
                    timestamp=int(time.time())
                )
            }),
            data=body
        ).json()

        # Cache the tokens
        if not cache:
            cache = self.cache.get(f"tokens_{self.profile}_{self.config['client']['id']}")
        cache.set(data=tokens)

        return tokens

    def verify_tokens(self):
        """Verify the tokens by calling the /auth/users/me endpoint"""
        sky_headers = {
            "X-SkyOTT-Device": self.config["client"]["device"],
            "X-SkyOTT-Platform": self.config["client"]["platform"],
            "X-SkyOTT-Proposition": self.config["client"]["proposition"],
            "X-SkyOTT-Provider": self.config["client"]["provider"],
            "X-SkyOTT-Territory": self.config["client"]["territory"],
            "X-SkyOTT-UserToken": self.tokens["userToken"]
        }

        try:
            self.session.get(
                url=self.config["endpoints"]["me"],
                headers=dict(**sky_headers, **{
                    "Accept": "application/vnd.userinfo.v2+json",
                    "Content-Type": "application/vnd.userinfo.v2+json",
                    "X-Sky-Signature": self.create_signature_header(
                        method="GET",
                        path="/auth/users/me",
                        sky_headers=sky_headers,
                        body="",
                        timestamp=int(time.time())
                    )
                })
            )
            return True
        except Exception:
            return False
