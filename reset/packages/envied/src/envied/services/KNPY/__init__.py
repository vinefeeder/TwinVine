from __future__ import annotations

import hashlib
import re
from collections.abc import Generator
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional

import click
import jwt
from click import Context
from langcodes import Language
from envied.core.credential import Credential
from envied.core.manifests import DASH, HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapters, Subtitle, Tracks


class KNPY(Service):
    """
    \b
    Service code for Kanopy streaming service (https://www.kanopy.com/).

    \b
    Version: 1.0.2
    Author: stabbedbybrick
    Authorization: Credentials
    Geofence: None
    Robustness:
      widevine:
        L3: 1080p
      playready:
        SL2000: 1080p

    \b
    Tips:
        - Input can be complete title URL or video path:
          MOVIE: https://www.kanopy.com/en/lapl/video/16239510 OR /video/16239510 OR /product/16239510
          SERIES: https://www.kanopy.com/en/lapl/video/14949910 OR /video/14949910 OR /product/14949910
          EPISODE: https://www.kanopy.com/en/lapl/video/14949910/14949912 OR /video/14949910/14949912

    \b
    Notes:
        - Available tickets are checked on each run, but they don't appear to be used
          when titles are accessed through the API.

    """

    # GEOFENCE = ()
    ALIASES = ("kanopy",)
    TITLE_RE = r"^(?:.*?/)?(?P<type>watch/video|video|watch|product)/(?P<id1>\d+)(?:/(?P<id2>\d+))?/?$"

    @staticmethod
    @click.command(name="KNPY", short_help="https://www.kanopy.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> KNPY:
        return KNPY(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update(self.config["headers"])

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        cache = self.cache.get(f"tokens_{credential.sha1}")

        if cache and not cache.expired:
            self.log.info(" + Using cached tokens")
            tokens = cache.data
        else:
            self.log.info(" + Logging in...")
            payload = {
                "credentialType": "email",
                "emailUser": {
                    "email": credential.username,
                    "password": credential.password,
                },
            }

            response = self.session.post(
                self.config["endpoints"]["login"], json=payload
            )
            response.raise_for_status()
            data = response.json()

            if not (access_token := data.get("jwt")):
                raise ConnectionError(f"Failed to login: {data}")
            
            user_id = data.get("userId")

            expiry = jwt.decode(access_token, options={"verify_signature": False}).get("exp")
            if not expiry:
                expiry = 86400 # 24 hour fallback

            tokens = {
                "accessToken": access_token,
                "userId": user_id,
            }
            
            cache.set(tokens, expiration=expiry - 3600)

        user = self._get_membership(tokens.get("accessToken"), tokens.get("userId"))

        self.subdomain = user.get("subdomain", "lapl")
        self.site = user.get("sitename")
        self.domain_id = user.get("domainId")
        self.available_tickets = user.get("ticketsAvailable", 0)

        self.log.info(f"Site: {self.site} | Available tickets: {self.available_tickets}")

        self.user_id = tokens.get("userId")
        self.access_token = tokens.get("accessToken")
        self.session.headers.update({"authorization": "Bearer {}".format(self.access_token)})

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "query": self.title,
            "sort": "relevance",
            "rfp": "exclude",
            "domainId": self.domain_id,
            "isKids": "false",
            "page": "0",
            "perPage": "40",
        }

        response = self.session.get(self.config["endpoints"]["search"], params=params)
        response.raise_for_status()
        data = response.json()

        for result in data.get("list", []):
            yield SearchResult(
                id_=f"/video/{result.get('videoId')}",
                title=result.get("title"),
                description=result.get("tagline"),
                label="Video",
                url=f"/video/{result.get('videoId')}",
            )

    def get_titles(self) -> Movies | Series:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(f"Invalid title: {self.title}")

        _, content_id, video_id = (match.group(i) for i in ("type", "id1", "id2"))

        response = self.session.get(
            self.config["endpoints"]["videos"].format(video_id=content_id),
            params={"domainId": self.domain_id, "ageRatingDomainId": self.domain_id},
        )
        response.raise_for_status()
        data = response.json()

        entity_type = data.get("type")
        
        if entity_type in ("video"):
            movie = self._get_movie(data)
            return Movies(movie)
        
        # listed as a single season
        elif entity_type in ("playlist"):
            episodes = self._get_playlist(data)
            if video_id is not None:
                episodes = [episode for episode in episodes if int(episode.id) == int(video_id)]
            return Series(episodes)
        
        # listed as a collection of seasons
        elif entity_type in ("collection"):
            episodes = self._get_collection(data)
            if video_id is not None:
                episodes = [episode for episode in episodes if int(episode.id) == int(video_id)]
            return Series(episodes)

        else:
            raise ValueError(f"Unsupported content type: {entity_type}")

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        json_data = {
            "videoId": title.id,
            "userId": self.user_id,
            "domainId": self.domain_id,
        }

        response = self.session.post(
            self.config["endpoints"]["plays"],
            headers={
                "authorization": "Bearer {}".format(self.access_token),
                **self.config["headers"],
            },
            json=json_data,
        )
        response.raise_for_status()
        data = response.json()

        manifests = data.get("manifests", [])

        stream_data = next((m for m in manifests if m.get("manifestType") == "dash"), None)
        stream_format = DASH

        # Fallback to HLS
        if not stream_data:
            stream_data = next((m for m in manifests if m.get("manifestType") == "hls"), None)
            stream_format = HLS

        if not stream_data:
            raise ValueError(f"Failed to find manifest for title: {title}")

        manifest = stream_data.get("url")
        title.data["drm_id"] = stream_data.get("drmLicenseID")

        self.session.headers.clear()
        tracks = stream_format.from_url(manifest, self.session).to_tracks(title.language)

        for caption in data.get("captions", []):
            language = caption.get("language")
            for sub in caption.get("files", []):
                if sub.get("type") == "transcript":
                    continue

                tracks.add(
                    Subtitle(
                        id_=hashlib.md5(sub.get("url").encode()).hexdigest()[0:6],
                        codec=Subtitle.Codec.from_codecs(sub.get("url").split(".")[-1]),
                        language=language,
                        url=sub.get("url"),
                        sdh="CC" in caption.get("label", ""),
                    )
                )

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        return Chapters()
    
    def get_widevine_service_certificate(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (drm_id := title.data.get("drm_id")):
            return None
        
        license_url = self.config["endpoints"]["widevine"].format(drm_id=drm_id)

        headers = {
            "authorization": "Bearer {}".format(self.access_token),
            "content-type": "application/octet-stream",
            **self.config["headers"]
        }

        r = self.session.post(url=license_url, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content
    
    def get_playready_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (drm_id := title.data.get("drm_id")):
            return None
        
        license_url = self.config["endpoints"]["playready"].format(drm_id=drm_id)

        headers = {
            "authorization": "Bearer {}".format(self.access_token),
            "content-type": "text/xml; charset=utf-8",
            "SOAPAction": "http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense",
            **self.config["headers"]
        }

        r = self.session.post(url=license_url, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content

    # Service-specific methods

    def _get_playlist(self, data: dict) -> list[Episode]:
        if not (metadata := data.get("playlist")):
            self.log.error(" - Error: No metadata found for this title.")
            return []
        
        series_title = metadata.get("title")
        season = re.search(r"\b(?:S|Season|Series)\s*(\d+)", series_title, re.IGNORECASE)
        season_number = int(season.group(1)) if season else 1
        
        response = self.session.get(
            self.config["endpoints"]["items"].format(video_id=metadata.get("videoId")),
            params={"domainId": self.domain_id, "ageRatingDomainId": self.domain_id}
        )
        if response.status_code != 200:
            self.log.error(f" - Error: Failed to fetch playlist for ID: {metadata.get('videoId')} - {response.status_code}")
            return []
        
        data = response.json()

        videos = []
        for e, item in enumerate(data.get("list", [])):
            if item.get("type", "").lower() != "video":
                continue

            episode = item.get("video", {})
            if episode:
                episode["number"] = e + 1
            videos.append(episode)
        
        episodes = []
        for video in videos:
            lang = next((x.get("name") for x in video.get("taxonomies", {}).get("languages", [])), "English")
            episodes.append(
                Episode(
                    id_=video.get("videoId"),
                    service=self.__class__,
                    title=series_title,
                    season=season_number,
                    number=video.get("number", 0),
                    name=video.get("title"),
                    year=video.get("productionYear"),
                    language=Language.find(lang).to_alpha3(),
                    data=video,
                )
            )

        return episodes

    def _get_collection(self, data: dict) -> list[Episode]:
        if not (metadata := data.get("collection")):
            self.log.error(" - Error: No metadata found for this title.")
            return []
        
        series_title = metadata.get("title")
        
        response = self.session.get(
            self.config["endpoints"]["items"].format(video_id=metadata.get("videoId")),
            params={"domainId": self.domain_id, "ageRatingDomainId": self.domain_id}
        )
        response.raise_for_status()
        data = response.json()

        playlists = [i for i in data.get("list", []) if i.get("type", "").lower() == "playlist"]
        if not playlists:
            self.log.error(" - Error: No playlists found for this title.")
            return []
        
        episodes = []
        for s, playlist in enumerate(playlists):
            playlist_episodes = self._get_playlist(playlist)

            for video in playlist_episodes:
                video.title = series_title
                video.season = video.season or s + 1

            episodes.extend(playlist_episodes)
        
        return episodes
        
    def _get_movie(self, data: dict) -> list[Movie]:
        if not (metadata := data.get("video")):
            self.log.error(" - Error: No metadata found for this title.")
            return []
        
        lang = next((x.get("name") for x in metadata.get("languages", [])), "English")

        return [
            Movie(
                id_=metadata.get("videoId"),
                service=self.__class__,
                name=metadata.get("title"),
                year=metadata.get("productionYear"),
                language=Language.find(lang).to_alpha3(),
                data=metadata,
            )
        ]
    
    def _get_membership(self, access_token: str, user_id: str) -> dict:
        response = self.session.get(
            self.config["endpoints"]["memberships"],
            headers={"authorization": "Bearer {}".format(access_token)},
            params={"userId": user_id},
        )
        response.raise_for_status()
        data = response.json()
        
        user_info = next((i for i in data.get("list", []) if i.get("userId") == user_id), None)
        if not user_info:
            raise ValueError(f"Failed to get membership info for user: {data}")
        
        if user_info.get("status", "").lower() != "active":
            raise ValueError(f"User is not active: {user_info}")
        
        return user_info


