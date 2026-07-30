import base64
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from typing import Any

import click
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.session import session
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Audio, Chapter, Chapters, Subtitle, Tracks
from envied.core.cdm.detect import is_playready_cdm


class CRAV(Service):
    """
    Service code for Bell Media's Crave streaming service (https://crave.ca).

    \b
    Version: 1.0.2
    Author: stabbedbybrick
    Authorization: Credentials
    Geofence: CA (API and downloads)
    Robustness:
        Widevine:
            L1: 1080p, 2160p
            L3: 720p
        PlayReady:
            SL150: 2160p

    \b
    Tips:
        - Input examples:
            SERIES: https://www.crave.ca/en/series/it-welcome-to-derry-58324
            EPISODE: https://www.crave.ca/en/play/it-welcome-to-derry/the-pilot-s1e1-3208281
            MOVIE: https://www.crave.ca/en/movie/superman-2025-58844

    \b
    Notes:
        - Subtitles are not always consistent, so both external (VTT) and internal (WVTT) subtitles are added.
            If you experience incomplete subtitles, try using a newer version of envied.(v5.0.0+).
        - Authentication will look for master profile first, then first profile with adult maturity scope.
        - Account pins are currently not supported.
    """

    ALIASES = ("crave",)
    GEOFENCE = ("ca",)
    TITLE_RE = r"^(?:https?://(?:www\.)?crave\.ca(?:/[a-z]{2})?/(?P<type>movie|series|play)/(?:[a-z0-9-]+/)?)?([a-z0-9-]+?)(?:-(?P<id>\d+))?$"

    @staticmethod
    @click.command(name="CRAV", short_help="https://crave.ca")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: click.Context, **kwargs: Any) -> "CRAV":
        return CRAV(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.title = title

        self.cdm = ctx.obj.cdm
        self.drm_system = "playready" if is_playready_cdm(self.cdm) else "widevine"

        self.user_profile = ctx.parent.params.get("profile")
        if not self.user_profile:
            self.user_profile = "default"

    def get_session(self) -> session:
        return session("OkHttp4_12", status_forcelist=[429, 502, 503, 504])

    def authenticate(self, cookies: CookieJar | None = None, credential: Credential | None = None) -> None:
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")
        
        self.session.headers.update(self.config["headers"])
        
        cache = self.cache.get(f"tokens_{self.user_profile}_{credential.sha1}")
        
        if cache and not cache.expired:
            # cached
            self.log.info(" + Using cached tokens...")
            profile = cache.data
        elif cache and cache.expired:
            # expired
            self.log.info(" + Authenticating...")
            profile = cache.data
            r = self.session.post(
                self.config["endpoints"]["login"],
                headers={"authorization": f"Basic {self.config['auth']['android']}"},
                data={
                    "grant_type": "password",
                    "username": credential.username,
                    "password": credential.password,
                },
            )
            if not r.ok:
                raise ValueError(f"Failed to acquire tokens: {r.text}")

            tokens = r.json()
            profile["access_token"] = tokens["access_token"]
            profile["refresh_token"] = tokens["refresh_token"]
            cache.set(profile, expiration=tokens["expires_in"] - 30)

        else:
            # new
            self.log.info(" + Authenticating...")
            r = self.session.post(
                self.config["endpoints"]["login"],
                headers={"authorization": f"Basic {self.config['auth']['android']}"},
                data={
                    "grant_type": "password",
                    "username": credential.username,
                    "password": credential.password,
                },
            )
            if not r.ok:
                raise ValueError(f"Failed to acquire tokens: {r.text}")

            tokens = r.json()

            account = self._get_account(tokens["access_token"])
            if not account.get("status", "").lower() == "active":
                raise ValueError("Account is not active. Please register first.")
            
            account_id = account.get("id")
            profile = self._get_profile(account_id, tokens["access_token"])

            profile_id = next((p.get("id") for p in profile if p.get("master") or p.get("maturity") == "ADULT"), None)

            profile = {
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "account_id": account_id,
                "profile_id": profile_id,
                "profile_pin": "",
            }

            cache.set(profile, expiration=tokens["expires_in"] - 30)

        # Always refresh profile tokens
        self.log.info(" + Refreshing profile tokens...")
        r = self.session.post(
            self.config["endpoints"]["login"],
            headers={"authorization": f"Basic {self.config['auth']['android']}"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": profile["refresh_token"],
                "profile_id": profile["profile_id"],
                "profile_pin": "",
            },
        )
        if not r.ok:
            raise ValueError(f"Failed to refresh tokens: {r.text}")

        tokens = r.json()
    
        data = {
            "platform": "platform_androidtv",
            "accessToken": f"{tokens['access_token']}"
        }

        self.graphql_token = base64.b64encode(json.dumps(data).encode()).decode()
        self.jwt = tokens["access_token"]
        self.session.headers.update({"authorization": f"Bearer {self.jwt}"})

    def get_titles(self):
        if not (match := re.match(self.TITLE_RE, self.title)):
            raise ValueError(f"Unable to parse title ID {self.title!r}")

        entity_type, content_id = (match.group(i) for i in ("type", "id"))

        if entity_type == "series":
            episodes = self._get_series(content_id)
            return Series(episodes)
        
        elif entity_type == "movie":
            movie = self._get_movie(content_id)
            return Movies(movie)
        
        elif entity_type == "play":
            episode = self._get_episode(content_id)
            return Series(episode)

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        r = self.session.get(
            url=self.config["endpoints"]["contents"].format(title.id),
            headers={
                "X-Client-Platform": "platform_jasper_androidtv",
                "X-Playback-Language": title.language.language,
            },
        )
        if not r.ok:
            raise ValueError(f"Failed to get content packages: {r.text}")
        data = r.json()

        title.data["chapters"] = data.get("contentPackage", {}).get("breaks")

        if not (package_id := data.get("contentPackage", {}).get("id")):
            raise ValueError(f"Failed to get package ID: {data}")
        if not (destination_id := data.get("contentPackage", {}).get("destinationId")):
            raise ValueError(f"Failed to get destination ID: {data}")
        
        title.data["package_id"] = package_id
        title.data["destination_id"] = destination_id
            
        params = {
            "format": "mpd",
            "filter": "ff",
            "uhd": "true",
            "hd": "true",
            "mcv": "true",
            "mca": "true",
            "mta": "true",
            "stt": "true",
        }
        
        r = self.session.get(
            url=self.config["endpoints"]["destination"].format(title.id, package_id, destination_id),
            params=params,
        )
        if not r.ok:
            raise ValueError(f"Failed to get content packages: {r.text}")
        data = r.json()

        manifest, subtitles = self._get_assets(data)
        
        tracks = DASH.from_url(manifest, self.session).to_tracks(title.language)

        if subtitles is not None:
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(subtitles.encode()).hexdigest()[0:6],
                    url=subtitles,
                    codec=Subtitle.Codec.WebVTT,
                    language=title.language,
                    forced=False,
                    sdh=True,
                )
            )

        for track in tracks:
            if isinstance(track, Audio):
                role = track.data["dash"]["adaptation_set"].find("Role")
                if role is not None and role.get("value").lower() in ["description", "descriptive", "alternate"]:
                    track.descriptive = True
                
                track.is_original_lang = track.language == title.language
                track.name = "Original" if track.is_original_lang else None

            elif isinstance(track, Subtitle):
                track.is_original_lang = track.language == title.language
                track.name = "Original" if track.is_original_lang else None
                if not track.forced:
                    track.sdh = True

        return tracks

    def get_chapters(self, title: Episode | Movie) -> Chapters:
        if not (cues := title.data.get("chapters")):
            return Chapters()
        
        chapters = []
        for chapter in cues:
            chapters.append(Chapter(name=chapter.get("breakType"), timestamp=int(chapter.get("startTime")) * 1000))
            if chapter.get("endTime"):
                chapters.append(Chapter(timestamp=int(chapter.get("endTime")) * 1000))
            
        if not any(c.timestamp == "00:00:00.000" for c in chapters):
            chapters.append(Chapter(timestamp=0))

        return sorted(chapters, key=lambda x: x.timestamp)
        
    def get_widevine_service_certificate(self, challenge: bytes, **_: Any) -> bytes | None:
        return self.session.post(url=self.config["endpoints"]["widevine"], data=challenge).content
        
    def get_widevine_license(self, *, challenge: bytes, title: Episode | Movie, track: Any, **kwargs) -> bytes | str | None:
        headers = {
            "Accept": "*/*",
            "Referer": "https://www.crave.ca/",
            "Origin": "https://www.crave.ca",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "payload": base64.b64encode(challenge).decode("utf-8"),
            "playbackContext": {
                "contentId": int(title.id),
                "contentpackageId": title.data["package_id"],
                "platformId": 1,
                "destinationId": title.data["destination_id"],
                "gl": "0",
                "jwt": self.jwt
            }
        }
        r = self.session.post(
            url=self.config["endpoints"]["widevine"],
            headers=headers,
            data=json.dumps(data),
        )
        if not r.ok:
            raise ValueError(f"Failed to get license: {r.text}")

        return r.content
    
    def get_playready_license(self, *, challenge: str, title: Episode | Movie, track: Any, **kwargs) -> bytes | str | None:
        headers = {
            "Accept": "*/*",
            "Referer": "https://www.crave.ca/",
            "Origin": "https://www.crave.ca",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "payload": base64.b64encode(challenge.encode("utf-8")).decode("utf-8"),
            "playbackContext": {
                "contentId": int(title.id),
                "contentpackageId": title.data["package_id"],
                "platformId": 1,
                "destinationId": title.data["destination_id"],
                "gl": "0",
                "jwt": self.jwt
            }
        }
        r = self.session.post(
            url=self.config["endpoints"]["playready"],
            headers=headers,
            data=json.dumps(data),
        )
        if not r.ok:
            raise ValueError(f"Failed to get license: {r.text}")

        return r.content
    
    # service-specific methods

    def _get_assets(self, data: dict) -> tuple:
        manifest, trickplay = data.get("playback"), data.get("trickplay", "")
        if not manifest:
            raise ValueError(f"Failed to get manifest URL: {data}")
        
        text_url = trickplay.replace("jpeg", "text")
        subtitles = text_url if self.session.head(text_url).ok else None

        if not subtitles:
            self.log.warning("No external subtitle URL available")

        # Shouldn't be needed with android auth, but just in case
        if "zultimate" not in manifest:
            manifest = re.sub(r"zbest-\w+", "zultimate-11110101", manifest)

        if self.drm_system == "playready":
            manifest = manifest.replace("widevine", "playready")

        return manifest, subtitles
    
    def _get_account(self, access_token: str) -> dict:
        r = self.session.get(
            url=self.config["endpoints"]["account"],
            headers={"authorization": f"Bearer {access_token}"},
        )
        if not r.ok:
            raise ValueError(f"Failed to get account: {r.text}")
        return r.json()
    
    def _get_profile(self, account_id: str, access_token: str) -> dict:
        r = self.session.get(
            url=self.config["endpoints"]["profile"].format(account_id),
            headers={"authorization": f"Bearer {access_token}"},
        )
        if not r.ok:
            raise ValueError(f"Failed to get profile: {r.text}")
        return r.json()
    
    def _get_series(self, content_id: str) -> list[Episode]:
        payload = {
            "operationName": "GetShowpage",
            "variables": {
                "ids": [f"{content_id}"],
                "sessionContext": {"userMaturity": "ADULT", "userLanguage": "EN"}
            },
            "query": """
            query GetShowpage($sessionContext: SessionContext!, $ids: [String!]!) {
                medias(sessionContext: $sessionContext, ids: $ids) {
                    id
                    title
                    mediaType
                    productionYear
                    originalLanguage {
                        id
                        displayName
                    }
                    seasons {
                        id
                        title
                        seasonNumber
                    }
                }
            }
            """
        }
        res = self.session.post(
            url=self.config["endpoints"]["graphql"],
            headers={"authorization": f"Bearer {self.graphql_token}"},
            json=payload
        )
        if not res.ok:
            raise ConnectionError(f"Failed to request the title for {content_id}: {res.text}")
        data = res.json()

        if not data["data"]["medias"]:
            raise ValueError(f"Failed to find the title for {content_id}")
        
        original_language = data["data"]["medias"][0].get("originalLanguage", {}).get("id", "en")
        
        seasons = data["data"]["medias"][0]["seasons"]

        episodes = self._fetch_episodes(seasons)
        
        return [
            Episode(
                id_=episode.get("id"),
                service=self.__class__,
                title=episode.get("media", {}).get("title"),
                year=episode.get("media", {}).get("productionYear"),
                season=int(episode.get("seasonNumber") or 0),
                number=int(episode.get("episodeNumber") or 0),
                name=episode.get("title"),
                language=original_language,
                data=episode,
            )
            for episode in episodes
        ]

    def _get_movie(self, content_id: str) -> list[Movie]:
        payload = {
            "operationName": "GetShowpage",
            "variables": {
                "ids": [f"{content_id}"],
                "sessionContext": {"userMaturity": "ADULT", "userLanguage": "EN"}
            },
            "query": """
            query GetShowpage($sessionContext: SessionContext!, $ids: [String!]!) {
                medias(sessionContext: $sessionContext, ids: $ids) {
                    id
                    title
                    mediaType
                    productionYear
                    originalLanguage {
                        id
                        displayName
                    }
                    firstContent {
                        id
                    }
                }
            }
            """
        }
        res = self.session.post(
            url=self.config["endpoints"]["graphql"],
            headers={"authorization": f"Bearer {self.graphql_token}"},
            json=payload
        )
        if not res.ok:
            raise ConnectionError(f"Failed to request the title for {content_id}: {res.text}")
        data = res.json()

        if not data["data"]["medias"]:
            raise ValueError(f"Failed to find the title for {content_id}")
        
        original_language = data["data"]["medias"][0].get("originalLanguage", {}).get("id", "en")
        title = data["data"]["medias"][0].get("title")

        return [
            Movie(
                id_=data["data"]["medias"][0].get("firstContent", {}).get("id"),
                service=self.__class__,
                name=re.sub(r"\s\(\d{4}\)", "", title),
                year=data["data"]["medias"][0].get("productionYear"),
                language=original_language,
                data=data["data"]["medias"][0],
            )
        ]

    def _get_episode(self, content_id: str) -> json:
        payload = {
            "operationName": "GetPlayerPage",
            "variables": {"ids": [f"{content_id}"], "sessionContext": {"userMaturity": "ADULT", "userLanguage": "EN"}},
            "query": """
            query GetPlayerPage($sessionContext: SessionContext!, $ids: [String!]!) {
            contents(sessionContext: $sessionContext, ids: $ids) {
                id
                path
                title
                shortDescription
                seasonNumber
                episodeNumber
                contentType
                broadcastDate
                locked
                media {
                    id
                    title
                    path
                    mediaType
                    productionYear
                    originalLanguage {
                        id
                        displayName
                    }
                }
            }
        }
        """,
        }
        res = self.session.post(
            url=self.config["endpoints"]["graphql"],
            headers={"authorization": f"Bearer {self.graphql_token}"},
            json=payload
        )
        if not res.ok:
            raise ConnectionError(f"Failed to request the title for {content_id}: {res.text}")
        data = res.json()

        if not (contents := data["data"]["contents"]):
            raise ValueError(f"Failed to find content for {content_id}")
        
        original_language = contents[0].get("media", {}).get("originalLanguage", {}).get("id", "en")

        return [
            Episode(
                id_=content.get("id"),
                service=self.__class__,
                title=content.get("media", {}).get("title"),
                year=content.get("media", {}).get("productionYear"),
                season=int(content.get("seasonNumber") or 0),
                number=int(content.get("episodeNumber") or 0),
                name=content.get("title"),
                language=original_language,
                data=content,
            )
            for content in contents
        ]

    def _fetch_episode(self, season_id: str) -> json:
        payload = {
            "operationName": "GetContentBySeasonId",
            "variables": {
                "id": season_id,
                "sessionContext": {"userMaturity": "ADULT", "userLanguage": "EN"},
                "contentFormat": {"format": "LONGFORM"},
            },
            "query": """
            query GetContentBySeasonId($sessionContext: SessionContext!, $id: String!, $contentFormat: ContentFormatRequest) {
                contentsBySeasonId(
                    sessionContext: $sessionContext
                    id: $id
                    contentFormat: $contentFormat
                ) {
                    id
                    locked
                    title
                    episodeNumber
                    seasonNumber
                    broadcastDate
                    path
                    shortDescription
                    languageIndicators {
                        indicator
                        languages {
                            langCode
                            displayTitle
                            locked
                        }
                    }
                    media {
                        id
                        title
                        path
                        productionYear
                    }
                }
            }
            """
        }
        response = self.session.post(
            url=self.config["endpoints"]["graphql"],
            headers={"authorization": f"Bearer {self.graphql_token}"},
            json=payload
        )
        if not response.ok:
            raise ConnectionError(f"Failed to request the title for {season_id}: {response.text}")
        data = response.json()
        return data["data"]["contentsBySeasonId"]

    def _fetch_episodes(self, data: dict) -> list:
        with ThreadPoolExecutor(max_workers=10) as executor:
            tasks = [executor.submit(self._fetch_episode, x["id"]) for x in data]
            titles = [future.result() for future in as_completed(tasks)]
        return [episode for episodes in titles for episode in episodes]
