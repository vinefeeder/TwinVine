import json
import re
import uuid
from http.cookiejar import CookieJar
from typing import Optional, Generator
from langcodes import Language
import base64
import click
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Title_T, Titles_T, Series
from envied.core.tracks import Chapter, Tracks, Subtitle


class MUBI(Service):
    """
    Service code for MUBI (mubi.com)
    Version: 1.2.0

    Authorization: Required cookies (lt token + session)
    Security: FHD @ L3 (Widevine)

    Supports:
      • Series ↦ https://mubi.com/en/nl/series/twin-peaks
      • Movies ↦ https://mubi.com/en/nl/films/the-substance

    """
    SERIES_TITLE_RE = r"^https?://(?:www\.)?mubi\.com(?:/[^/]+)*?/series/(?P<series_slug>[^/]+)(?:/season/(?P<season_slug>[^/]+))?$"
    TITLE_RE = r"^(?:https?://(?:www\.)?mubi\.com)(?:/[^/]+)*?/films/(?P<slug>[^/?#]+)$"
    NO_SUBTITLES = False

    @staticmethod
    @click.command(name="MUBI", short_help="https://mubi.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return MUBI(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)

        m_film = re.match(self.TITLE_RE, title)
        m_series = re.match(self.SERIES_TITLE_RE, title)

        if not m_film and not m_series:
            raise ValueError(f"Invalid MUBI URL: {title}")

        self.is_series = bool(m_series)
        self.slug = m_film.group("slug") if m_film else None
        self.series_slug = m_series.group("series_slug") if m_series else None
        self.season_slug = m_series.group("season_slug") if m_series else None

        self.film_id: Optional[int] = None
        self.lt_token: Optional[str] = None
        self.session_token: Optional[str] = None
        self.user_id: Optional[int] = None
        self.country_code: Optional[str] = None
        self.anonymous_user_id: Optional[str] = None
        self.default_country: Optional[str] = None
        self.reels_data: Optional[list] = None 

        # Store CDM reference
        self.cdm = ctx.obj.cdm

        if self.config is None:
            raise EnvironmentError("Missing service config for MUBI.")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        try:
            r_ip = self.session.get(self.config["endpoints"]["ip_geolocation"], timeout=5)
            r_ip.raise_for_status()
            ip_data = r_ip.json()
            if ip_data.get("country"):
                self.default_country = ip_data["country"]
                self.log.debug(f"Detected country from IP: {self.default_country}")
            else:
                self.log.warning("IP geolocation response did not contain a country code.")
        except Exception as e:
            raise ValueError(f"Failed to fetch IP geolocation: {e}")

        if not cookies:
            raise PermissionError("MUBI requires login cookies.")

        # Extract essential tokens
        lt_cookie = next((c for c in cookies if c.name == "lt"), None)
        session_cookie = next((c for c in cookies if c.name == "_mubi_session"), None)
        snow_id_cookie = next((c for c in cookies if c.name == "_snow_id.c006"), None)

        if not lt_cookie:
            raise PermissionError("Missing 'lt' cookie (Bearer token).")
        if not session_cookie:
            raise PermissionError("Missing '_mubi_session' cookie.")

        self.lt_token = lt_cookie.value
        self.session_token = session_cookie.value

        # Extract anonymous_user_id from _snow_id.c006
        if snow_id_cookie and "." in snow_id_cookie.value:
            self.anonymous_user_id = snow_id_cookie.value.split(".")[0]
        else:
            self.anonymous_user_id = str(uuid.uuid4())
            self.log.warning(f"No _snow_id.c006 cookie found — generated new anonymous_user_id: {self.anonymous_user_id}")

        base_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/143.0",
            "Origin": "https://mubi.com",
            "Referer": "https://mubi.com/",
            "CLIENT": "web",
            "Client-Accept-Video-Codecs": "h265,vp9,h264",
            "Client-Accept-Audio-Codecs": "aac",
            "Authorization": f"Bearer {self.lt_token}",
            "ANONYMOUS_USER_ID": self.anonymous_user_id,
            "Client-Country": self.default_country,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        self.session.headers.update(base_headers)

        r_account = self.session.get(self.config["endpoints"]["account"])
        if not r_account.ok:
            raise PermissionError(f"Failed to fetch MUBI account: {r_account.status_code} {r_account.text}")

        account_data = r_account.json()
        self.user_id = account_data.get("id")
        self.country_code = (account_data.get("country") or {}).get("code", "NL")

        self.session.headers["Client-Country"] = self.country_code
        self.GEOFENCE = (self.country_code,)

        self._bind_anonymous_user()

        self.log.info(
            f"Authenticated as user {self.user_id}, "
            f"country: {self.country_code}, "
            f"anonymous_id: {self.anonymous_user_id}"
        )

    def _bind_anonymous_user(self):
        try:
            r = self.session.put(
                self.config["endpoints"]["current_user"],
                json={"anonymous_user_uuid": self.anonymous_user_id},
                headers={"Content-Type": "application/json"}
            )
            if r.ok:
                self.log.debug("Anonymous user ID successfully bound to account.")
            else:
                self.log.warning(f"Failed to bind anonymous_user_uuid: {r.status_code}")
        except Exception as e:
            self.log.warning(f"Exception while binding anonymous_user_uuid: {e}")

    def get_titles(self) -> Titles_T:
        if self.is_series:
            return self._get_series_titles()
        else:
            return self._get_film_title()

    def _get_film_title(self) -> Movies:
        url = self.config["endpoints"]["film_by_slug"].format(slug=self.slug)
        r = self.session.get(url)
        r.raise_for_status()
        data = r.json()

        self.film_id = data["id"]

        # Fetch reels to get definitive language code and cache the response
        url_reels = self.config["endpoints"]["reels"].format(film_id=self.film_id)
        r_reels = self.session.get(url_reels)
        r_reels.raise_for_status()
        self.reels_data = r_reels.json()

        # Extract original language from the first audio track of the first reel
        original_language_code = "en"  # Default fallback
        if self.reels_data and self.reels_data[0].get("audio_tracks"):
            first_audio_track = self.reels_data[0]["audio_tracks"][0]
            if "language_code" in first_audio_track:
                original_language_code = first_audio_track["language_code"]
                self.log.debug(f"Detected original language from reels: '{original_language_code}'")

        genres = ", ".join(data.get("genres", [])) or "Unknown"
        description = (
            data.get("default_editorial_html", "")
            .replace("<p>", "").replace("</p>", "").replace("<em>", "").replace("</em>", "").strip()
        )
        year = data.get("year")
        name = data.get("title", "Unknown")

        movie = Movie(
            id_=self.film_id,
            service=self.__class__,
            name=name,
            year=year,
            description=description,
            language=Language.get(original_language_code),
            data=data,
        )

        return Movies([movie])

    def _get_series_titles(self) -> Titles_T:
        # Fetch series metadata
        series_url = self.config["endpoints"]["series"].format(series_slug=self.series_slug)
        r_series = self.session.get(series_url)
        r_series.raise_for_status()
        series_data = r_series.json()

        episodes = []

        # If season is explicitly specified, only fetch that season
        if self.season_slug:
            eps_url = self.config["endpoints"]["season_episodes"].format(
                series_slug=self.series_slug,
                season_slug=self.season_slug
            )
            r_eps = self.session.get(eps_url)
            if r_eps.status_code == 404:
                raise ValueError(f"Season '{self.season_slug}' not found.")
            r_eps.raise_for_status()
            episodes_data = r_eps.json().get("episodes", [])
            self._add_episodes_to_list(episodes, episodes_data, series_data)
        else:
            # No season specified fetch ALL seasons
            seasons = series_data.get("seasons", [])
            if not seasons:
                raise ValueError("No seasons found for this series.")

            for season in seasons:
                season_slug = season["slug"]
                eps_url = self.config["endpoints"]["season_episodes"].format(
                    series_slug=self.series_slug,
                    season_slug=season_slug
                )

                self.log.debug(f"Fetching episodes for season: {season_slug}")

                r_eps = self.session.get(eps_url)

                # Stop if season returns 404 or empty
                if r_eps.status_code == 404:
                    self.log.info(f"Season '{season_slug}' not available, skipping.")
                    continue

                r_eps.raise_for_status()
                episodes_data = r_eps.json().get("episodes", [])

                if not episodes_data:
                    self.log.info(f"No episodes found in season '{season_slug}'.")
                    continue

                self._add_episodes_to_list(episodes, episodes_data, series_data)

        from envied.core.titles import Series
        return Series(sorted(episodes, key=lambda x: (x.season, x.number)))

    def _add_episodes_to_list(self, episodes_list: list, episodes_data: list, series_data: dict):
        """Helper to avoid code duplication when adding episodes."""
        for ep in episodes_data:
            # Use episode's own language detection via its consumable.playback_languages
            playback_langs = ep.get("consumable", {}).get("playback_languages", {})
            audio_langs = playback_langs.get("audio_options", ["English"])
            lang_code = audio_langs[0].split()[0].lower() if audio_langs else "en"

            try:
                detected_lang = Language.get(lang_code)
            except:
                detected_lang = Language.get("en")

            episodes_list.append(Episode(
                id_=ep["id"],
                service=self.__class__,
                title=series_data["title"],  # Series title
                season=ep["episode"]["season_number"],
                number=ep["episode"]["number"],
                name=ep["title"],  # Episode title
                description=ep.get("short_synopsis", ""),
                language=detected_lang,
                data=ep,  # Full episode data for later use in get_tracks
            ))

    def get_tracks(self, title: Title_T) -> Tracks:
        film_id = getattr(title, "id", None)
        if not film_id:
            raise RuntimeError("Title ID not found.")

        # For series episodes, we don't have reels cached, so skip reel-based logic
        url_view = self.config["endpoints"]["initiate_viewing"].format(film_id=film_id)
        r_view = self.session.post(url_view, json={}, headers={"Content-Type": "application/json"})
        r_view.raise_for_status()
        view_data = r_view.json()
        reel_id = view_data["reel_id"]

        # For films, use reels data for language/audio mapping
        if not self.is_series:
            if not self.film_id:
                raise RuntimeError("film_id not set. Call get_titles() first.")

            if not self.reels_data:
                self.log.warning("Reels data not cached, fetching now.")
                url_reels = self.config["endpoints"]["reels"].format(film_id=film_id)
                r_reels = self.session.get(url_reels)
                r_reels.raise_for_status()
                reels = r_reels.json()
            else:
                reels = self.reels_data

            reel = next((r for r in reels if r["id"] == reel_id), reels[0])
        else:
            # For episodes, we don’t need reel-based logic — just proceed
            pass

        # Request secure streaming URL, works for both films and episodes
        url_secure = self.config["endpoints"]["secure_url"].format(film_id=film_id)
        r_secure = self.session.get(url_secure)
        r_secure.raise_for_status()
        secure_data = r_secure.json()

        manifest_url = None
        for entry in secure_data.get("urls", []):
            if entry.get("content_type") == "application/dash+xml":
                manifest_url = entry["src"]
                break

        if not manifest_url:
            raise ValueError("No DASH manifest URL found.")

        # Parse DASH, use title.language as fallback
        tracks = DASH.from_url(manifest_url, session=self.session).to_tracks(language=title.language)

        # Add subtitles
        subtitles = []
        for sub in secure_data.get("text_track_urls", []):
            lang_code = sub.get("language_code", "und")
            vtt_url = sub.get("url")
            if not vtt_url:
                continue

            is_original = lang_code == title.language.language

            subtitles.append(
                Subtitle(
                    id_=sub["id"],
                    url=vtt_url,
                    language=Language.get(lang_code),
                    is_original_lang=is_original,
                    codec=Subtitle.Codec.WebVTT,
                    name=sub.get("display_name", lang_code.upper()),
                    forced=False,
                    sdh=False,
                )
            )
        tracks.subtitles = subtitles

        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []

    def get_widevine_license(self, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.user_id:
            raise RuntimeError("user_id not set — authenticate first.")

        dt_custom_data = {
            "userId": self.user_id,
            "sessionId": self.lt_token,
            "merchant": "mubi"
        }
        
        dt_custom_data_b64 = base64.b64encode(json.dumps(dt_custom_data).encode()).decode()

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
            "Accept": "*/*",
            "Origin": "https://mubi.com",
            "Referer": "https://mubi.com/",
            "dt-custom-data": dt_custom_data_b64,
        }

        r = self.session.post(
            self.config["endpoints"]["license"],
            data=challenge,
            headers=headers,
        )
        r.raise_for_status()
        license_data = r.json()
        if license_data.get("status") != "OK":
            raise PermissionError(f"DRM license error: {license_data}")
        return base64.b64decode(license_data["license"])

