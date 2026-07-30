from __future__ import annotations
import hashlib
import re
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Optional
import click
from Cryptodome.Cipher import Blowfish
from envied.core.credential import Credential
from envied.core.music import MusicTrackOption
from envied.core.service import Service
from envied.core.titles import Music, Song, Titles_T
from envied.core.tracks import Audio, Chapters, Tracks
from envied.core.tracks.track import Track


class DEZR(Service):
    """
    Service code for Deezer (https://deezer.com)
    www.nostalgic.cc
    Authorization: Credentials, ARLs
    Security: None
    """

    ALIASES = ("DEZR", "deezer", "DEEZ")
    GROUP_AUDIO_DOWNLOADS = True

    TITLE_RE = (
        r"^(?:https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?(?P<type>track|album|playlist|artist)/)?"
        r"(?P<id>\d+)"
    )

    GW_LIGHT = "https://www.deezer.com/ajax/gw-light.php"
    GET_URL = "https://media.deezer.com/v1/get_url"
    BLOWFISH_SECRET = b"g4el58wc0zvf9na1"
    FORMATS = {
        "FLAC": ("FLAC", "FLAC 16-bit/44.1kHz"),
        "MP3_320": ("MP3_320", "MP3 320 kb/s"),
        "MP3_128": ("MP3_128", "MP3 128 kb/s"),
    }
    QUALITY_MAP = {
        "FLAC": "FLAC", "LOSSLESS": "FLAC", "FUCK": "FLAC",
        "MP3_320": "MP3_320", "320": "MP3_320", "MP3": "MP3_320",
        "MP3_128": "MP3_128", "128": "MP3_128",
    }
    FALLBACK_ORDER = ["FLAC", "MP3_320", "MP3_128"]

    @staticmethod
    @click.command(name="DEZR", short_help="https://deezer.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-q", "--quality", "quality",
                  type=click.Choice(["FLAC", "MP3_320", "MP3_128", "320", "128", "MP3", "LOSSLESS"],
                                    case_sensitive=False),
                  default=None,
                  help="Audio quality (default: config default_quality, or FLAC). "
                       "FLAC needs a Deezer HiFi subscription.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return DEZR(ctx, **kwargs)

    def __init__(self, ctx, title: str, quality: Optional[str]):
        super().__init__(ctx)
        self.title = title

        if quality:
            self.quality = self.QUALITY_MAP[quality.upper()]
        else:
            cfg_q = str(self.config.get("default_quality", "FLAC")).upper()
            self.quality = self.QUALITY_MAP.get(cfg_q, "FLAC")

        self.arl: Optional[str] = None
        self.api_token: str = ""
        self.license_token: Optional[str] = None
        self.lossless_allowed: bool = False

        m = re.search(self.TITLE_RE, self.title)
        if not m:
            self.log.error("Could not parse a Deezer track/album/playlist/artist ID from the input.")
            raise SystemExit(1)
        self.item_type = m.group("type") or "album"
        self.item_id = m.group("id")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.session.headers.update({
            "User-Agent": self.config.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json; charset=UTF-8",
            "Origin": "https://www.deezer.com",
            "Referer": "https://www.deezer.com/",
        })

        self.arl = self._resolve_arl(cookies, credential)
        if not self.arl:
            self.log.error(
                "No Deezer ARL found. Set it in your unshackle config under "
                "services: DEZR: arl: \"YOUR_ARL\", or provide an 'arl' "
                "cookie, or credentials as 'arl:YOUR_ARL'."
            )
            raise SystemExit(1)

        self.session.cookies.set("arl", self.arl, domain=".deezer.com")

        user = self._gw("deezer.getUserData")
        options = (user.get("USER") or {}).get("OPTIONS") or {}
        user_id = (user.get("USER") or {}).get("USER_ID")
        if not user_id or user_id == 0:
            self.log.error("Deezer ARL is invalid or expired. Refresh your ARL.")
            raise SystemExit(1)

        self.api_token = user.get("checkForm") or ""
        self.license_token = options.get("license_token")
        wsq = options.get("web_sound_quality") or {}
        self.lossless_allowed = bool(wsq.get("lossless"))

        if self.quality == "FLAC" and not self.lossless_allowed:
            self.log.warning("FLAC requested but this account has no HiFi/lossless plan.")
        self.log.info(
            f" + Authenticated with Deezer (Lossless {'available' if self.lossless_allowed else 'unavailable'})"
        )

    def _resolve_arl(self, cookies: Optional[CookieJar], credential: Optional[Credential]) -> Optional[str]:
        if credential:
            user = (credential.username or "").strip()
            pw = (credential.password or "").strip()
            if user.lower() in ("arl", "token", "deezer") and pw:
                return pw
            if pw and not user:
                return pw
            if user and not pw:
                return user
        if cookies:
            for cookie in cookies:
                if cookie.name.lower() == "arl" and cookie.value:
                    return cookie.value
        for key in ("arl", "token"):
            value = str(self.config.get(key) or "").strip()
            if value:
                return value
        return None

    def _gw(self, method: str, payload: Optional[dict] = None) -> dict:
        resp = self.session.post(
            self.GW_LIGHT,
            params={"method": method, "input": "3", "api_version": "1.0", "api_token": self.api_token},
            json=payload or {},
        )
        resp.raise_for_status()
        data = resp.json()
        error = data.get("error")
        results = data.get("results")
        if not results:
            raise ValueError(f"Deezer gateway '{method}' failed: {error or 'empty response'}")
        return results

    def get_titles(self) -> Titles_T:
        if self.item_type == "track":
            return self._titles_from_track(self.item_id)
        if self.item_type == "playlist":
            return self._titles_from_playlist(self.item_id)
        if self.item_type == "artist":
            return self._titles_from_artist(self.item_id)
        return self._titles_from_album(self.item_id)

    def _titles_from_track(self, sng_id: str) -> Music:
        song_data = self._gw("song.getData", {"sng_id": sng_id})
        album_data = {}
        alb_id = song_data.get("ALB_ID")
        if alb_id:
            try:
                album_data = self._gw("album.getData", {"alb_id": alb_id})
            except Exception as e:
                self.log.debug(f"Album lookup for track {sng_id} failed: {e}")
        song = self._build_song(song_data, album_data)
        return Music(
            [song],
            kind="single",
            title=song.album,
            artist=song.album_artist or song.artist,
            year=song.year,
            total_tracks=1,
            artwork_url=song.artwork_url,
        )

    def _titles_from_album(self, alb_id: str) -> Music:
        page = self._gw("deezer.pageAlbum", {"alb_id": alb_id, "lang": "en", "tab": 0})
        album_data = page.get("DATA") or {}
        songs_raw = (page.get("SONGS") or {}).get("data") or []
        songs = [self._build_song(s, album_data) for s in songs_raw]
        if not songs:
            self.log.error(f" - No tracks found for album {alb_id}."); raise SystemExit(1)
        return Music(
            songs,
            kind=self._release_kind(album_data, len(songs)),
            title=album_data.get("ALB_TITLE"),
            artist=album_data.get("ART_NAME"),
            year=self._year(album_data),
            total_tracks=len(songs),
            total_discs=max((s.disc for s in songs), default=1),
            artwork_url=self._cover_url(album_data.get("ALB_PICTURE")),
            total_duration=sum(int(s.data.get("duration") or 0) for s in songs) or None,
        )

    def _titles_from_playlist(self, playlist_id: str) -> Music:
        page = self._gw("deezer.pagePlaylist", {
            "playlist_id": playlist_id, "lang": "en", "nb": 2000, "start": 0, "tab": 0, "header": True,
        })
        pl_data = page.get("DATA") or {}
        songs_raw = (page.get("SONGS") or {}).get("data") or []
        songs = []
        for position, s in enumerate(songs_raw, start=1):
            songs.append(self._build_song(s, {}, playlist_position=position))
        if not songs:
            self.log.error(f"No tracks found for playlist {playlist_id}."); raise SystemExit(1)
        return Music(
            songs,
            kind="playlist",
            title=pl_data.get("TITLE"),
            artist=(pl_data.get("PARENT_USERNAME") or None),
            total_tracks=len(songs),
            owner=(pl_data.get("PARENT_USERNAME") or None),
            artwork_url=self._cover_url(pl_data.get("PLAYLIST_PICTURE"), kind="playlist"),
            total_duration=int(pl_data.get("DURATION") or 0) or None,
        )

    def _titles_from_artist(self, artist_id: str) -> Music:
        page = self._gw("artist.getTopTrack", {"art_id": artist_id, "nb": 100})
        songs_raw = page.get("data") or []
        songs = []
        for position, s in enumerate(songs_raw, start=1):
            songs.append(self._build_song(s, {}, playlist_position=position))
        if not songs:
            self.log.error(f"No top tracks found for artist {artist_id}."); raise SystemExit(1)
        artist_name = songs[0].artist
        return Music(
            songs,
            kind="playlist",
            title=f"{artist_name} - Top Tracks",
            artist=artist_name,
            total_tracks=len(songs),
        )

    def _build_song(self, s: dict, album: dict, playlist_position: Optional[int] = None) -> Song:
        album = album or {}
        title = (s.get("SNG_TITLE") or "").strip() or "Unknown"
        version = (s.get("VERSION") or "").strip()
        if version:
            title = f"{title} {version}".strip()

        artist = (s.get("ART_NAME") or "").strip() or "Unknown Artist"
        album_title = (s.get("ALB_TITLE") or album.get("ALB_TITLE") or "").strip() or "Unknown Album"
        album_artist = (album.get("ART_NAME") or s.get("ART_NAME") or artist).strip()
        year = self._year(album) or self._year(s) or 1
        cover_md5 = s.get("ALB_PICTURE") or album.get("ALB_PICTURE")
        artwork = self._cover_url(cover_md5)
        disc = self._to_int(s.get("DISK_NUMBER"), 1)
        track_num = playlist_position or self._to_int(s.get("TRACK_NUMBER"), 1)
        isrc = (s.get("ISRC") or "").strip() or None
        explicit = str(s.get("EXPLICIT_LYRICS", "0")) == "1"

        data = {
            "service": self.ALIASES[0],
            "sng_id": str(s.get("SNG_ID")),
            "track_token": s.get("TRACK_TOKEN"),
            "album_id": str(album.get("ALB_ID") or s.get("ALB_ID") or "") or None,
            "title": title,
            "artist": artist,
            "album": album_title,
            "album_artist": album_artist,
            "duration": self._to_int(s.get("DURATION"), 0),
            "isrc": isrc,
            "artwork_url": artwork,
            "fallback_id": ((s.get("FALLBACK") or {}).get("SNG_ID") if isinstance(s.get("FALLBACK"), dict) else None),
        }

        return Song(
            id_=str(s.get("SNG_ID")),
            service=self.__class__,
            name=title,
            artist=artist,
            album=album_title,
            track=int(track_num),
            disc=int(disc),
            year=int(year),
            album_artist=album_artist,
            release_type=self._release_kind(album, None),
            total_tracks=self._to_int(album.get("NUMBER_TRACK"), None),
            total_discs=self._to_int(album.get("NUMBER_DISK"), None),
            explicit=explicit,
            isrc=isrc,
            label=(album.get("LABEL_NAME") or None),
            artwork_url=artwork,
            data=data,
        )

    def get_music_track_options(self, song: Song) -> list[MusicTrackOption]:
        fmt = self._effective_format()
        deezer_fmt = self.FORMATS[fmt][0]
        data = song.data if isinstance(song.data, dict) else {}
        if deezer_fmt == "FLAC":
            option = MusicTrackOption(
                codec="FLAC", bit_depth=16, sample_rate=44100, channels=2.0,
                lossless=True, hires=False,
            )
        else:
            option = MusicTrackOption(
                codec="MP3", bitrate=320000 if deezer_fmt == "MP3_320" else 128000,
                channels=2.0, lossless=False,
            )
        option.explicit = bool(data.get("explicit")) if "explicit" in data else song.explicit or False
        option.duration = int(data.get("duration")) if data.get("duration") else None
        option.quality_label = self.FORMATS[fmt][1]
        return [option]

    def get_tracks(self, song: Song) -> Tracks:
        sng_id = str(song.id)
        try:
            fresh = self._gw("song.getData", {"sng_id": sng_id})
            track_token = fresh.get("TRACK_TOKEN") or (song.data or {}).get("track_token")
            fallback_id = (fresh.get("FALLBACK") or {}).get("SNG_ID") if isinstance(fresh.get("FALLBACK"), dict) else None
        except Exception as e:
            self.log.debug(f"Fresh token fetch failed for {sng_id}: {e}")
            track_token = (song.data or {}).get("track_token")
            fallback_id = (song.data or {}).get("fallback_id")

        if not track_token:
            self.log.error(f"No track token for '{song.name}' (Not streamable?)."); raise SystemExit(1)

        url, used_fmt, used_id = self._get_stream_url(sng_id, track_token, fallback_id)
        deezer_fmt = self.FORMATS[used_fmt][0]
        is_flac = deezer_fmt == "FLAC"

        audio = Audio(
            url,
            language=song.language or "en",
            codec=Audio.Codec.FLAC if is_flac else None,
            bitrate=None if is_flac else (320000 if deezer_fmt == "MP3_320" else 128000),
            channels=2,
            descriptor=Track.Descriptor.URL,
            id_=sng_id,
            data={
                "dezr_sng_id": str(used_id),
                "dezr_ext": "flac" if is_flac else "mp3",
                "dezr_encrypted": True,
            },
        )
        return Tracks([audio])

    def get_chapters(self, song: Song) -> Chapters:
        return Chapters()

    def _effective_format(self) -> str:
        if self.quality == "FLAC" and not self.lossless_allowed:
            return "MP3_320"
        return self.quality

    def _get_stream_url(self, sng_id: str, track_token: str, fallback_id: Optional[str]):
        start = self._effective_format()
        order = self.FALLBACK_ORDER[self.FALLBACK_ORDER.index(start):]

        last_error = ""
        for fmt in order:
            url, err = self._request_url(track_token, self.FORMATS[fmt][0])
            if url:
                if fmt != self.quality:
                    self.log.warning(f" - {self.quality} unavailable for this track. Using: {fmt}.")
                return url, fmt, sng_id
            last_error = err

        if fallback_id and str(fallback_id) != str(sng_id):
            self.log.warning(f"Track unavailable ({last_error}). Trying fallback {fallback_id}.")
            try:
                fb = self._gw("song.getData", {"sng_id": str(fallback_id)})
                fb_token = fb.get("TRACK_TOKEN")
                if fb_token:
                    return self._get_stream_url(str(fallback_id), fb_token, None)
            except Exception as e:
                self.log.debug(f"Fallback track fetch failed: {e}")

        self.log.error(f"Could not get a stream URL for track {sng_id}. {last_error}")
        raise SystemExit(1)

    def _request_url(self, track_token: str, deezer_fmt: str):
        try:
            resp = self.session.post(self.GET_URL, json={
                "license_token": self.license_token,
                "media": [{
                    "type": "FULL",
                    "formats": [{"cipher": "BF_CBC_STRIPE", "format": deezer_fmt}],
                }],
                "track_tokens": [track_token],
            })
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return None, f"request failed: {e}"

        entries = data.get("data") or []
        if not entries:
            return None, "no data in get_url response"
        entry = entries[0]
        if entry.get("errors"):
            msg = entry["errors"][0].get("message", "unknown error")
            return None, msg
        media = entry.get("media") or []
        if not media or not media[0].get("sources"):
            return None, f"no {deezer_fmt} media available"
        return media[0]["sources"][0]["url"], ""

    def on_track_downloaded(self, track: Any) -> None:
        try:
            data = getattr(track, "data", None)
            path = getattr(track, "path", None)
            if not isinstance(data, dict) or not data.get("dezr_encrypted"):
                return
            if data.get("dezr_done"):
                return
            if not path or not Path(path).exists():
                return

            path = Path(path)
            sng_id = str(data.get("dezr_sng_id"))
            key = self._blowfish_key(sng_id)
            if path.stat().st_size < 2048:
                head = path.read_bytes()[:1]
                if head in (b"{", b"["):
                    self.log.error(
                        f"Deezer returned an error instead of audio for track {sng_id} "
                        "(token/geo/quality). File left in place for inspection."
                    )
                    data["dezr_done"] = True
                    return

            ext = data.get("dezr_ext") or "flac"
            out_path = path.with_suffix(f".{ext}")
            tmp_path = path.with_suffix(path.suffix + ".dec")

            iv = bytes(range(8))  # 00 01 02 03 04 05 06 07
            block_size = 2048
            with path.open("rb") as fi, tmp_path.open("wb") as fo:
                index = 0
                while True:
                    chunk = fi.read(block_size)
                    if not chunk:
                        break
                    if index % 3 == 0 and len(chunk) == block_size:
                        chunk = Blowfish.new(key, Blowfish.MODE_CBC, iv).decrypt(chunk)
                    fo.write(chunk)
                    index += 1

            path.unlink()
            if out_path.exists():
                out_path.unlink()
            tmp_path.rename(out_path)
            track.path = out_path
            data["dezr_done"] = True
        except Exception as e:
            self.log.error(f"Failed to decrypt Deezer track: {e}")
            raise

    def _blowfish_key(self, sng_id: str) -> bytes:
        md5_hex = hashlib.md5(sng_id.encode()).hexdigest()
        return bytes(
            ord(md5_hex[i]) ^ ord(md5_hex[i + 16]) ^ self.BLOWFISH_SECRET[i]
            for i in range(16)
        )

    @staticmethod
    def _to_int(value: Any, default: Optional[int]) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _year(obj: dict) -> int:
        for key in ("DIGITAL_RELEASE_DATE", "PHYSICAL_RELEASE_DATE", "DATE_ADD", "ORIGINAL_RELEASE_DATE"):
            value = obj.get(key)
            if value:
                match = re.match(r"(\d{4})", str(value))
                if match and int(match.group(1)) > 0:
                    return int(match.group(1))
        return 0

    @staticmethod
    def _cover_url(md5: Optional[str], kind: str = "cover") -> Optional[str]:
        if not md5:
            return None
        return f"https://e-cdns-images.dzcdn.net/images/{kind}/{md5}/1400x0-000000-100-0-0.jpg"

    @staticmethod
    def _release_kind(album: dict, track_count: Optional[int]) -> str:
        rt = str((album or {}).get("TYPE") or (album or {}).get("RECORD_TYPE") or "").lower()
        if rt in ("single", "ep", "compile", "compilation", "album"):
            return "compilation" if rt == "compile" else rt
        if track_count is not None and track_count <= 3:
            return "single"
        return "album"
