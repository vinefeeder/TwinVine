import json
import re
from http.cookiejar import CookieJar
from typing import Optional, Iterable
from langcodes import Language
import base64

import click

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Episode, Series, Movie, Movies, Title_T, Titles_T
from envied.core.tracks import Chapter, Tracks, Subtitle, Audio


class HIDI(Service):
    """
    Service code for HiDive (hidive.com)
    Version: 1.2.0
    Authorization: Email + password login, with automatic token refresh.
    Security: FHD@L3
    """

    TITLE_RE = r"^https?://(?:www\.)?hidive\.com/(?:season/(?P<season_id>\d+)|playlist/(?P<playlist_id>\d+))$"
    GEOFENCE = ()
    NO_SUBTITLES = False

    @staticmethod
    @click.command(name="HIDI", short_help="https://hidive.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return HIDI(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        m = re.match(self.TITLE_RE, title)
        if not m:
            raise ValueError("Unsupported HiDive URL. Use /season/<id> or /playlist/<id>")

        self.season_id = m.group("season_id")
        self.playlist_id = m.group("playlist_id")
        self.kind = "serie" if self.season_id else "movie"
        self.content_id = int(self.season_id or self.playlist_id)

        if not self.config:
            raise EnvironmentError("Missing HIDI service config.")
        self.cdm = ctx.obj.cdm
        self._auth_token = None
        self._refresh_token = None
        self._drm_cache = {}

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        base_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US",
            "Referer": "https://www.hidive.com/",
            "Origin": "https://www.hidive.com",
            "x-api-key": self.config["x_api_key"],
            "app": "dice",
            "Realm": "dce.hidive",
            "x-app-var": self.config["x_app_var"],
        }
        self.session.headers.update(base_headers)

        if not credential or not credential.username or not credential.password:
            raise ValueError("HiDive requires email + password")

        r_login = self.session.post(
            self.config["endpoints"]["login"],
            json={"id": credential.username, "secret": credential.password}
        )
        if r_login.status_code == 401:
            raise PermissionError("Invalid email or password.")
        r_login.raise_for_status()

        login_data = r_login.json()
        self._auth_token = login_data["authorisationToken"]
        self._refresh_token = login_data["refreshToken"]

        self.session.headers["Authorization"] = f"Bearer {self._auth_token}"
        self.log.info("HiDive login successful.")

    def _refresh_auth(self):
        if not self._refresh_token:
            raise PermissionError("No refresh token available to renew session.")
        
        self.log.warning("Auth token expired, refreshing...")
        r = self.session.post(
            self.config["endpoints"]["refresh"],
            json={"refreshToken": self._refresh_token}
        )
        if r.status_code == 401:
            raise PermissionError("Refresh token is invalid. Please log in again.")
        r.raise_for_status()
        
        data = r.json()
        self._auth_token = data["authorisationToken"]
        self.session.headers["Authorization"] = f"Bearer {self._auth_token}"
        self.log.info("Auth token refreshed successfully.")

    def _api_get(self, url, **kwargs):
        resp = self.session.get(url, **kwargs)
        if resp.status_code == 401:
            self._refresh_auth()
            resp = self.session.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    def get_titles(self) -> Titles_T:
        # One endpoint for both season and playlist
        resp = self._api_get(
            self.config["endpoints"]["view"],
            params={"type": ("playlist" if self.kind == "movie" else "season"),
                    "id": self.content_id,
                    "timezone": "Europe/Amsterdam"}
        )
        data = resp.json()

        if self.kind == "movie":
            # Find the playlist bucket, then the single VOD
            vod_id = None
            movie_title = None
            description = ""
            for elem in data.get("elements", []):
                if elem.get("$type") == "hero":
                    hdr = (elem.get("attributes", {}).get("header", {}) or {}).get("attributes", {})
                    movie_title = hdr.get("text", movie_title)
                    for c in elem.get("attributes", {}).get("content", []):
                        if c.get("$type") == "textblock":
                            description = c.get("attributes", {}).get("text", description)
                if elem.get("$type") == "bucket" and elem.get("attributes", {}).get("type") == "playlist":
                    items = elem.get("attributes", {}).get("items", [])
                    if items:
                        vod_id = items[0]["id"]
                        if not movie_title:
                            movie_title = items[0].get("title")
                        if not description:
                            description = items[0].get("description", "")
                        break

            if not vod_id:
                raise ValueError("No VOD found in playlist data.")

            return Movies([
                Movie(
                    id_=vod_id,
                    service=self.__class__,
                    name=movie_title or "Unknown Title",
                    description=description or "",
                    year=None,
                    language=Language.get("en"),
                    data={"playlistId": self.content_id}
                )
            ])

        # Series
        episodes = []
        series_title = None
        for elem in data.get("elements", []):
            if elem.get("$type") == "bucket" and elem["attributes"].get("type") == "season":
                for item in elem["attributes"].get("items", []):
                    if item.get("type") != "SEASON_VOD":
                        continue
                    ep_title = item["title"]
                    ep_num = 1
                    if ep_title.startswith("E") and " - " in ep_title:
                        try:
                            ep_num = int(ep_title.split(" - ")[0][1:])
                        except:
                            pass
                    episodes.append(Episode(
                        id_=item["id"],
                        service=self.__class__,
                        title=data.get("metadata", {}).get("series", {}).get("title", "") or "HiDive",
                        season=1,
                        number=ep_num,
                        name=item["title"],
                        description=item.get("description", ""),
                        language=Language.get("en"),
                        data=item,
                    ))
                break

        if not episodes:
            raise ValueError("No episodes found in season data.")
        return Series(sorted(episodes, key=lambda x: x.number))

    def _get_audio_for_langs(self, mpd_url: str, langs: Iterable[Language]) -> list[Audio]:
        merged: list[Audio] = []
        seen = set()

        # Use first available language as fallback, or "en" as ultimate fallback
        fallback_lang = langs[0] if langs else Language.get("en")
        
        dash = DASH.from_url(mpd_url, session=self.session)
        try:
            # Parse with a valid fallback language
            base_tracks = dash.to_tracks(language=fallback_lang)
        except Exception:
            # Try with English as ultimate fallback
            base_tracks = dash.to_tracks(language=Language.get("en"))

        all_audio = base_tracks.audio or []

        for lang in langs:
            # Match by language prefix (e.g. en, ja)
            for audio in all_audio:
                lang_code = getattr(audio.language, "language", "en")
                if lang_code.startswith(lang.language[:2]):
                    key = (lang_code, getattr(audio, "codec", None), getattr(audio, "bitrate", None))
                    if key in seen:
                        continue
                    merged.append(audio)
                    seen.add(key)

        # If nothing matched, just return all available audio tracks
        if not merged and all_audio:
            merged = all_audio

        return merged


    def get_tracks(self, title: Title_T) -> Tracks:
        vod_resp = self._api_get(
            self.config["endpoints"]["vod"].format(vod_id=title.id),
            params={"includePlaybackDetails": "URL"},
        )
        vod = vod_resp.json()

        playback_url = vod.get("playerUrlCallback")
        if not playback_url:
            raise ValueError("No playback URL found.")

        stream_data = self._api_get(playback_url).json()
        dash_list = stream_data.get("dash", [])
        if not dash_list:
            raise ValueError("No DASH streams available.")

        entry = dash_list[0]
        mpd_url = entry["url"]

        # Collect available HiDive metadata languages
        meta_audio_tracks = vod.get("onlinePlaybackMetadata", {}).get("audioTracks", [])
        available_langs = []
        for m in meta_audio_tracks:
            lang_code = (m.get("languageCode") or "").split("-")[0]
            if not lang_code:
                continue
            try:
                available_langs.append(Language.get(lang_code))
            except Exception:
                continue

        # Use first available language as fallback, or English as ultimate fallback
        fallback_lang = available_langs[0] if available_langs else Language.get("en")
        
        # Parse DASH manifest with a valid fallback language
        base_tracks = DASH.from_url(mpd_url, session=self.session).to_tracks(language=fallback_lang)

        audio_tracks = self._get_audio_for_langs(mpd_url, available_langs)

        # Map metadata labels
        meta_audio_map = {m.get("languageCode", "").split("-")[0]: m.get("label") for m in meta_audio_tracks}
        for a in audio_tracks:
            lang_code = getattr(a.language, "language", "en")
            a.name = meta_audio_map.get(lang_code, lang_code)
            a.is_original_lang = (lang_code == title.language.language)

        base_tracks.audio = audio_tracks

        # Subtitles
        subtitles = []
        for sub in entry.get("subtitles", []):
            if sub.get("format", "").lower() != "vtt":
                continue
            lang_code = sub.get("language", "en").replace("-", "_")
            try:
                lang = Language.get(lang_code)
            except Exception:
                lang = Language.get("en")
            subtitles.append(Subtitle(
                id_=f"{lang_code}:vtt",
                url=sub.get("url"),
                language=lang,
                codec=Subtitle.Codec.WebVTT,
                name=lang.language_name(),
            ))
        base_tracks.subtitles = subtitles

        # DRM info
        drm = entry.get("drm", {}) or {}
        jwt = drm.get("jwtToken")
        lic_url = (drm.get("url") or "").strip()
        if jwt and lic_url:
            self._drm_cache[title.id] = (jwt, lic_url)

        return base_tracks


    def _hidive_get_drm_info(self, title: Title_T) -> tuple[str, str]:
        if title.id in self._drm_cache:
            return self._drm_cache[title.id]
        self.get_tracks(title)
        return self._drm_cache[title.id]

    def _decode_hidive_license_payload(self, payload: bytes) -> bytes:
        text = payload.decode("utf-8", errors="ignore")
        prefix = "data:application/octet-stream;base64,"
        if text.startswith(prefix):
            b64 = text.split(",", 1)[1]
            return base64.b64decode(b64)
        return payload

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes | str | None:
        jwt_token, license_url = self._hidive_get_drm_info(title)
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/octet-stream",
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "Origin": "https://www.hidive.com",
            "Referer": "https://www.hidive.com/",
            "X-DRM-INFO": "eyJzeXN0ZW0iOiJjb20ud2lkZXZpbmUuYWxwaGEifQ==",
        }
        r = self.session.post(license_url, data=challenge, headers=headers, timeout=30)
        r.raise_for_status()
        return self._decode_hidive_license_payload(r.content)

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []
