from __future__ import annotations

import base64
import hashlib
import json
import sys
import re
from collections.abc import Generator
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union

import click
from click import Context
from Crypto.Util.Padding import unpad
from Cryptodome.Cipher import AES
from pywidevine.cdm import Cdm as WidevineCdm

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Subtitle, Tracks


class ALL4(Service):
    """
    Service code for Channel 4's All4 streaming service (https://channel4.com).

    \b
    Version: 1.0.0
    Author: stabbedbybrick
    Authorization: Credentials
    Robustness:
      L3: 1080p, AAC2.0

    \b
    Tips:
        - Use complete title URL or slug as input:
            https://www.channel4.com/programmes/taskmaster OR taskmaster
        - Use on demand URL for directly downloading episodes:
            https://www.channel4.com/programmes/taskmaster/on-demand/75588-002
        - Both android and web/pc endpoints are checked for quality profiles.
            If android is missing 1080p, it automatically falls back to web.
    """

    GEOFENCE = ("gb", "ie")
    TITLE_RE = r"^(?:https?://(?:www\.)?channel4\.com/programmes/)?(?P<id>[a-z0-9-]+)(?:/on-demand/(?P<vid>[0-9-]+))?"

    @staticmethod
    @click.command(name="ALL4", short_help="https://channel4.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> ALL4:
        return ALL4(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.authorization: str
        self.asset_id: int
        self.license_token: str
        self.manifest: str

        self.session.headers.update(
            {
                "X-C4-Platform-Name": self.config["device"]["platform_name"],
                "X-C4-Device-Type": self.config["device"]["device_type"],
                "X-C4-Device-Name": self.config["device"]["device_name"],
                "X-C4-App-Version": self.config["device"]["app_version"],
                "X-C4-Optimizely-Datafile": self.config["device"]["optimizely_datafile"],
            }
        )

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        cache = self.cache.get(f"tokens_{credential.sha1}")

        if cache and not cache.expired:
            # cached
            self.log.info(" + Using cached Tokens...")
            tokens = cache.data
        elif cache and cache.expired:
            # expired, refresh
            self.log.info("Refreshing cached Tokens")
            r = self.session.post(
                self.config["endpoints"]["login"],
                headers={"authorization": f"Basic {self.config['android']['auth']}"},
                data={
                    "grant_type": "refresh_token",
                    "username": credential.username,
                    "password": credential.password,
                    "refresh_token": cache.data["refreshToken"],
                },
            )
            try:
                res = r.json()
            except json.JSONDecodeError:
                raise ValueError(f"Failed to refresh tokens: {r.text}")

            if "error" in res:
                self.log.error(f"Failed to refresh tokens: {res['errorMessage']}")
                sys.exit(1)

            tokens = res
            self.log.info(" + Refreshed")
        else:
            # new
            headers = {"authorization": f"Basic {self.config['android']['auth']}"}
            data = {
                "grant_type": "password",
                "username": credential.username,
                "password": credential.password,
            }
            r = self.session.post(self.config["endpoints"]["login"], headers=headers, data=data)
            try:
                res = r.json()
            except json.JSONDecodeError:
                raise ValueError(f"Failed to log in: {r.text}")

            if "error" in res:
                self.log.error(f"Failed to log in: {res['errorMessage']}")
                sys.exit(1)

            tokens = res
            self.log.info(" + Acquired tokens...")

        cache.set(tokens, expiration=tokens["expiresIn"])

        self.authorization = f"Bearer {tokens['accessToken']}"

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "expand": "default",
            "q": self.title,
            "limit": "100",
            "offset": "0",
        }

        r = self.session.get(self.config["endpoints"]["search"], params=params)
        r.raise_for_status()

        results = r.json()
        if isinstance(results["results"], list):
            for result in results["results"]:
                yield SearchResult(
                    id_=result["brand"].get("websafeTitle"),
                    title=result["brand"].get("title"),
                    description=result["brand"].get("description"),
                    label=result.get("label"),
                    url=result["brand"].get("href"),
                )

    def get_titles(self) -> Union[Movies, Series]:
        title, on_demand = (re.match(self.TITLE_RE, self.title).group(i) for i in ("id", "vid"))

        r = self.session.get(
            self.config["endpoints"]["title"].format(title=title),
            params={"client": "android-mod", "deviceGroup": "mobile", "include": "extended-restart"},
            headers={"Authorization": self.authorization},
        )
        if not r.ok:
            self.log.error(r.text)
            sys.exit(1)

        data = r.json()

        if on_demand is not None:
            episodes = [
                Episode(
                    id_=episode["programmeId"],
                    service=self.__class__,
                    title=data["brand"]["title"],
                    season=episode["seriesNumber"],
                    number=episode["episodeNumber"],
                    name=episode["originalTitle"],
                    language="en",
                    data=episode["assetInfo"].get("streaming") or episode["assetInfo"].get("download"),
                )
                for episode in data["brand"]["episodes"]
                if episode.get("assetInfo") and episode["programmeId"] == on_demand
            ]
            if not episodes:
                # Parse HTML of episode page to find title
                data = self.get_html(self.title)
                episodes = [
                    Episode(
                        id_=data["selectedEpisode"]["programmeId"],
                        service=self.__class__,
                        title=data["brand"]["title"],
                        season=data["selectedEpisode"]["seriesNumber"] or 0,
                        number=data["selectedEpisode"]["episodeNumber"] or 0,
                        name=data["selectedEpisode"]["originalTitle"],
                        language="en",
                        data=data["selectedEpisode"],
                    )
                ]

            return Series(episodes)

        elif data["brand"]["programmeType"] == "FM":
            return Movies(
                [
                    Movie(
                        id_=movie["programmeId"],
                        service=self.__class__,
                        name=data["brand"]["title"],
                        year=int(data["brand"]["summary"].split(" ")[0].strip().strip("()")),
                        language="en",
                        data=movie["assetInfo"].get("streaming") or movie["assetInfo"].get("download"),
                    )
                    for movie in data["brand"]["episodes"]
                ]
            )
        else:
            return Series(
                [
                    Episode(
                        id_=episode["programmeId"],
                        service=self.__class__,
                        title=data["brand"]["title"],
                        season=episode["seriesNumber"],
                        number=episode["episodeNumber"],
                        name=episode["originalTitle"],
                        language="en",
                        data=episode["assetInfo"].get("streaming") or episode["assetInfo"].get("download"),
                    )
                    for episode in data["brand"]["episodes"]
                    if episode.get("assetInfo")
                ]
            )

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        android_assets: tuple = self.android_playlist(title.id)
        web_assets: tuple = self.web_playlist(title.id)
        self.manifest, self.license_token, subtitle, data = self.sort_assets(title, android_assets, web_assets)
        self.asset_id = int(title.data["assetId"])

        tracks = DASH.from_url(self.manifest, self.session).to_tracks(title.language)
        tracks.videos[0].data = data

        # manifest subtitles are sometimes empty even if they exist
        # so we clear them and add the subtitles manually
        tracks.subtitles.clear()
        if subtitle is not None:
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(subtitle.encode()).hexdigest()[0:6],
                    url=subtitle,
                    codec=Subtitle.Codec.from_mime(subtitle[-3:]),
                    language=title.language,
                    is_original_lang=True,
                    forced=False,
                    sdh=False,
                )
            )
        else:
            self.log.warning("- Subtitles are either missing or empty")

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> list[Chapter]:
        track = title.tracks.videos[0]

        chapters = [
            Chapter(
                name=f"Chapter {i + 1:02}",
                timestamp=datetime.fromtimestamp((ms / 1000), tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3],
            )
            for i, ms in enumerate(x["breakOffset"] for x in track.data["adverts"]["breaks"])
        ]

        if track.data.get("endCredits", {}).get("squeezeIn"):
            chapters.append(
                Chapter(
                    name="Credits",
                    timestamp=datetime.fromtimestamp(
                        (track.data["endCredits"]["squeezeIn"] / 1000), tz=timezone.utc
                    ).strftime("%H:%M:%S.%f")[:-3],
                )
            )

        return chapters

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return WidevineCdm.common_privacy_cert

    def get_widevine_license(self, challenge: bytes, **_: Any) -> str:
        payload = {
            "message": base64.b64encode(challenge).decode("utf8"),
            "token": self.license_token,
            "request_id": self.asset_id,
            "video": {"type": "ondemand", "url": self.manifest},
        }

        r = self.session.post(self.config["endpoints"]["license"], json=payload)
        if not r.ok:
            raise ConnectionError(f"License request failed: {r.json()['status']['type']}")

        return r.json()["license"]

    # Service specific functions

    def sort_assets(self, title: Union[Movie, Episode], android_assets: tuple, web_assets: tuple) -> tuple:
        android_heights = None
        web_heights = None

        if android_assets is not None:
            try:
                a_manifest, a_token, a_subtitle, data = android_assets
                android_tracks = DASH.from_url(a_manifest, self.session).to_tracks(title.language)
                android_heights = sorted([int(track.height) for track in android_tracks.videos], reverse=True)
            except Exception:
                android_heights = None

        if web_assets is not None:
            try:
                b_manifest, b_token, b_subtitle, data = web_assets
                session = self.session
                session.headers.update(self.config["headers"])
                web_tracks = DASH.from_url(b_manifest, session).to_tracks(title.language)
                web_heights = sorted([int(track.height) for track in web_tracks.videos], reverse=True)
            except Exception:
                web_heights = None

        if not android_heights and not web_heights:
            self.log.error("Failed to request manifest data. If you're behind a VPN/proxy, you might be blocked")
            sys.exit(1)

        if not android_heights or android_heights[0] < 1080:
            lic_token = self.decrypt_token(b_token, client="WEB")
            return b_manifest, lic_token, b_subtitle, data
        else:
            lic_token = self.decrypt_token(a_token, client="ANDROID")
            return a_manifest, lic_token, a_subtitle, data

    def android_playlist(self, video_id: str) -> tuple:
        url = self.config["android"]["vod"].format(video_id=video_id)
        headers = {"authorization": self.authorization}

        r = self.session.get(url=url, headers=headers)
        if not r.ok:
            self.log.warning("Request for Android endpoint returned %s", r)
            return None

        data = json.loads(r.content)
        manifest = data["videoProfiles"][0]["streams"][0]["uri"]
        token = data["videoProfiles"][0]["streams"][0]["token"]
        subtitle = next(
            (x["url"] for x in data["subtitlesAssets"] if x["url"].endswith(".vtt")),
            None,
        )

        return manifest, token, subtitle, data

    def web_playlist(self, video_id: str) -> tuple:
        url = self.config["web"]["vod"].format(programmeId=video_id)
        r = self.session.get(url, headers=self.config["headers"])
        if not r.ok:
            self.log.warning("Request for WEB endpoint returned %s", r)
            return None

        data = json.loads(r.content)

        for item in data["videoProfiles"]:
            if item["name"] == "dashwv-dyn-stream-1":
                token = item["streams"][0]["token"]
                manifest = item["streams"][0]["uri"]

        subtitle = next(
            (x["url"] for x in data["subtitlesAssets"] if x["url"].endswith(".vtt")),
            None,
        )

        return manifest, token, subtitle, data

    def decrypt_token(self, token: str, client: str) -> tuple:
        if client == "ANDROID":
            key = self.config["android"]["key"]
            iv = self.config["android"]["iv"]

        if client == "WEB":
            key = self.config["web"]["key"]
            iv = self.config["web"]["iv"]

        if isinstance(token, str):
            token = base64.b64decode(token)
            cipher = AES.new(
                key=base64.b64decode(key),
                iv=base64.b64decode(iv),
                mode=AES.MODE_CBC,
            )
            data = unpad(cipher.decrypt(token), AES.block_size)
            dec_token = data.decode().split("|")[1]
            return dec_token.strip()

    def get_html(self, url: str) -> dict:
        r = self.session.get(url=url, headers=self.config["headers"])
        r.raise_for_status()

        init_data = re.search(
            "<script>window.__PARAMS__ = (.*)</script>",
            "".join(r.content.decode().replace("\u200c", "").replace("\r\n", "").replace("undefined", "null")),
        )
        try:
            data = json.loads(init_data.group(1))
            return data["initialData"]
        except Exception:
            self.log.error(f"Failed to get episode for {url}")
            sys.exit(1)
