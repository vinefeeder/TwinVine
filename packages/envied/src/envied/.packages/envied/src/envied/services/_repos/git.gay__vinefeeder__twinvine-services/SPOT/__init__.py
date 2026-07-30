from __future__ import annotations
import hmac
import hashlib
import re
import time
from http.cookiejar import CookieJar
from typing import Any, Optional
import click
import requests
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import RetryError
from pywidevine.pssh import PSSH
from pywidevine.license_protocol_pb2 import WidevinePsshData
from envied.core.credential import Credential
from envied.core.drm import Widevine
from envied.core.music import MusicTrackOption
from envied.core.service import Service
from envied.core.titles import Music, Song, Titles_T
from envied.core.tracks import Audio, Chapters, Tracks
from envied.core.tracks.track import Track


class _WebApiAuthError(Exception):
    """Raised on API failure."""

class _WebApiRateLimited(Exception):
    """Raised on Rate-Limit."""


class SPOT(Service):
    """
    Service code for Spotify (https://spotify.com)
    www.nostalgic.cc
    Authorization: Cookies, Credentials
    Security: FLAC@L1, AAC@L3
    """

    ALIASES = ("SPOT", "spotify")
    GROUP_AUDIO_DOWNLOADS = True

    TITLE_RE = (
        r"(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?|spotify:)"
        r"(?P<type>track|album|playlist|artist)[:/](?P<id>[0-9A-Za-z]{22})"
    )
    B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    PROTECTION_SCHEME_CENC = 1667591779

    QUALITIES = {
        "AAC_MEDIUM": ("10", "m4a", "AAC 128 kb/s", False, False),
        "AAC_HIGH": ("11", "m4a", "AAC 256 kb/s", False, True),
        "FLAC": ("17", "flac", "FLAC (MP4)", True, True),
        "FLAC_24": ("23", "flac", "FLAC 24-bit (MP4)", True, True),
    }
    QUALITY_MAP = {
        "AAC_MEDIUM": "AAC_MEDIUM", "AAC-MEDIUM": "AAC_MEDIUM", "128": "AAC_MEDIUM",
        "AAC_HIGH": "AAC_HIGH", "AAC-HIGH": "AAC_HIGH", "AAC": "AAC_HIGH", "256": "AAC_HIGH",
        "FLAC": "FLAC", "LOSSLESS": "FLAC",
        "FLAC_24": "FLAC_24", "FLAC-24": "FLAC_24", "24": "FLAC_24",
    }
    FALLBACK_ORDER = ["FLAC_24", "FLAC", "AAC_HIGH", "AAC_MEDIUM"]

    @staticmethod
    @click.command(name="SPOT", short_help="https://spotify.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-q", "--quality", "quality",
                  type=click.Choice(["AAC_MEDIUM", "AAC_HIGH", "FLAC", "FLAC_24",
                                     "128", "256", "AAC", "LOSSLESS", "24"], case_sensitive=False),
                  default=None,
                  help="Audio quality (Default: config default_quality, or FLAC_24). "
                       "FLAC needs premium or will fallback to best available.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return SPOT(ctx, **kwargs)

    def __init__(self, ctx, title: str, quality: Optional[str]):
        super().__init__(ctx)
        self.title = title
        self.endpoints = self.config["endpoints"]
        self.client_version = self.config.get("client_version") or "1.2.87.27.ga2033a72"
        self.pathfinder_hashes = self.config.get("pathfinder_hashes") or {}
        self.cdm = getattr(getattr(ctx, "obj", None), "cdm", None)
        self._flac_licensable: Optional[bool] = None

        if quality:
            self.quality = self.QUALITY_MAP[quality.upper()]
        else:
            cfg_q = str(self.config.get("default_quality", "AAC_HIGH")).upper()
            self.quality = self.QUALITY_MAP.get(cfg_q, "AAC_HIGH")

        self.sp_dc: Optional[str] = None
        self.access_token: Optional[str] = None
        self.client_token: Optional[str] = None
        self.token_expiry: float = 0.0
        self.is_premium: bool = False
        self._web_api_throttled: bool = False

        m = re.search(self.TITLE_RE, self.title)
        if not m:
            self.log.error(" - Could not parse a Spotify track/album/playlist/artist URL or URI.")
            raise SystemExit(1)
        self.item_type = m.group("type")
        self.item_id = m.group("id")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.session.headers.update({
            "accept": "application/json",
            "accept-language": "en-US",
            "content-type": "application/json",
            "origin": "https://open.spotify.com",
            "referer": "https://open.spotify.com/",
            "user-agent": self.config.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            ),
            "spotify-app-version": self.client_version,
            "app-platform": "WebPlayer",
        })

        self.session.mount(
            "https://api.spotify.com",
            HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.2,
                                          status_forcelist=[500, 502, 503, 504])),
        )

        self.sp_dc = self._resolve_sp_dc(cookies, credential)
        if not self.sp_dc:
            self.log.error(
                " - No Spotify 'sp_dc' found. Set it in your unshackle config under "
                "services: SPOT: sp_dc: \"VALUE\", or provide an 'sp_dc' "
                "cookie, or credentials as 'sp_dc:VALUE'."
            )
            raise SystemExit(1)
        self.session.cookies.set("sp_dc", self.sp_dc, domain=".spotify.com")

        self._refresh_token()
        self._check_account()
        self.log.info(
            f" + Authenticated with Spotify ({'Premium' if self.is_premium else 'Free'})"
        )

    def _resolve_sp_dc(self, cookies: Optional[CookieJar], credential: Optional[Credential]) -> Optional[str]:
        if credential:
            user = (credential.username or "").strip()
            pw = (credential.password or "").strip()
            if user.lower() in ("sp_dc", "token", "spotify") and pw:
                return pw
            if pw and not user:
                return pw
            if user and not pw:
                return user
        if cookies:
            for cookie in cookies:
                if cookie.name.lower() == "sp_dc" and cookie.value:
                    return cookie.value
        for key in ("sp_dc", "token"):
            value = str(self.config.get(key) or "").strip()
            if value:
                return value
        return None

    def _refresh_token(self) -> None:
        server_time_ms = self._server_time_ms()
        totp = self._generate_totp(server_time_ms)
        version = self._totp_version

        resp = self.session.get(self.endpoints["session_token"], params={
            "reason": "init",
            "productType": "web-player",
            "totp": totp,
            "totpServer": totp,
            "totpVer": version,
        })
        resp.raise_for_status()
        info = resp.json()
        self.access_token = info.get("accessToken")
        if not self.access_token:
            self.log.error(f" - Spotify token request returned no accessToken: {info}")
            raise SystemExit(1)
        self.token_expiry = float(info.get("accessTokenExpirationTimestampMs", 0)) / 1000 or (time.time() + 3000)

        client_id = info.get("clientId")
        try:
            ct = self.session.post(self.endpoints["client_token"], json={
                "client_data": {
                    "client_version": self.client_version,
                    "client_id": client_id,
                    "js_sdk_data": {},
                }
            }, headers={"Accept": "application/json"}).json()
            self.client_token = ct.get("granted_token", {}).get("token")
        except Exception as e:
            self.log.debug(f"client-token fetch failed: {e}")

        self.session.headers["authorization"] = f"Bearer {self.access_token}"
        if self.client_token:
            self.session.headers["client-token"] = self.client_token

    def _ensure_token(self) -> None:
        if not self.access_token or time.time() >= self.token_expiry:
            self._refresh_token()

    def _server_time_ms(self) -> int:
        try:
            resp = self.session.get(self.endpoints["server_time"])
            resp.raise_for_status()
            return int(1e3 * resp.json()["serverTime"])
        except Exception:
            return int(time.time() * 1000)

    def _check_account(self) -> None:
        try:
            resp = self.session.get(f"{self.endpoints['web_api']}/me")
            if resp.status_code == 429:
                self._web_api_throttled = True
                self.log.debug(" - 429 Throttled. Fallback to pathfinder.")
                return
            if resp.status_code == 200:
                self.is_premium = resp.json().get("product") == "premium"
        except RetryError:
            self._web_api_throttled = True
            self.log.debug(" - Rate-limited. Metadata will use Pathfinder.")
        except Exception as e:
            self.log.debug(f"Account check failed: {e}")


    @property
    def _totp_version(self) -> str:
        secrets = self._totp_secrets()
        return max(secrets.keys(), key=int)

    def _totp_secrets(self) -> dict:
        if getattr(self, "_totp_cache", None):
            return self._totp_cache
        pinned_v = self.config.get("totp_version")
        pinned_s = self.config.get("totp_secret")
        if pinned_v and pinned_s:
            self._totp_cache = {str(pinned_v): list(pinned_s)}
            return self._totp_cache
        url = str(self.config.get("totp_secrets_url") or "").strip()
        if not url:
            self.log.error(
                " - No totp secrets url in config. "
                "Set one of them so a web-player token can be generated."
            )
            raise SystemExit(1)
        resp = self.session.get(url, headers={"authorization": "", "client-token": ""})
        resp.raise_for_status()
        self._totp_cache = {str(k): v for k, v in resp.json().items()}
        return self._totp_cache

    def _generate_totp(self, timestamp_ms: int) -> str:
        secrets = self._totp_secrets()
        version = max(secrets.keys(), key=int)
        cipher = secrets[version]
        secret = "".join(str(byte ^ ((i % 33) + 9)) for i, byte in enumerate(cipher)).encode("ascii")
        counter = int(timestamp_ms) // 1000 // 30
        h = hmac.new(secret, counter.to_bytes(8, "big"), hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        binary = ((h[offset] & 0x7F) << 24 | (h[offset + 1] & 0xFF) << 16
                  | (h[offset + 2] & 0xFF) << 8 | (h[offset + 3] & 0xFF))
        return str(binary % 1_000_000).zfill(6)

    def _api(self, path: str, params: Optional[dict] = None) -> dict:
        if self._web_api_throttled:
            raise _WebApiRateLimited(f"web-api throttled. Skipping: {path}")
        self._ensure_token()
        try:
            resp = self.session.get(f"{self.endpoints['web_api']}{path}", params=params or {})
        except RetryError as e:
            self._web_api_throttled = True
            raise _WebApiRateLimited(f"429 on {path}") from e
        if resp.status_code == 429:
            self._web_api_throttled = True
            raise _WebApiRateLimited(f"429 on {path}")
        if resp.status_code in (401, 403):
            raise _WebApiAuthError(f"{resp.status_code} on {path}")
        if resp.status_code != 200:
            self.log.error(f" - Spotify Web API error on {path}: {resp.status_code} {resp.text[:200]}")
            raise SystemExit(1)
        return resp.json()

    def _media_id_to_gid(self, media_id: str) -> str:
        n = 0
        for ch in media_id:
            n = n * 62 + self.B62.index(ch)
        return f"{n:032x}"

    def get_titles(self) -> Titles_T:
        try:
            return self._titles_web()
        except _WebApiRateLimited as e:
            self.log.warning(
                f" - Web API is being rate-limited ({e}). Using pathfinder metadata."
            )
            return self._titles_pathfinder()
        except _WebApiAuthError as e:
            self.log.warning(
                f" - Web API rejected the token ({e}). Using pathfinder metadata."
            )
            return self._titles_pathfinder()

    def _titles_web(self) -> Titles_T:
        if self.item_type == "track":
            return self._titles_from_track(self.item_id)
        if self.item_type == "album":
            return self._titles_from_album(self.item_id)
        if self.item_type == "playlist":
            return self._titles_from_playlist(self.item_id)
        return self._titles_from_artist(self.item_id)

    def _titles_from_track(self, track_id: str) -> Music:
        track = self._api(f"/tracks/{track_id}")
        album = track.get("album") or {}
        song = self._build_song(track, album)
        return Music(
            [song], kind="single", title=song.album,
            artist=song.album_artist or song.artist, year=song.year,
            total_tracks=1, artwork_url=song.artwork_url,
        )

    def _titles_from_album(self, album_id: str) -> Music:
        album = self._api(f"/albums/{album_id}")
        items = list((album.get("tracks") or {}).get("items") or [])
        next_url = (album.get("tracks") or {}).get("next")
        while next_url:
            page = self._api_absolute(next_url)
            items.extend(page.get("items") or [])
            next_url = page.get("next")
        songs = [self._build_song(t, album) for t in items]
        if not songs:
            self.log.error(f" - No tracks found for album {album_id}."); raise SystemExit(1)
        return Music(
            songs, kind=self._release_kind(album, len(songs)),
            title=album.get("name"), artist=self._first_artist(album),
            year=self._year(album.get("release_date")),
            total_tracks=album.get("total_tracks") or len(songs),
            total_discs=max((s.disc for s in songs), default=1),
            artwork_url=self._cover(album),
            total_duration=sum(int((t.get("duration_ms") or 0) / 1000) for t in items) or None,
        )

    def _titles_from_playlist(self, playlist_id: str) -> Music:
        pl = self._api(f"/playlists/{playlist_id}")
        items = list((pl.get("tracks") or {}).get("items") or [])
        next_url = (pl.get("tracks") or {}).get("next")
        while next_url:
            page = self._api_absolute(next_url)
            items.extend(page.get("items") or [])
            next_url = page.get("next")
        songs = []
        for position, entry in enumerate(items, start=1):
            track = entry.get("track") if isinstance(entry, dict) else None
            if not track or track.get("type") != "track" or not track.get("id"):
                continue
            songs.append(self._build_song(track, track.get("album") or {}, playlist_position=position))
        if not songs:
            self.log.error(f" - No playable tracks found for playlist {playlist_id}."); raise SystemExit(1)
        return Music(
            songs, kind="playlist", title=pl.get("name"),
            artist=(pl.get("owner") or {}).get("display_name") or None,
            total_tracks=len(songs),
            owner=(pl.get("owner") or {}).get("display_name") or None,
            artwork_url=self._cover(pl),
            description=(pl.get("description") or None),
        )

    def _titles_from_artist(self, artist_id: str) -> Music:
        data = self._api(f"/artists/{artist_id}/top-tracks", params={"market": "US"})
        items = data.get("tracks") or []
        songs = []
        for position, track in enumerate(items, start=1):
            songs.append(self._build_song(track, track.get("album") or {}, playlist_position=position))
        if not songs:
            self.log.error(f" - No top tracks found for artist {artist_id}."); raise SystemExit(1)
        return Music(
            songs, kind="playlist",
            title=f"{songs[0].artist} - Top Tracks", artist=songs[0].artist,
            total_tracks=len(songs),
        )

    def _pathfinder(self, operation: str, variables: dict) -> dict:
        self._ensure_token()
        sha = self.pathfinder_hashes.get(operation)
        if not sha:
            self.log.error(f" - No Pathfinder hash configured for '{operation}'."); raise SystemExit(1)
        try:
            resp = self.session.post(self.endpoints["pathfinder"], json={
                "variables": variables,
                "operationName": operation,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
            })
        except RetryError as e:
            self.log.error(
                f" - Pathfinder {operation} is also being rate-limited (429). "
                "Spotify is throttling this IP/token. Wait a while, or route through a proxy/VPN."
            )
            raise SystemExit(1) from e
        if resp.status_code == 429:
            self.log.error(
                f" - Pathfinder {operation} is also being rate-limited (429). "
                "Spotify is throttling this IP/token. Wait a while, or route through a proxy/VPN."
            )
            raise SystemExit(1)
        if resp.status_code != 200:
            self.log.error(f" - Pathfinder {operation} failed: {resp.status_code} {resp.text[:200]}")
            raise SystemExit(1)
        body = resp.json()
        if body.get("errors"):
            self.log.error(f" - Pathfinder {operation} error: {body['errors']}"); raise SystemExit(1)
        return body.get("data") or {}

    def _titles_pathfinder(self) -> Titles_T:
        if self.item_type == "track":
            td = self._pathfinder("getTrack", {"uri": f"spotify:track:{self.item_id}"}).get("trackUnion") or {}
            web = self._pf_track_to_web(td)
            song = self._build_song(web, web.get("album") or {})
            return Music([song], kind="single", title=song.album,
                         artist=song.album_artist or song.artist, year=song.year,
                         total_tracks=1, artwork_url=song.artwork_url)
        if self.item_type == "album":
            return self._pf_album_titles()
        if self.item_type == "playlist":
            return self._pf_playlist_titles()
        self.log.error(" - Artist links aren't supported via the Pathfinder fallback.")
        raise SystemExit(1)

    def _pf_album_titles(self) -> Music:
        ad = self._pathfinder(
            "getAlbum", {"uri": f"spotify:album:{self.item_id}", "offset": 0, "limit": 300}
        ).get("albumUnion") or {}
        items = list((ad.get("tracksV2") or {}).get("items") or [])
        total = (ad.get("tracksV2") or {}).get("totalCount") or len(items)
        while len(items) < total:
            page = self._pathfinder(
                "getAlbum", {"uri": f"spotify:album:{self.item_id}", "offset": len(items), "limit": 300}
            ).get("albumUnion") or {}
            more = (page.get("tracksV2") or {}).get("items") or []
            if not more:
                break
            items.extend(more)
        album_web = self._pf_album_to_web(ad)
        songs = []
        for it in items:
            web = self._pf_track_to_web(it.get("track") or {})
            if not web.get("id"):
                continue
            songs.append(self._build_song(web, album_web))
        if not songs:
            self.log.error(f" - No tracks found for album {self.item_id} (Pathfinder)."); raise SystemExit(1)
        return Music(
            songs, kind=self._release_kind(album_web, len(songs)),
            title=album_web.get("name"), artist=self._first_artist(album_web),
            year=self._year(album_web.get("release_date")),
            total_tracks=album_web.get("total_tracks") or len(songs),
            total_discs=max((s.disc for s in songs), default=1),
            artwork_url=self._cover(album_web),
        )

    def _pf_playlist_titles(self) -> Music:
        pd = self._pathfinder(
            "fetchPlaylist",
            {"uri": f"spotify:playlist:{self.item_id}", "offset": 0, "limit": 300,
             "enableWatchFeedEntrypoint": False},
        ).get("playlistV2") or {}
        content = pd.get("content") or {}
        items = list(content.get("items") or [])
        total = content.get("totalCount") or len(items)
        while len(items) < total:
            page = self._pathfinder(
                "fetchPlaylist",
                {"uri": f"spotify:playlist:{self.item_id}", "offset": len(items), "limit": 300,
                 "enableWatchFeedEntrypoint": False},
            ).get("playlistV2") or {}
            more = (page.get("content") or {}).get("items") or []
            if not more:
                break
            items.extend(more)
        songs = []
        for position, it in enumerate(items, start=1):
            data = (it.get("itemV2") or {}).get("data") or {}
            web = self._pf_track_to_web(data)
            if not web.get("id"):
                continue
            songs.append(self._build_song(web, web.get("album") or {}, playlist_position=position))
        if not songs:
            self.log.error(f" - No playable tracks for playlist {self.item_id} (Pathfinder)."); raise SystemExit(1)
        owner = (pd.get("ownerV2") or {}).get("data") or {}
        return Music(
            songs, kind="playlist", title=pd.get("name"),
            artist=owner.get("name") or None, total_tracks=len(songs),
            owner=owner.get("name") or None,
        )

    def _pf_track_to_web(self, td: dict) -> dict:
        td = td or {}
        uri = td.get("uri") or ""
        album = td.get("albumOfTrack") or td.get("album") or {}
        return {
            "id": uri.split(":")[-1] if uri else td.get("id"),
            "name": td.get("name"),
            "track_number": td.get("trackNumber"),
            "disc_number": td.get("discNumber") or 1,
            "duration_ms": (td.get("duration") or {}).get("totalMilliseconds"),
            "explicit": (td.get("contentRating") or {}).get("label") == "EXPLICIT",
            "external_ids": {},
            "artists": self._pf_artists(td),
            "album": self._pf_album_to_web(album),
        }

    def _pf_album_to_web(self, ad: dict) -> dict:
        ad = ad or {}
        uri = ad.get("uri") or ""
        sources = (ad.get("coverArt") or {}).get("sources") or []
        date = ad.get("date") or {}
        return {
            "id": (uri.split(":")[-1] if uri else None),
            "name": ad.get("name"),
            "images": [{"url": s.get("url")} for s in sources if s.get("url")],
            "release_date": date.get("isoString"),
            "album_type": str(ad.get("type") or "").lower(),
            "total_tracks": (ad.get("tracksV2") or ad.get("tracks") or {}).get("totalCount"),
            "artists": self._pf_artists(ad),
        }

    @staticmethod
    def _pf_artists(obj: dict) -> list:
        obj = obj or {}
        names = []
        for group in ("firstArtist", "otherArtists", "artists"):
            for a in (obj.get(group) or {}).get("items") or []:
                name = (a.get("profile") or {}).get("name") or a.get("name")
                if name:
                    names.append({"name": name})
        return names

    def _api_absolute(self, url: str) -> dict:
        if self._web_api_throttled:
            raise _WebApiRateLimited("API throttled earlier this run.")
        self._ensure_token()
        try:
            resp = self.session.get(url)
        except RetryError as e:
            self._web_api_throttled = True
            raise _WebApiRateLimited("429 on pagination") from e
        if resp.status_code == 429:
            self._web_api_throttled = True
            raise _WebApiRateLimited("429 on pagination")
        if resp.status_code in (401, 403):
            raise _WebApiAuthError(f"{resp.status_code} on pagination")
        if resp.status_code != 200:
            self.log.error(f" - Spotify Web API pagination error: {resp.status_code} {resp.text[:160]}")
            raise SystemExit(1)
        return resp.json()

    def _build_song(self, track: dict, album: dict, playlist_position: Optional[int] = None) -> Song:
        album = album or track.get("album") or {}
        title = (track.get("name") or "Unknown").strip()
        artist = self._first_artist(track) or self._first_artist(album) or "Unknown Artist"
        album_title = (album.get("name") or "Unknown Album").strip()
        album_artist = self._first_artist(album) or artist
        year = self._year(album.get("release_date")) or 1
        disc = int(track.get("disc_number") or 1)
        track_num = playlist_position or int(track.get("track_number") or 1)
        isrc = ((track.get("external_ids") or {}).get("isrc") or "").strip() or None
        artwork = self._cover(album)

        data = {
            "service": self.ALIASES[0],
            "track_id": track.get("id"),
            "album_id": album.get("id"),
            "title": title,
            "artist": artist,
            "album": album_title,
            "album_artist": album_artist,
            "duration": int((track.get("duration_ms") or 0) / 1000),
            "isrc": isrc,
            "artwork_url": artwork,
        }
        return Song(
            id_=track.get("id"),
            service=self.__class__,
            name=title,
            artist=artist,
            album=album_title,
            track=int(track_num),
            disc=int(disc),
            year=int(year),
            album_artist=album_artist,
            release_type=self._release_kind(album, None),
            total_tracks=int(album["total_tracks"]) if album.get("total_tracks") else None,
            explicit=bool(track.get("explicit")),
            isrc=isrc,
            artwork_url=artwork,
            data=data,
        )

    def get_music_track_options(self, song: Song) -> list[MusicTrackOption]:
        fmt = self._effective_quality()
        _fid, container, label, lossless, _prem = self.QUALITIES[fmt]
        data = song.data if isinstance(song.data, dict) else {}
        if lossless:
            option = MusicTrackOption(codec="FLAC", bit_depth=24 if fmt == "FLAC_24" else 16,
                                      sample_rate=44100, channels=2.0, lossless=True,
                                      hires=fmt == "FLAC_24")
        else:
            option = MusicTrackOption(codec="AAC", bitrate=256000 if fmt == "AAC_HIGH" else 128000,
                                      channels=2.0, lossless=False)
        option.explicit = bool(song.explicit)
        option.duration = int(data.get("duration")) if data.get("duration") else None
        option.quality_label = label
        return [option]

    def get_tracks(self, song: Song) -> Tracks:
        track_id = str(song.id)
        fmt, file_id, stream_url, pssh = self._resolve_file(track_id)
        format_id, container, _label, lossless, _prem = self.QUALITIES[fmt]

        drm = Widevine(pssh=pssh, kid=bytes.fromhex(file_id[:32]))

        audio = Audio(
            stream_url,
            language=song.language or "en",
            codec=Audio.Codec.FLAC if lossless else Audio.Codec.AAC,
            bitrate=None if lossless else (256000 if fmt == "AAC_HIGH" else 128000),
            channels=2,
            descriptor=Track.Descriptor.URL,
            drm=[drm],
            id_=track_id,
            data={"spot_ext": container, "spot_format": fmt},
        )
        audio.session = self._download_session()
        self._probe_cdn(stream_url)
        return Tracks([audio])

    def _download_session(self) -> requests.Session:
        s = requests.Session()
        ua = self.session.headers.get("user-agent")
        if ua:
            s.headers["user-agent"] = ua
        if self.session.proxies:
            s.proxies.update(self.session.proxies)
        return s

    def _probe_cdn(self, url: str) -> None:
        if getattr(self, "_cdn_probed", False):
            return
        self._cdn_probed = True
        host = url.split("/", 3)[2] if "//" in url else "?"
        try:
            r = self._download_session().get(
                url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(10, 30)
            )
            ctype = r.headers.get("Content-Type", "?")
            r.close()
            if r.status_code in (200, 206):
                self.log.info(f" + Audio CDN reachable: HTTP {r.status_code} [{host}] {ctype}")
            else:
                self.log.warning(
                    f" - Audio CDN returned HTTP {r.status_code} on a clean-session [{host}]. "
                )
        except Exception as e:
            self.log.warning(f" - Audio CDN probe errored [{host}]: {e}")

    def get_chapters(self, song: Song) -> Chapters:
        return Chapters()

    def _effective_quality(self) -> str:
        fmt = self.quality
        if self.QUALITIES[fmt][4] and not self.is_premium:
            return "AAC_MEDIUM"
        return fmt

    def _resolve_file(self, track_id: str):
        start = self._effective_quality()
        order = self.FALLBACK_ORDER[self.FALLBACK_ORDER.index(start):]
        last = ""
        for fmt in order:
            format_id, _c, _l, lossless, prem = self.QUALITIES[fmt]
            if prem and not self.is_premium:
                continue
            if lossless and self._flac_licensable is False:
                continue
            manifest_key = "file_ids_mp4flac" if lossless else "file_ids_mp4"
            file_id = self._playback_file_id(track_id, manifest_key, format_id)
            if not file_id:
                last = f"no {fmt} file offered"
                continue

            pssh = self._build_pssh(file_id)
            if lossless and self._flac_licensable is None:
                probe = self._try_license(pssh)
                if probe is False:
                    self._flac_licensable = False
                    self.log.warning(f" - {fmt} isn't licensable with this CDM.")
                    last = f"{fmt} not licensable"
                    continue
                if probe is True:
                    self._flac_licensable = True

            stream_url = self._storage_resolve(format_id, file_id)
            if fmt != self.quality:
                self.log.warning(f" - {self.quality} unavailable for this track. Using: {fmt}.")
            return fmt, file_id, stream_url, pssh
        self.log.error(f" - No licensable quality for track {track_id}. {last}")
        raise SystemExit(1)

    def _try_license(self, pssh: PSSH) -> Optional[bool]:
        cdm = self.cdm
        needed = ("open", "get_license_challenge", "parse_license", "get_keys", "close")
        if cdm is None or not all(hasattr(cdm, m) for m in needed):
            return None
        session_id = None
        try:
            session_id = cdm.open()
            try:
                challenge = cdm.get_license_challenge(session_id, pssh, privacy_mode=False)
            except TypeError:
                challenge = cdm.get_license_challenge(session_id, pssh)
            resp = self.session.post(self.endpoints["widevine_license"], data=challenge)
            if resp.status_code in (401, 403):
                return False
            if resp.status_code != 200 or not resp.content:
                return None
            try:
                cdm.parse_license(session_id, resp.content)
                keys = cdm.get_keys(session_id)
            except Exception:
                return False
            return any(getattr(k, "type", None) == "CONTENT" for k in keys)
        except Exception as e:
            self.log.debug(f"license probe error: {e}")
            return None
        finally:
            if session_id is not None:
                try:
                    cdm.close(session_id)
                except Exception:
                    pass

    def _playback_file_id(self, track_id: str, manifest_key: str, format_id: str) -> Optional[str]:
        self._ensure_token()
        try:
            resp = self.session.get(
                self.endpoints["playback_info"].format(media_type="track", media_id=track_id),
                params={"manifestFileFormat": manifest_key},
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            self.log.debug(f"playback-info failed ({manifest_key}): {e}")
            return None
        media = body.get("media") or {}
        item = (media.get(next(iter(media), "")) or {}).get("item") or body.get("item") or {}
        files = (item.get("manifest") or {}).get(manifest_key) or []
        for f in files:
            if str(f.get("format")) == str(format_id):
                return f.get("file_id")
        return None

    def _storage_resolve(self, format_id: str, file_id: str) -> str:
        self._ensure_token()
        resp = self.session.get(self.endpoints["storage_resolve"].format(format_id=format_id, file_id=file_id))
        if resp.status_code != 200:
            self.log.error(f" - storage-resolve failed: {resp.status_code} {resp.text[:160]}")
            raise SystemExit(1)
        urls = resp.json().get("cdnurl") or []
        if not urls:
            self.log.error(" - storage-resolve returned no CDN URL."); raise SystemExit(1)
        return urls[0]

    def _build_pssh(self, file_id: str) -> PSSH:
        wv = WidevinePsshData()
        wv.algorithm = WidevinePsshData.AESCTR
        wv.key_ids.append(bytes.fromhex(file_id[:32]))
        wv.provider = "spotify"
        wv.content_id = bytes.fromhex(file_id)
        wv.protection_scheme = self.PROTECTION_SCHEME_CENC
        return PSSH.new(system_id=PSSH.SystemId.Widevine, init_data=wv.SerializeToString())

    def get_widevine_service_certificate(self, **_: Any) -> None:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Any, track: Any) -> bytes:
        self._ensure_token()
        resp = None
        for attempt in range(3):
            resp = self.session.post(self.endpoints["widevine_license"], data=challenge)
            if resp.status_code == 200 and resp.content:
                return resp.content
            if resp.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        status = resp.status_code if resp is not None else "?"
        body = (resp.text or "")[:300] if resp is not None else ""
        has_ct = bool(self.session.headers.get("client-token"))
        has_auth = bool(self.session.headers.get("authorization"))
        self.log.error(f" - Spotify Widevine license error: {status} {body}")
        if isinstance(status, int) and status >= 500:
            self.log.error(
                " - A 500 from Spotify's license server is a server-side rejection. "
                f"(client-token present: {has_ct}, authorization present: {has_auth}.) "
            )
        raise SystemExit(1)

    def on_track_decrypted(self, track: Any, drm: Any = None, segment: Any = None) -> None:
        try:
            from pathlib import Path
            data = getattr(track, "data", None)
            path = getattr(track, "path", None)
            if not isinstance(data, dict) or not path:
                return
            path = Path(path)
            if not path.exists():
                return
            if str(data.get("spot_format", "")).startswith("AAC"):
                self._rename_ext(track, path, "m4a")
            else:
                self._remux_flac(track, path)
        except Exception as e:
            self.log.debug(f"post-decrypt step skipped: {e}")

    def _rename_ext(self, track: Any, path, ext: str) -> None:
        if path.suffix.lower() == f".{ext}":
            return
        new_path = path.with_suffix(f".{ext}")
        if new_path.exists():
            new_path.unlink()
        path.rename(new_path)
        track.path = new_path

    def _remux_flac(self, track: Any, path) -> None:
        import subprocess
        from envied.core import binaries
        if path.suffix.lower() == ".flac":
            return
        if not binaries.FFMPEG:
            self.log.warning(" - ffmpeg not installed.")
            self._rename_ext(track, path, "mp4")
            return
        out_path = path.with_suffix(".flac")
        if out_path.exists():
            out_path.unlink()
        proc = subprocess.run(
            [str(binaries.FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-map", "0:a", "-c:a", "copy", str(out_path)],
            capture_output=True,
        )
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            self.log.warning(
                f" - FLAC remux failed. ffmpeg: {proc.stderr.decode(errors='ignore')[:200]}"
            )
            if out_path.exists():
                out_path.unlink()
            self._rename_ext(track, path, "mp4")
            return
        path.unlink()
        track.path = out_path

    @staticmethod
    def _first_artist(obj: dict) -> Optional[str]:
        artists = (obj or {}).get("artists") or []
        if artists and isinstance(artists, list):
            name = (artists[0] or {}).get("name")
            return name.strip() if name else None
        return None

    @staticmethod
    def _cover(obj: dict) -> Optional[str]:
        images = (obj or {}).get("images") or []
        if images and isinstance(images, list):
            return images[0].get("url")
        return None

    @staticmethod
    def _year(release_date: Optional[str]) -> int:
        if release_date:
            m = re.match(r"(\d{4})", str(release_date))
            if m:
                return int(m.group(1))
        return 0

    @staticmethod
    def _release_kind(album: dict, track_count: Optional[int]) -> str:
        at = str((album or {}).get("album_type") or "").lower()
        if at in ("single", "compilation", "album"):
            return at
        if track_count is not None and track_count <= 3:
            return "single"
        return "album"
