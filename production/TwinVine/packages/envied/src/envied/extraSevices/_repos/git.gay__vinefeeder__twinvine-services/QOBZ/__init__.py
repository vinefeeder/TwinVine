from __future__ import annotations
import base64
import hashlib
import re
import time
from http.cookiejar import CookieJar
from typing import Any, Optional
import click
import requests
from envied.core.config import config
from envied.core.credential import Credential
from envied.core.music import MusicTrackOption
from envied.core.service import Service
from envied.core.titles import Music, Song, Titles_T
from envied.core.tracks import Audio, Chapters, Tracks
from envied.core.tracks.track import Track


class QOBZ(Service):
    """
    Service code for Qobuz (https://qobuz.com)
    www.nostalgic.cc
    Authorization: Credentials, Tokens
    Security: None
    """

    ALIASES = ("QOBZ", "qobuz")
    GROUP_AUDIO_DOWNLOADS = True

    TITLE_RE = r"^(?:https?://(?:www\.|open\.|play\.)?qobuz\.com/(?:[a-z]{2}-[a-z]{2}/)?(?P<type>album|track|playlist|interpreter|artist|label)/(?:[^/]+/)*)?(?P<id>[A-Za-z0-9]+)"

    FORMATS = {
        5: ("MP3", "MP3 320 kb/s"),
        6: ("FLAC", "FLAC 16-bit/44.1kHz"),
        7: ("FLAC", "FLAC 24-bit ≤96kHz"),
        27: ("FLAC", "FLAC 24-bit ≤192kHz"),
    }

    QUALITY_MAP = {
        "MP3": 5, "CD": 6, "HIFI": 7, "HIRES": 27,
        "5": 5, "6": 6, "7": 7, "27": 27,
    }

    @staticmethod
    @click.command(name="QOBZ", short_help="https://qobuz.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-q", "--quality", "quality",
                  type=click.Choice(["MP3", "CD", "HIFI", "HIRES", "5", "6", "7", "27"], case_sensitive=False),
                  default=None,
                  help="Quality: MP3/5=MP3 320, CD/6=FLAC 16/44.1, HIFI/7=FLAC 24/96, "
                       "HIRES/27=FLAC 24/192 (default).")
    @click.pass_context
    def cli(ctx, **kwargs):
        return QOBZ(ctx, **kwargs)

    def __init__(self, ctx, title: str, quality: Optional[str]):
        super().__init__(ctx)
        self.title = title
        if quality:
            self.quality = self.QUALITY_MAP[quality.upper()]
        else:
            self.quality = int(self.config.get("default_format_id", 27))

        self.app_id: Optional[str] = None
        self.secrets: list[str] = []
        self.valid_secret: Optional[str] = None
        self.user_auth_token: Optional[str] = None

        m = re.search(self.TITLE_RE, self.title)
        self.item_type = (m.group("type") if m and m.group("type") else "album")
        self.item_id = m.group("id") if m else self.title

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.session.headers.update({"User-Agent": self.config["user_agent"]})

        self.app_id = str(self.config.get("app_id") or "").strip() or None
        self.secrets = [s for s in (self.config.get("secrets") or []) if s]
        if not self.app_id or not self.secrets:
            spoofed_id, spoofed_secrets = self._spoof_app_credentials()
            self.app_id = self.app_id or spoofed_id
            if not self.secrets:
                self.secrets = spoofed_secrets
        if not self.app_id:
            self.log.error(" - Could not find a Qobuz app_id."); raise SystemExit(1)
        self.session.headers["X-App-Id"] = self.app_id

        token = None
        if credential:
            user = (credential.username or "").strip()
            pw = (credential.password or "").strip()
            if user.lower() in ("token", "auth", "authtoken", "user_auth_token"):
                token = pw
            elif user and pw:
                token = self._login(user, pw)
            elif pw and not user:
                token = pw
            elif user and not pw:
                token = None if "@" in user else user
        if not token:
            for key in ("user_auth_token", "token", "auth_token"):
                value = str(self.config.get(key) or "").strip()
                if value:
                    token = value
                    break
        if not token and cookies:
            for cookie in cookies:
                if cookie.name in ("X-User-Auth-Token", "user_auth_token", "qobuz_token"):
                    token = cookie.value
                    break

        if not token:
            self.log.error(
                " - No Qobuz auth. Set it in your unshackle config under "
                "services: QOBZ: token: \"YOUR_AUTH_TOKEN\", or use "
                "credentials as 'token:YOUR_AUTH_TOKEN' or 'email:password'."
            )
            raise SystemExit(1)

        self.user_auth_token = token
        self.session.headers["X-User-Auth-Token"] = token
        self.log.info(" + Authenticated with Qobuz")

    def _login(self, email: str, password: str) -> str:
        login_app_id, _ = self._spoof_app_credentials()
        login_app_id = login_app_id or self.app_id
        self.log.info(f" + Logging in with app_id={login_app_id}")
        attempts = [("plain", password), ("md5", hashlib.md5(password.encode()).hexdigest())]
        for label, pwd in attempts:
            resp = self.session.get(
                self.config["base_url"] + "user/login",
                params={"email": email, "password": pwd, "app_id": login_app_id},
                headers={"X-App-Id": login_app_id},
            )
            self.log.info(f"   login[{label}] HTTP {resp.status_code}: {resp.text[:160]}")
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("user_auth_token")
                if token:
                    self.log.info(f" + Logged in as {data.get('user', {}).get('display_name', email)}")
                    return token
        self.log.error(
            " - Qobuz password login failed. If it's a 401 'User authentication is required', the "
            "login app_id couldn't be extracted from the web bundle. Use a bare user_auth_token in "
            "envied.yaml credentials instead of email:password."
        )
        raise SystemExit(1)

    def _spoof_app_credentials(self) -> tuple[Optional[str], list[str]]:
        try:
            login_page = self.session.get(self.config["web_url"] + "/login").text
            bundle_match = re.search(
                r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>', login_page
            )
            if not bundle_match:
                self.log.warning(" - Could not locate Qobuz bundle.js for credential extraction.")
                return None, []
            bundle = self.session.get(self.config["web_url"] + bundle_match.group(1)).text

            app_id_match = re.search(r'production:\{api:\{appId:"(\d+)",appSecret:', bundle)
            app_id = app_id_match.group(1) if app_id_match else None

            seeds: dict[str, str] = {}
            for m in re.finditer(
                r'[a-z]\.initialSeed\("([\w=]+)",window\.utimezone\.(?P<tz>[a-z]+)\)', bundle
            ):
                seeds[m.group("tz").capitalize()] = m.group(1)

            secrets: list[str] = []
            for m in re.finditer(
                r'name:"\w+/(?P<tz>[A-Z][a-z]+)",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"', bundle
            ):
                tz = m.group("tz")
                if tz not in seeds:
                    continue
                combined = seeds[tz] + m.group("info") + m.group("extras")
                try:
                    secret = base64.standard_b64decode(combined[:-44]).decode("utf-8")
                    if secret:
                        secrets.append(secret)
                except Exception:
                    continue
            self.log.debug(f" + Extracted app_id={app_id}, {len(secrets)} secret(s)")
            return app_id, secrets
        except Exception as e:
            self.log.warning(f" - Qobuz credential extraction failed: {e}")
            return None, []

    def _api(self, endpoint: str, params: dict, allow_error: bool = False) -> Optional[dict]:
        resp = self.session.get(self.config["base_url"] + endpoint, params=params)
        if resp.status_code != 200:
            if allow_error:
                return None
            self.log.error(f" - Qobuz API error on {endpoint}: {resp.status_code} {resp.text[:200]}")
            raise SystemExit(1)
        return resp.json()

    def get_titles(self) -> Titles_T:
        if self.item_type == "track":
            track = self._api("track/get", params={"track_id": self.item_id})
            album = self._api("album/get", params={"album_id": track["album"]["id"]})
            songs = [self._build_song(track, album)]
            return self._build_music(album, songs, kind="single")

        if self.item_type == "playlist":
            return self._get_playlist()

        album = self._get_album_full(self.item_id)
        tracks = album.get("tracks", {}).get("items", [])
        songs = [self._build_song(t, album) for t in tracks]
        return self._build_music(album, songs, kind=self._release_kind(album))

    def _get_album_full(self, album_id: str) -> dict:
        album = self._api("album/get", params={"album_id": album_id, "limit": 500, "offset": 0})
        items = album.get("tracks", {}).get("items", [])
        total = album.get("tracks", {}).get("total", len(items))
        offset = 500
        while len(items) < total:
            page = self._api("album/get", params={"album_id": album_id, "limit": 500, "offset": offset})
            page_items = page.get("tracks", {}).get("items", [])
            if not page_items:
                break
            items.extend(page_items)
            offset += 500
        album.setdefault("tracks", {})["items"] = items
        return album

    def _get_playlist(self) -> Music:
        playlist = self._api("playlist/get", params={
            "playlist_id": self.item_id, "extra": "tracks", "limit": 500, "offset": 0,
        })
        items = playlist.get("tracks", {}).get("items", [])
        total = playlist.get("tracks", {}).get("total", len(items))
        offset = 500
        while len(items) < total:
            page = self._api("playlist/get", params={
                "playlist_id": self.item_id, "extra": "tracks", "limit": 500, "offset": offset,
            })
            page_items = page.get("tracks", {}).get("items", [])
            if not page_items:
                break
            items.extend(page_items)
            offset += 500

        songs = []
        for position, track in enumerate(items, start=1):
            album = track.get("album") or {}
            song = self._build_song(track, album, playlist_position=position)
            songs.append(song)

        music = Music(
            songs,
            kind="playlist",
            title=playlist.get("name"),
            artist=(playlist.get("owner") or {}).get("name"),
            total_tracks=len(songs) or None,
            owner=(playlist.get("owner") or {}).get("name"),
            description=(playlist.get("description") or None),
        )
        return music

    def _build_music(self, album: dict, songs: list[Song], kind: str) -> Music:
        artwork = self._cover_url(album)
        year = self._year(album)
        return Music(
            songs,
            kind=kind,
            title=self._album_title(album),
            artist=(album.get("artist") or {}).get("name"),
            year=year,
            total_tracks=album.get("tracks_count") or (len(songs) or None),
            total_discs=album.get("media_count") or None,
            artwork_url=artwork,
            total_duration=int(album.get("duration") or 0) or None,
        )

    def _build_song(self, track: dict, album: dict, playlist_position: Optional[int] = None) -> Song:
        album = album or track.get("album") or {}
        album_artist = (album.get("artist") or {}).get("name") or "Various Artists"
        performer = (track.get("performer") or {}).get("name") or album_artist
        year = self._year(album) or self._year(track) or 1
        artwork = self._cover_url(album)
        release_date = self._release_date(album) or self._release_date(track)

        title = track.get("title") or ""
        if track.get("version"):
            title = f"{title.strip()} ({track['version']})"

        genre = (album.get("genre") or {}).get("name")
        label = (album.get("label") or {}).get("name")
        composer = (track.get("composer") or {}).get("name")

        data = {
            "service": self.ALIASES[0],
            "source": self.ALIASES[0],
            "track_id": str(track.get("id")),
            "album_id": str(album.get("id")) if album.get("id") else None,
            "track_url": track.get("url"),
            "album_url": album.get("url"),
            "title": title,
            "artist": performer,
            "album": self._album_title(album),
            "album_artist": album_artist,
            "performer": performer,
            "composer": composer,
            "track_number": track.get("track_number"),
            "total_tracks": album.get("tracks_count"),
            "disc_number": track.get("media_number") or 1,
            "total_discs": album.get("media_count") or 1,
            "release_date": release_date,
            "year": year,
            "genre": genre,
            "label": label,
            "isrc": track.get("isrc"),
            "upc": album.get("upc"),
            "copyright": album.get("copyright"),
            "explicit": bool(track.get("parental_warning")),
            "duration": track.get("duration"),
            "channels": 2,
            "artwork_url": artwork,
            "quality": self.FORMATS.get(self.quality, ("", ""))[1],
            "maximum_bit_depth": track.get("maximum_bit_depth") or album.get("maximum_bit_depth"),
            "maximum_sampling_rate": track.get("maximum_sampling_rate") or album.get("maximum_sampling_rate"),
        }
        if config.tag:
            data["comment"] = config.tag

        return Song(
            id_=str(track.get("id")),
            service=self.__class__,
            name=title or "Unknown",
            artist=performer,
            album=self._album_title(album) or "Unknown Album",
            track=int(playlist_position or track.get("track_number") or 1),
            disc=int(track.get("media_number") or 1),
            year=int(year),
            album_artist=album_artist,
            release_type=self._release_kind(album),
            total_tracks=int(album["tracks_count"]) if album.get("tracks_count") else None,
            total_discs=int(album["media_count"]) if album.get("media_count") else None,
            genre=genre,
            explicit=bool(track.get("parental_warning")),
            isrc=track.get("isrc") or None,
            upc=str(album.get("upc")) if album.get("upc") else None,
            copyright=album.get("copyright") or None,
            label=label,
            artwork_url=artwork,
            data=data,
        )

    def get_music_track_options(self, song: Song) -> list[MusicTrackOption]:
        data = song.data if isinstance(song.data, dict) else {}
        codec = self.FORMATS.get(self.quality, ("FLAC", ""))[0]
        bit_depth = None
        sample_rate = None
        if self.quality != 5:
            max_bd = data.get("maximum_bit_depth")
            max_sr = data.get("maximum_sampling_rate")
            bit_depth = min(int(max_bd or 24), 16 if self.quality == 6 else 24)
            sr_cap = {6: 44.1, 7: 96.0, 27: 192.0}.get(self.quality, 192.0)
            sample_rate = int(min(float(max_sr or sr_cap), sr_cap) * 1000)
        hires = bool(bit_depth and bit_depth > 16) or bool(sample_rate and sample_rate > 48000)
        return [MusicTrackOption(
            codec=codec,
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            bitrate=320000 if self.quality == 5 else None,
            channels=2.0,
            lossless=self.quality != 5,
            hires=hires,
            explicit=bool(data.get("explicit")),
            duration=int(data["duration"]) if data.get("duration") else None,
            quality_label=self.FORMATS.get(self.quality, ("", ""))[1],
        )]

    def get_tracks(self, title: Song) -> Tracks:
        track_id = str(title.id)
        file_info = self._get_file_url(track_id, self.quality)
        url = file_info.get("url")
        if not url:
            self.log.error(f" - No file URL for track {track_id}."); raise SystemExit(1)

        actual_format = int(file_info.get("format_id") or self.quality)
        is_mp3 = actual_format == 5 or (file_info.get("mime_type") or "").endswith("mpeg")
        codec = Audio.Codec.FLAC if not is_mp3 else None
        extension = "mp3" if is_mp3 else "flac"

        bit_depth = file_info.get("bit_depth")
        sampling_rate = file_info.get("sampling_rate")
        bitrate = 320000 if is_mp3 else None

        audio = Audio(
            url,
            language=title.language or "en",
            codec=codec,
            bitrate=bitrate,
            channels=2,
            descriptor=Track.Descriptor.URL,
            id_=track_id,
            data={
                "qobuz_ext": extension,
                "bit_depth": bit_depth,
                "sampling_rate": sampling_rate,
            },
        )
        return Tracks([audio])

    def get_chapters(self, title: Song) -> Chapters:
        return Chapters()

    def _get_file_url(self, track_id: str, format_id: int) -> dict:
        secrets_to_try = ([self.valid_secret] if self.valid_secret else []) + [
            s for s in self.secrets if s != self.valid_secret
        ]
        last_error = ""
        for secret in secrets_to_try:
            ts = int(time.time())
            sig_str = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{ts}{secret}"
            request_sig = hashlib.md5(sig_str.encode()).hexdigest()
            resp = self.session.get(
                self.config["base_url"] + "track/getFileUrl",
                params={
                    "request_ts": ts,
                    "request_sig": request_sig,
                    "track_id": track_id,
                    "format_id": format_id,
                    "intent": "stream",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("url"):
                    self.valid_secret = secret
                    return data
                last_error = data.get("message", "no url in response")
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
        self.log.error(f" - Failed to get file URL for track {track_id}: {last_error}")
        raise SystemExit(1)

    def on_track_downloaded(self, track: Any) -> None:
        try:
            path = getattr(track, "path", None)
            data = getattr(track, "data", None)
            if not path or not path.exists() or not isinstance(data, dict):
                return
            extension = data.get("qobuz_ext")
            if not extension or path.suffix.lower() == f".{extension}":
                return
            new_path = path.with_suffix(f".{extension}")
            if new_path.exists():
                new_path.unlink()
            path.rename(new_path)
            track.path = new_path
        except Exception as e:
            self.log.debug(f"Extension rename skipped: {e}")

    @staticmethod
    def _album_title(album: dict) -> str:
        title = album.get("title") or ""
        if album.get("version"):
            title = f"{title.strip()} ({album['version']})"
        return title.strip()

    @staticmethod
    def _year(obj: dict) -> int:
        for key in ("release_date_original", "release_date_stream", "released_at", "release_date"):
            value = obj.get(key)
            if not value:
                continue
            if isinstance(value, (int, float)):
                try:
                    return int(time.gmtime(int(value)).tm_year)
                except Exception:
                    continue
            match = re.match(r"(\d{4})", str(value))
            if match:
                return int(match.group(1))
        return 0

    @staticmethod
    def _release_date(obj: dict) -> Optional[str]:
        for key in ("release_date_original", "release_date_stream", "release_date"):
            value = obj.get(key)
            if value and re.match(r"\d{4}-\d{2}-\d{2}", str(value)):
                return str(value)
        return None

    @staticmethod
    def _cover_url(album: dict) -> Optional[str]:
        image = album.get("image") or {}
        url = image.get("large") or image.get("small") or image.get("thumbnail")
        if not url:
            return None
        return re.sub(r"_(\d+|max|org)\.jpg", "_org.jpg", url)

    @staticmethod
    def _release_kind(album: dict) -> str:
        release_type = (album.get("release_type") or album.get("product_type") or "").lower()
        if release_type in ("single", "ep", "compilation", "album"):
            return release_type
        tracks_count = album.get("tracks_count") or 0
        if tracks_count and tracks_count <= 3:
            return "single"
        return "album"
