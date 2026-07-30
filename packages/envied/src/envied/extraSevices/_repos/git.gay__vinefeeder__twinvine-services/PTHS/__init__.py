import json
import re
from typing import Optional
from http.cookiejar import CookieJar
from langcodes import Language
import click

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Movie, Movies, Title_T, Titles_T
from envied.core.tracks import Tracks


class PTHS(Service):
    """
    Service code for Pathé Thuis (pathe-thuis.nl)
    Version: 1.0.0

    Security: SD @ L3 (Widevine)
              FHD @ L1
    Authorization: Cookies or authentication token

    Supported:
      • Movies → https://www.pathe-thuis.nl/film/{id}

    Note:
      Pathé Thuis does not have episodic content, only movies.
    """

    TITLE_RE = (
        r"^(?:https?://(?:www\.)?pathe-thuis\.nl/film/)?(?P<id>\d+)(?:/[^/]+)?$"
    )
    GEOFENCE = ("NL",)
    NO_SUBTITLES = True 

    @staticmethod
    @click.command(name="PTHS", short_help="https://www.pathe-thuis.nl")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return PTHS(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)

        m = re.match(self.TITLE_RE, title)
        if not m:
            raise ValueError(
                f"Unsupported Pathé Thuis URL or ID: {title}\n"
                "Use e.g. https://www.pathe-thuis.nl/film/30591"
            )

        self.movie_id = m.group("id")
        self.drm_token = None

        if self.config is None:
            raise EnvironmentError("Missing service config for Pathé Thuis.")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        if not cookies:
            self.log.warning("No cookies provided, proceeding unauthenticated.")
            return

        token = next((c.value for c in cookies if c.name == "authenticationToken"), None)
        if not token:
            self.log.info("No authenticationToken cookie found, unauthenticated mode.")
            return

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
            "X-Pathe-Device-Identifier": "web-widevine-1",
            "X-Pathe-Auth-Session-Token": token,
        })
        self.log.info("Authentication token successfully attached to session.")


    def get_titles(self) -> Titles_T:
        url = self.config["endpoints"]["metadata"].format(movie_id=self.movie_id)
        r = self.session.get(url)
        r.raise_for_status()
        data = r.json()

        movie = Movie(
            id_=str(data["id"]),
            service=self.__class__,
            name=data["name"],
            description=data.get("intro", ""),
            year=data.get("year"),
            language=Language.get(data.get("language", "en")),
            data=data,
        )
        return Movies([movie])


    def get_tracks(self, title: Title_T) -> Tracks:
        ticket_id = self._get_ticket_id(title)
        url = self.config["endpoints"]["ticket"].format(ticket_id=ticket_id)

        r = self.session.get(url)
        r.raise_for_status()
        data = r.json()
        stream = data["stream"]

        manifest_url = stream.get("url") or stream.get("drmurl")
        if not manifest_url:
            raise ValueError("No stream manifest URL found.")

        self.drm_token = stream["token"]
        self.license_url = stream["rawData"]["licenseserver"]

        tracks = DASH.from_url(manifest_url, session=self.session).to_tracks(language=title.language)

        return tracks


    def _get_ticket_id(self, title: Title_T) -> str:
        """Fetch the user's owned ticket ID if present."""
        data = title.data
        for t in (data.get("tickets") or []):
            if t.get("playable") and str(t.get("movieId")) == str(self.movie_id):
                return str(t["id"])
        raise ValueError("No valid ticket found for this movie. Ensure purchase or login.")


    def get_chapters(self, title: Title_T):
        return []


    def get_widevine_license(self, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.license_url or not self.drm_token:
            raise ValueError("Missing license URL or token.")

        headers = {
            "Content-Type": "application/octet-stream",
            "Authorization": f"Bearer {self.drm_token}",
        }

        params = {"custom_data": self.drm_token}

        r = self.session.post(self.license_url, params=params, data=challenge, headers=headers)
        r.raise_for_status()

        if not r.content:
            raise ValueError("Empty license response, likely invalid or expired token.")
        return r.content