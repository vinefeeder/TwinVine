from __future__ import annotations

import re
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import MozillaCookieJar
from typing import Any, List, Optional, Union
from uuid import uuid4

import click
from click import Context
from pyplayready.cdm import Cdm as PlayReadyCdm
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapter, Chapters, Tracks


class SEVEN(Service):
    """
    Service code for 7Plus streaming service (https://7plus.com.au/).

    \b
    Version: 1.0.1
    Author: stabbedbybrick
    Authorization: Cookies
    Geofence: AU (API and downloads)
    Robustness:
        Widevine:
            L3: 720p
        PlayReady:
            SL2000: 720p

    \b
    Tips:
        - Use complete title URL as input:
            SERIES: https://7plus.com.au/ncis-los-angeles
            EPISODE: https://7plus.com.au/ncis-los-angeles?episode-id=NCIL01-001
        - There's no way to distinguish between series and movies, so use `--movie` to download as movie

    \b
    Examples:
        - SERIES: envied.dl -w s01e01 7plus https://7plus.com.au/ncis-los-angeles
        - EPISODE: envied.dl 7plus https://7plus.com.au/ncis-los-angeles?episode-id=NCIL01-001
        - MOVIE: envied.dl 7plus --movie https://7plus.com.au/puss-in-boots-the-last-wish

    """

    GEOFENCE = ("au",)
    ALIASES = ("7plus", "sevenplus",)

    @staticmethod
    @click.command(name="SEVEN", short_help="https://7plus.com.au/", help=__doc__)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Download as Movie")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> SEVEN:
        return SEVEN(ctx, **kwargs)

    def __init__(self, ctx: Context, movie: bool, title: str):
        self.title = title
        self.movie = movie
        super().__init__(ctx)

        self.cdm = ctx.obj.cdm
        self.drm_system = "playready" if isinstance(self.cdm, PlayReadyCdm) else "widevine"
        self.key_system = "com.microsoft.playready" if isinstance(self.cdm, PlayReadyCdm) else "com.widevine.alpha"

        self.profile = ctx.parent.params.get("profile")
        if not self.profile:
            self.profile = "default"

        self.session.headers.update(self.config["headers"])

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if cookies is None:
            raise EnvironmentError("Service requires Cookies for Authentication.")
        self.session.cookies.update(cookies)

        api_key = next((cookie.name.replace("gig_bootstrap_", "") for cookie in cookies if "login_ver" in cookie.value), None)
        login_token = next((cookie.value for cookie in cookies if "glt_" in cookie.name), None)
        if not api_key or not login_token:
            raise ValueError("Invalid cookies. Try refreshing.")

        market = self.session.get(
            "https://market-cdn.swm.digital/v1/market/ip/",
            params={"apikey": "web"}
        ).json()

        self.market_id = market.get("_id", 4)

        cache = self.cache.get(f"tokens_{self.profile}")

        if cache and not cache.expired:
            # cached
            self.log.info(" + Using cached tokens...")
            tokens = cache.data
        elif cache and cache.expired:
            # expired, refresh
            self.log.info("+ Refreshing tokens...")
            payload = {
                "platformId": self.config["PLATFORM_ID"],
                "regSource": "7plus",
                "refreshToken": cache.data.get("refresh_token"),
            }
            r = self.session.post("https://auth2.swm.digital/connect/token", data=payload)
            if r.status_code != 200:
                raise ConnectionError(f"Failed to refresh tokens: {r.text}")
            tokens = r.json()
            cache.set(tokens, expiration=int(tokens["expires_in"]) - 60)

        else:
            # new
            self.log.info(" + Authenticating...")
            device_id = str(uuid4()) 
            payload = {
                "platformId": self.config["PLATFORM_ID"],
                "regSource": "7plus",
                "deviceId": device_id,
                "locationVerificationRequired": "false",
            }
            r = self.session.post("https://auth2.swm.digital/account/device/authorize", data=payload)
            if r.status_code != 200:
                raise ConnectionError(f"Failed to authenticate: {r.text}")
            auth = r.json()

            uri = auth.get("verification_uri_complete")
            user_code = auth.get("user_code")
            device_code = auth.get("device_code")
            if not uri or not user_code or not device_code:
                raise ValueError(f"Failed to authenticate device: {auth}")

            data = {
                "APIKey": api_key,
                "sdk": "js_next",
                "login_token": login_token,
                "authMode": "cookie",
                "pageURL": "https://7plus.com.au/connect",
                "sdkBuild": "18051",
                "format": "json",
            }

            response = self.session.post("https://login.7plus.com.au/accounts.getJWT", cookies=cookies, data=data)
            if response.status_code != 200:
                raise ConnectionError(f"Failed to fetch JWT: {response.text}")
            
            id_token = response.json().get("id_token")
            if not id_token:
                raise ValueError(f"Failed to fetch JWT: {response.text}")
            
            headers = {
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-US,en;q=0.9",
                "authorization": f"Bearer {id_token}",
                "content-type": "application/json;charset=UTF-8",
                "origin": "https://7plus.com.au",
                "referer": "https://7plus.com.au/connect",
            }

            payload = {
                "platformId": "web",
                "regSource": "7plus",
                "code": user_code,
                "attemptLocationPairing": False,
            }
            r = self.session.post("https://7plus.com.au/auth/otp", headers=headers, json=payload)
            if r.status_code != 200:
                raise ConnectionError(f"Failed to verify OTP: {r.status_code}")
            
            payload = {
                "platformId": self.config["PLATFORM_ID"],
                "regSource": "7plus",
                "deviceCode": device_code,
            }
            r = self.session.post("https://auth2.swm.digital/connect/token", data=payload)
            if r.status_code != 200:
                raise ConnectionError(f"Failed to fetch device token: {r.text}")
            tokens = r.json()

            tokens["device_id"] = device_id
            cache.set(tokens, expiration=int(tokens["expires_in"]) - 60)
        
        self.device_id = tokens.get("device_id") or str(uuid4())
        self.session.headers.update({"authorization": f"Bearer {tokens['access_token']}"})

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "searchTerm": self.title,
            "market-id": self.market_id,
            "api-version": "4.4",
            "platform-id": self.config["PLATFORM_ID"],
            "platform-version": self.config["PLATFORM_VERSION"],
        }

        r = self.session.get("https://searchapi.swm.digital/3.0/api/Search", params=params)
        r.raise_for_status()

        results = r.json()
        if isinstance(results, list):
            for result in results:
                title = result.get("image", {}).get("altTag")
                slug = result.get("contentLink", {}).get("url")

                yield SearchResult(
                    id_=f"https://7plus.com.au{slug}",
                    title=title,
                    url=f"https://7plus.com.au{slug}",
                )

    def get_titles(self) -> Movies | Series:
        if match := re.match(r"https:\/\/7plus\.com\.au\/([^?\/]+)(?:\?.*episode-id=([^&]+))?", self.title):
            slug, episode_id = match.groups()
        else:
            raise ValueError(f"Invalid title: {self.title}")
        
        params = {
            "platform-id": self.config["PLATFORM_ID"],
            "market-id": self.market_id,
            "platform-version": self.config["PLATFORM_VERSION"],
            "api-version": self.config["API_VERSION"],
        }

        r = self.session.get(f"https://component-cdn.swm.digital/content/{slug}", params=params)
        if r.status_code != 200:
            raise ConnectionError(f"Failed to fetch content: {r.text}")

        content = r.json()
        
        if episode_id:
            episodes = self._series(content, slug)
            episode = next((e for e in episodes if e.id == episode_id), None)
            return Series([episode])
        
        elif self.movie:
            movie = self._movie(content)
            return Movies([movie])
        
        else:
            episodes = self._series(content, slug)
            return Series(episodes)

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        params = {
            "appId": "7plus",
            "deviceType": self.config["PLATFORM_ID"],
            "platformType": "tv",
            "deviceId": self.device_id,
            "pc": 3181,
            "advertid": "null",
            "accountId": "5303576322001",
            "referenceId": f"ref:{title.id}",
            "deliveryId": "csai",
            "marketId": self.market_id,
            "ozid": "dc6095c7-e895-41d3-6609-79f673fc7f63",
            "sdkverification": "true",
            "cp.encryptionType": "cenc",
            "cp.drmSystems": self.drm_system,
            "cp.containerFormat": "cmaf",
            "cp.supportedCodecs": "avc",
            "cp.drmAuth": "true",
        }
        resp = self.session.get("https://videoservice.swm.digital/playback", params=params)
        if resp.status_code != 200:
            raise ConnectionError(f"Failed to fetch playback data: {resp.text}")
        data = resp.json()

        drm = data.get("media", {}).get("stream_type_drm", False)
        if drm:
            source_manifest = next((
                x["src"] for x in data["media"]["sources"] 
                if x.get("key_systems").get("com.widevine.alpha")),
                None,
            )
            title.data["license_url"] = next((
                x["key_systems"][self.key_system]["license_url"]
                for x in data["media"]["sources"]
                if x.get("key_systems").get(self.key_system)),
                None,
            )
        else:
            source_manifest = next((
                x["src"] for x in data["media"]["sources"] 
                if x.get("type") == "application/dash+xml"),
                None,
            )
        if not source_manifest:
            raise ValueError("Failed to get manifest")
        
        title.data["cue_points"] = data.get("media", {}).get("cue_points")
        
        tracks = DASH.from_url(source_manifest, self.session).to_tracks(title.language)

        for track in tracks.audio:
            role = track.data["dash"]["representation"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        if not (cue_points := title.data.get("cue_points")):
            return Chapters()
        
        cue_points = sorted(cue_points, key=lambda x: x["time"])

        chapters = []
        for cue_point in cue_points:
            if cue_point.get("time", 0) > 0:
                name = "End Credits" if cue_point.get("name", "").lower() == "credits" else None
                chapters.append(Chapter(name=name, timestamp=cue_point["time"] * 1000))

        return Chapters(chapters)

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> Optional[Union[bytes, str]]:
        if license_url := title.data.get("license_url"):
            r = self.session.post(url=license_url, data=challenge)
            if r.status_code != 200:
                raise ConnectionError(r.text)
            return r.content

        return None
    
    def get_playready_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> Optional[Union[bytes, str]]:
        if license_url := title.data.get("license_url"):
            r = self.session.post(url=license_url, data=challenge)
            if r.status_code != 200:
                raise ConnectionError(r.text)
            return r.content

        return None

    # Service specific functions

    def _movie(self, content: dict) -> Movie:
        title = content.get("title")
        metadata = content.get("items", [{}])[0].get("videoMetadata", {})
        if not metadata:
            raise ValueError("Failed to find metadata for this movie")

        return Movie(
            id_=metadata.get("videoBref"),
            service=self.__class__,
            name=title,
            year=metadata.get("productionYear"),
            language="en",
            data=content,
        )

    def _get_season_data(self, season_id: str, slug: str) -> List[Episode]:
        params = {
            "component-id": season_id,
            "platform-id": self.config.get("PLATFORM_ID"),
            "market-id": self.market_id,
            "platform-version": self.config.get("PLATFORM_VERSION"),
            "api-version": self.config.get("API_VERSION"),
            "signedUp": "True",
        }

        try:
            r = self.session.get(f"https://component.swm.digital/component/{slug}", params=params)
            r.raise_for_status()
            comp = r.json()
        except ConnectionError as e:
            self.log.error(f"Error fetching season {season_id}: {e}")
            return []
        except Exception as e:
            self.log.error(f"An unexpected error occurred for season {season_id}: {e}")
            return []

        episodes = []
        for episode in comp.get("items", []):
            info_panel = episode.get("infoPanelData", {})
            player_data = episode.get("playerData", {})
            card_data = episode.get("cardData", {})
            catalogue_number = episode.get("catalogueNumber", "")

            title = info_panel.get("title")
            episode_name = card_data.get("image", {}).get("altTag")
            card_name = card_data.get("title", "").lstrip("0123456789. ").split(" - ")[-1].strip()
            
            season, number, name = 0, 0, card_name
            if match := re.search(r"(?:Season|Year)\s*(\d+)\s*E(?:pisode)?\s*(\d+)", episode_name, re.IGNORECASE):
                season = int(match.group(1))
                number = int(match.group(2))

            if not season and not number:
                if match := re.compile(r"\w+(\d+)-(\d+)").search(catalogue_number):
                    season = int(match.group(1))
                    number = int(match.group(2))

            episodes.append(
                Episode(
                    id_=player_data.get("episodePlayerId"),
                    service=self.__class__,
                    title=title,
                    year=card_data.get("productionYear"),
                    season=season,
                    number=number,
                    name=name,
                    language="en",
                    data=episode,
                )
            )
        return episodes

    def _series(self, content: dict, slug: str) -> List[Episode]:
        items = next((x for x in content.get("items", []) if x.get("type") == "shelfContainer"), {})
        episodes_shelf = next((x for x in items.get("items", []) if x.get("title") == "Episodes"), {})
        seasons_container = next((x for x in episodes_shelf.get("items", []) if x.get("title") in ("Season", "Year", "Bulletin")), {})
        
        season_ids = [
            item.get("items", [{}])[0].get("id")
            for item in seasons_container.get("items", [])
            if item.get("items") and item.get("items")[0].get("id")
        ]

        if not season_ids:
            return []

        all_episodes = []
        with ThreadPoolExecutor(max_workers=len(season_ids)) as executor:
            future_to_season = {
                executor.submit(self._get_season_data, season_id, slug): season_id for season_id in season_ids
            }
            for future in future_to_season:
                try:
                    episodes_of_season = future.result()
                    all_episodes.extend(episodes_of_season)
                except Exception as exc:
                    season_id = future_to_season[future]
                    self.log.error(f"{season_id} generated an exception: {exc}")

        return all_episodes
