from __future__ import annotations
import re
from http.cookiejar import CookieJar
from typing import Any, Optional
from urllib.parse import urlparse
import click
from envied.core.cdm.detect import is_widevine_cdm
from envied.core.config import config
from envied.core.credential import Credential
from envied.core.music import MusicTrackOption
from envied.core.service import Service
from envied.core.titles import Music, Song, Titles_T
from envied.core.tracks import Audio, Chapters, Tracks
from envied.core.tracks.track import Track
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿]")


class SNDC(Service):
    """
    Service code for SoundCloud (https://soundcloud.com)
    www.nostalgic.cc
    Authorization: Cookies, Credentials
    Security: None
    """

    ALIASES = ("SNDC", "soundcloud", "sc")
    GROUP_AUDIO_DOWNLOADS = True

    TITLE_RE = r"^(?:https?://)?(?:www\.|m\.|on\.)?soundcloud\.com/[^\s?#]+"
    _PRESET_BITRATE = {
        "aac_hq": 256000, "aac_256k": 256000, "aac_160k": 160000, "aac_96k": 96000,
        "aac_1_0": 128000, "mp3_1_0": 128000, "mp3_0_0": 128000, "mp3_0_1": 64000,
        "opus_0_0": 72000,
    }

    @staticmethod
    @click.command(name="SNDC", short_help="https://soundcloud.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-q", "--quality", "quality",
                  type=click.Choice(["original", "aac", "opus", "mp3"], case_sensitive=False),
                  default=None,
                  help="original = Uploader's downloadable file (or best available); "
                       "aac = AAC-256k; opus = Opus-72k; "
                       "mp3 = MP3-128k. Default: original.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return SNDC(ctx, **kwargs)

    def __init__(self, ctx, title: str, quality: Optional[str]):
        super().__init__(ctx)
        self.title = title
        self.quality_pref = (quality or self.config.get("default_quality", "original")).lower()
        self.client_id: Optional[str] = None
        self.oauth_token: Optional[str] = None
        self.cdm = getattr(ctx.obj, "cdm", None)

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.session.headers.update({
            "User-Agent": self.config["user_agent"],
            "Origin": self.config["web_url"],
            "Referer": self.config["web_url"] + "/",
        })
        self.client_id = self._extract_client_id() or (str(self.config.get("client_id") or "").strip() or None)
        if not self.client_id:
            self.log.error(" - Could not get a SoundCloud client_id."); raise SystemExit(1)
        token = self._resolve_token(cookies, credential)

        if token:
            self.oauth_token = token if token.lower().startswith("oauth ") else token
            self.session.headers["Authorization"] = f"OAuth {token}"
            self.log.info(" + Authenticated with SoundCloud")
        else:
            self.log.warning(" - No oauth_token found. Go+ 256k AAC and original downloads "
                             "unavailable. Using the best fallback stream.")
            self.log.warning("   Set it under services: SNDC: oauth_token: in your unshackle "
                             "config, or supply an 'oauth_token' cookie.")

    def _resolve_token(self, cookies: Optional[CookieJar], credential: Optional[Credential]) -> Optional[str]:
        if credential:
            user = (credential.username or "").strip()
            pw = (credential.password or "").strip()
            if user.lower() in ("token", "oauth", "oauth_token", "soundcloud") and pw:
                return pw
            if pw and not user:
                return pw
            if user and not pw:
                return user
        if cookies:
            for cookie in cookies:
                if cookie.name.lower() == "oauth_token" and cookie.value:
                    return cookie.value
        for key in ("oauth_token", "token"):
            value = str(self.config.get(key) or "").strip()
            if value:
                return value
        return None

    def _resolve_short_link(self, url: str) -> str:
        try:
            host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
        except Exception:
            return url
        if host == "on.soundcloud.com":
            try:
                r = self.session.head(url, allow_redirects=True, timeout=15)
                if r.url and "soundcloud.com" in r.url and "on.soundcloud.com" not in r.url:
                    self.log.debug(f" + Resolved short link: {r.url}")
                    return r.url
            except Exception as e:
                self.log.debug(f"Short link resolution failed: {e}")
        return url

    def _extract_client_id(self) -> Optional[str]:
        try:
            page = self.session.get(self.config["web_url"] + "/").text
            scripts = re.findall(r'<script[^>]+src="(https://[^"]+\.js)"', page)
            for src in reversed(scripts):
                try:
                    js = self.session.get(src).text
                except Exception:
                    continue
                m = re.search(r'client_id\s*[:=]\s*"([a-zA-Z0-9]{20,})"', js)
                if m:
                    self.log.debug(f" + Extracted client_id from {src}")
                    return m.group(1)
        except Exception as e:
            self.log.debug(f"client_id extraction failed: {e}")
        return None

    def _api(self, endpoint: str, params: Optional[dict] = None, allow_error: bool = False) -> Optional[Any]:
        url = endpoint if endpoint.startswith("http") else f"{self.config['base_url']}/{endpoint.lstrip('/')}"
        p = {"client_id": self.client_id}
        if params:
            p.update(params)
        resp = self.session.get(url, params=p)
        if resp.status_code != 200:
            if allow_error:
                return None
            self.log.error(f" - SoundCloud API error on {endpoint}: {resp.status_code} {resp.text[:200]}")
            raise SystemExit(1)
        return resp.json()

    def get_titles(self) -> Titles_T:
        obj = self._api("resolve", params={"url": self._resolve_short_link(self.title)})
        kind = obj.get("kind")

        if kind == "track":
            song = self._build_song(obj, total_tracks=1)
            return Music([song], kind="single",
                         title=song.album, artist=song.artist, year=song.year,
                         total_tracks=1, artwork_url=song.artwork_url)

        if kind == "playlist":
            return self._build_playlist(obj)

        self.log.error(f" - Unsupported SoundCloud URL: {kind!r}.")
        raise SystemExit(1)

    def _build_playlist(self, playlist: dict) -> Music:
        items = playlist.get("tracks", [])
        full: dict[int, dict] = {}
        need_ids = [t["id"] for t in items if isinstance(t, dict) and "media" not in t and t.get("id")]
        for t in items:
            if isinstance(t, dict) and t.get("media"):
                full[t["id"]] = t
        for i in range(0, len(need_ids), 50):
            batch = need_ids[i:i + 50]
            fetched = self._api("tracks", params={"ids": ",".join(map(str, batch))}, allow_error=True) or []
            for ft in fetched:
                if ft.get("id"):
                    full[ft["id"]] = ft

        album_title = self._clean(playlist.get("title")) or "Unknown Album"
        is_album = bool(playlist.get("is_album"))
        total = playlist.get("track_count") or len([t for t in items if full.get(t.get("id") if isinstance(t, dict) else t)])
        songs = []
        position = 0
        for entry in items:
            tid = entry.get("id") if isinstance(entry, dict) else entry
            track = full.get(tid)
            if not track:
                continue
            position += 1
            songs.append(self._build_song(track, album_ctx=album_title, position=position, total_tracks=total))

        if not songs:
            self.log.error(" - No playable tracks found in playlist."); raise SystemExit(1)

        return Music(
            songs,
            kind="album" if is_album else "playlist",
            title=album_title,
            artist=self._clean((playlist.get("user") or {}).get("username")),
            year=self._year(playlist),
            total_tracks=len(songs),
            artwork_url=self._cover(playlist.get("artwork_url") or songs[0].artwork_url),
        )

    def _build_song(self, track: dict, album_ctx: Optional[str] = None, position: Optional[int] = None,
                    total_tracks: Optional[int] = None) -> Song:
        pm = track.get("publisher_metadata") or {}
        user = track.get("user") or {}
        title = self._clean(track.get("title")) or "Unknown"

        policy = str(track.get("policy") or "").upper()
        if policy == "SNIP":
            self.log.warning(f" - '{title}' is a paid track. Only a 30s preview available.")
        elif policy == "BLOCK":
            self.log.warning(f" - '{title}' is region-blocked.")

        artist = self._clean(pm.get("artist")) or self._clean(user.get("username")) or "Unknown Artist"
        album = self._clean(album_ctx) or self._clean(pm.get("album_title")) or title
        year = self._year(track) or 1
        artwork = self._cover(track.get("artwork_url") or user.get("avatar_url"))
        genre = self._clean(track.get("genre")) or None
        isrc = pm.get("isrc") or None
        label = self._clean(track.get("label_name")) or None
        release_date = self._release_date(track)

        data = {
            "service": self.ALIASES[0],
            "source": self.ALIASES[0],
            "track_id": str(track.get("id")),
            "track_url": track.get("permalink_url"),
            "title": title,
            "artist": artist,
            "performer": artist,
            "album": album,
            "album_artist": artist,
            "track_number": position or 1,
            "total_tracks": total_tracks,
            "disc_number": 1,
            "genre": genre,
            "isrc": isrc,
            "label": label,
            "release_date": release_date,
            "year": year,
            "copyright": self._clean(pm.get("c_line")) or None,
            "artwork_url": artwork,
            "duration": round((track.get("duration") or 0) / 1000) or None,
            "channels": 2,
            "downloadable": bool(track.get("downloadable")),
            "has_downloads_left": bool(track.get("has_downloads_left")),
            "original_format": track.get("original_format"),
            "track_authorization": track.get("track_authorization"),
            "transcodings": (track.get("media") or {}).get("transcodings", []),
        }
        if config.tag:
            data["comment"] = config.tag
        data["quality"] = self._quality_label(data)

        return Song(
            id_=str(track.get("id")),
            service=self.__class__,
            name=title,
            artist=artist,
            album=album,
            track=int(position or 1),
            disc=1,
            year=int(year),
            album_artist=artist,
            release_type="single" if not album_ctx else "album",
            total_tracks=total_tracks if total_tracks else None,
            genre=genre,
            isrc=isrc if isinstance(isrc, str) else None,
            label=label,
            artwork_url=artwork,
            data=data,
        )

    def get_music_track_options(self, song: Song) -> list[MusicTrackOption]:
        data = song.data if isinstance(song.data, dict) else {}
        use_original = self._will_use_original(data)
        if use_original:
            fmt = (data.get("original_format") or "flac").lower()
            codec = {"flac": "FLAC", "wav": "WAV", "aiff": "AIFF", "aif": "AIFF", "mp3": "MP3", "m4a": "AAC"}.get(fmt, fmt.upper())
            return [MusicTrackOption(codec=codec, channels=2.0, lossless=codec in ("FLAC", "WAV", "AIFF"),
                                     duration=data.get("duration"), quality_label=self._quality_label(data))]
        transcoding, _is_drm = self._pick_stream(data)
        tc = self._tc_codec(transcoding) if transcoding else "aac"
        codec = {"mp3": "MP3", "opus": "OPUS", "aac": "AAC"}.get(tc, "AAC")
        bitrate = self._tc_bitrate(transcoding) if transcoding else None
        return [MusicTrackOption(codec=codec, bitrate=bitrate, channels=2.0,
                                 lossless=False, duration=data.get("duration"),
                                 quality_label=self._quality_label(data))]

    def get_tracks(self, title: Song) -> Tracks:
        data = title.data if isinstance(title.data, dict) else {}
        track_id = str(title.id)

        if self._will_use_original(data):
            url, ext = self._original_download(track_id, data)
            if url:
                codec = Audio.Codec.FLAC if ext == "flac" else None
                audio = Audio(
                    url, language=title.language or "en", codec=codec, channels=2,
                    descriptor=Track.Descriptor.URL, id_=track_id, data={"ext": ext},
                )
                return Tracks([audio])
            self.log.warning(" - Original download unavailable; falling back to stream.")

        transcoding, is_drm = self._pick_stream(data)

        if is_drm:
            drm_audio = self._drm_track(title, track_id, transcoding, data)
            if drm_audio is not None:
                return Tracks([drm_audio])
            self.log.warning(" - DRM stream resolution failed; falling back to a non-DRM stream.")
            transcoding = self._best_stream_transcoding(data)

        if not transcoding:
            self.log.error(f" - No usable stream for track {track_id}."); raise SystemExit(1)
        stream_url = self._resolve_stream(transcoding, data.get("track_authorization"))
        tc = self._tc_codec(transcoding)
        is_progressive = self._tc_progressive(transcoding)
        codec = {"aac": Audio.Codec.AAC, "opus": Audio.Codec.OPUS}.get(tc)
        ext = {"aac": "m4a", "mp3": "mp3", "opus": "opus"}.get(tc, "m4a")
        self.log.debug(
            f" + Stream: preset={transcoding.get('preset')} quality={transcoding.get('quality')} "
            f"protocol={'progressive' if is_progressive else 'hls'} codec={tc}"
        )
        audio = Audio(
            stream_url, language=title.language or "en",
            codec=codec,
            bitrate=self._tc_bitrate(transcoding), channels=2,
            descriptor=Track.Descriptor.URL if is_progressive else Track.Descriptor.HLS,
            id_=track_id,
            data={"ext": ext},
        )
        return Tracks([audio])

    def get_chapters(self, title: Song) -> Chapters:
        return Chapters()

    def _will_use_original(self, data: dict) -> bool:
        return (self.quality_pref == "original"
                and bool(data.get("downloadable")) and bool(data.get("has_downloads_left")))

    def _stream_transcodings(self, data: dict) -> list[dict]:
        out = []
        for t in (data.get("transcodings") or []):
            if not isinstance(t, dict) or not t.get("url") or not t.get("preset"):
                continue
            if str(t.get("preset")).lower().startswith("abr_"):
                continue
            if "encrypted" in str((t.get("format") or {}).get("protocol") or "").lower():
                continue
            out.append(t)
        return out

    @staticmethod
    def _tc_codec(t: dict) -> str:
        preset = str(t.get("preset") or "").lower()
        mime = str((t.get("format") or {}).get("mime_type") or "").lower()
        if "aac" in preset or "mp4a" in mime:
            return "aac"
        if "opus" in preset or "opus" in mime:
            return "opus"
        if "mp3" in preset or "mpeg" in mime:
            return "mp3"
        return "other"

    @staticmethod
    def _tc_progressive(t: dict) -> bool:
        return str((t.get("format") or {}).get("protocol") or "").lower() == "progressive"

    def _tc_bitrate(self, t: dict) -> int:
        preset = str(t.get("preset") or "").lower()
        m = re.search(r"(\d+)k", preset)
        if m:
            return int(m.group(1)) * 1000
        if preset in self._PRESET_BITRATE:
            return self._PRESET_BITRATE[preset]
        quality = str(t.get("quality") or "").lower()
        codec = self._tc_codec(t)
        if codec == "aac":
            return 256000 if quality == "hq" else 128000
        if codec == "mp3":
            return 128000
        if codec == "opus":
            return 72000
        return 128000 if quality == "hq" else 96000

    def _best_stream_transcoding(self, data: dict) -> Optional[dict]:
        candidates = self._stream_transcodings(data)
        if not candidates:
            return None
        priority = {
            "mp3": {"mp3": 3, "aac": 2, "opus": 1},
            "opus": {"opus": 3, "aac": 2, "mp3": 1},
        }.get(self.quality_pref, {"aac": 3, "opus": 2, "mp3": 1})

        def codec_pref(t: dict) -> int:
            return priority.get(self._tc_codec(t), 0)

        def rank(t: dict) -> tuple:
            quality = str(t.get("quality") or "").lower()
            return (
                0 if t.get("snipped") else 1,
                codec_pref(t),
                1 if quality == "hq" else 0,
                1 if self._tc_progressive(t) else 0,
                0 if t.get("is_legacy_transcoding") else 1,
                self._tc_bitrate(t),
            )

        return max(candidates, key=rank)

    def _resolve_stream(self, transcoding: dict, track_authorization: Optional[str]) -> str:
        params = {}
        if track_authorization:
            params["track_authorization"] = track_authorization
        res = self._api(transcoding["url"], params=params)
        url = res.get("url")
        if not url:
            self.log.error(f" - No stream URL returned: {res}"); raise SystemExit(1)
        return url

    def _encrypted_transcoding(self, data: dict) -> Optional[dict]:
        candidates = []
        for t in (data.get("transcodings") or []):
            if not isinstance(t, dict) or not t.get("url") or not t.get("preset"):
                continue
            if str(t.get("preset")).lower().startswith("abr_"):
                continue
            proto = str((t.get("format") or {}).get("protocol") or "").lower()
            if "encrypted" not in proto:
                continue
            candidates.append(t)
        if not candidates:
            return None

        def rank(t: dict) -> tuple:
            proto = str((t.get("format") or {}).get("protocol") or "").lower()
            return (
                1 if proto.startswith("ctr") else 0,
                1 if self._tc_codec(t) == "aac" else 0,
                self._tc_bitrate(t),
            )

        return max(candidates, key=rank)

    def _pick_stream(self, data: dict) -> tuple[Optional[dict], bool]:
        non_drm = self._best_stream_transcoding(data)
        enc = self._encrypted_transcoding(data) if is_widevine_cdm(self.cdm) else None
        if enc is not None:
            if self.quality_pref in ("original", "aac"):
                if non_drm is None or self._tc_bitrate(enc) > self._tc_bitrate(non_drm):
                    return enc, True
            elif non_drm is None:
                return enc, True
        return non_drm, False

    def _resolve_encrypted_stream(self, transcoding: dict, track_authorization: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        params = {}
        if track_authorization:
            params["track_authorization"] = track_authorization
        res = self._api(transcoding["url"], params=params, allow_error=True) or {}
        url = res.get("url")
        token = res.get("licenseAuthToken")
        if not url:
            self.log.warning(f" - No encrypted stream URL returned: {res}")
            return None, None
        return url, token

    def _drm_track(self, title: Song, track_id: str, transcoding: dict, data: dict) -> Optional[Audio]:
        url, token = self._resolve_encrypted_stream(transcoding, data.get("track_authorization"))
        if not url or not token:
            if url and not token:
                self.log.warning(" - Encrypted stream returned no license token.")
            return None
        kbps = self._tc_bitrate(transcoding) // 1000
        self.log.info(f" + Using DRM stream: AAC {kbps} kb/s. Needs a CDM to decrypt.")
        return Audio(
            url,
            language=title.language or "en",
            codec=Audio.Codec.AAC,
            bitrate=self._tc_bitrate(transcoding),
            channels=2,
            descriptor=Track.Descriptor.HLS,
            id_=track_id,
            data={"ext": "m4a", "license_token": token},
        )

    def _license_post(self, kind: str, challenge: bytes, track: Any) -> Optional[bytes]:
        tdata = getattr(track, "data", None)
        token = tdata.get("license_token") if isinstance(tdata, dict) else None
        if not token:
            self.log.error(" - No license token available for the DRM request.")
            return None
        host = str(self.config.get("drm_license_host") or "").rstrip("/")
        if not host:
            self.log.error(" - No drm_license_host configured."); return None
        try:
            resp = self.session.post(
                f"{host}/playback/{kind}",
                params={"license_token": token},
                data=bytes(challenge),
                headers={"Content-Type": "application/octet-stream", "Accept": "*/*"},
            )
        except Exception as e:
            self.log.error(f" - DRM {kind} request failed: {e}")
            return None
        if resp.status_code != 200:
            self.log.error(f" - DRM {kind} request rejected: {resp.status_code} {resp.text[:200]}")
            return None
        return resp.content

    def get_widevine_service_certificate(self, *, challenge: bytes, title: Any = None,
                                         track: Any = None, **_: Any) -> Optional[bytes]:
        return self._license_post("widevine", challenge, track) or None

    def get_widevine_license(self, *, challenge: bytes, title: Any = None,
                             track: Any = None, **_: Any) -> Optional[bytes]:
        return self._license_post("widevine", challenge, track)

    def _original_download(self, track_id: str, data: dict) -> tuple[Optional[str], str]:
        res = self._api(f"tracks/{track_id}/download",
                        params={"app_version": self.config.get("app_version"), "app_locale": "en"},
                        allow_error=True)
        redirect = (res or {}).get("redirectUri")
        if not redirect:
            return None, "flac"
        ext = (data.get("original_format") or "").lower().replace("aif", "aiff")
        if not ext:
            try:
                head = self.session.head(redirect, allow_redirects=True, timeout=15)
                cd = head.headers.get("Content-Disposition", "")
                m = re.search(r'filename="?[^"]+\.([A-Za-z0-9]+)"?', cd)
                if m:
                    ext = m.group(1).lower()
            except Exception:
                pass
        return redirect, (ext or "flac")

    def on_track_downloaded(self, track: Any) -> None:
        try:
            path = getattr(track, "path", None)
            tdata = getattr(track, "data", None)
            if not path or not path.exists() or not isinstance(tdata, dict):
                return
            ext = tdata.get("ext")
            if not ext or path.suffix.lower() == f".{ext}":
                return
            new_path = path.with_suffix(f".{ext}")
            if new_path.exists():
                new_path.unlink()
            path.rename(new_path)
            track.path = new_path
        except Exception as e:
            self.log.debug(f"Extension rename skipped: {e}")

    @staticmethod
    def _clean(value: Any) -> str:
        if not value:
            return ""
        return _INVISIBLE.sub("", str(value)).strip()

    @staticmethod
    def _year(obj: dict) -> int:
        for key in ("release_date", "display_date", "created_at"):
            value = obj.get(key)
            if value:
                m = re.match(r"(\d{4})", str(value))
                if m and int(m.group(1)) > 0:
                    return int(m.group(1))
        return 0

    @staticmethod
    def _release_date(obj: dict) -> Optional[str]:
        for key in ("release_date", "display_date", "created_at"):
            value = obj.get(key)
            if value and re.match(r"\d{4}-\d{2}-\d{2}", str(value)):
                return str(value)[:10]
        return None

    @staticmethod
    def _cover(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        return re.sub(r"-(large|t\d+x\d+|small|badge|tiny|mini|original)\.(jpg|png)", r"-original.\2", url)

    def _quality_label(self, data: dict) -> str:
        if self._will_use_original(data):
            return f"Original {(data.get('original_format') or 'FLAC').upper()}"
        transcoding, _is_drm = self._pick_stream(data)
        if not transcoding:
            return ""
        codec = self._tc_codec(transcoding)
        kbps = self._tc_bitrate(transcoding) // 1000
        if codec == "mp3":
            return f"MP3 {kbps} kb/s"
        if codec == "opus":
            return f"Opus {kbps} kb/s"
        return f"AAC {kbps} kb/s"
