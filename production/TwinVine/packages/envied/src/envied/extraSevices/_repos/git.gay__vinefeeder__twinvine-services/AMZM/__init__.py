from __future__ import annotations
import base64
import json
import re
import secrets
import struct
import subprocess
import time
import uuid
from http.cookiejar import CookieJar
from typing import Any, Optional
from urllib.parse import unquote, urlparse
import click
import requests
from pyplayready.system.pssh import PSSH as PlayReadyPSSH
from pywidevine.pssh import PSSH as WidevinePSSH
from envied.core import binaries
from envied.core.cdm.detect import is_playready_cdm
from envied.core.config import config
from envied.core.credential import Credential
from envied.core.drm import PlayReady, Widevine
from envied.core.music import MusicTrackOption
from envied.core.service import Service
from envied.core.titles import Music, Song, Titles_T
from envied.core.tracks import Audio, Chapters, Tracks
from envied.core.tracks.track import Track

_INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿]")


class AMZM(Service):
    """
    Service code for Amazon Music (https://music.amazon.com).
    www.nostalgic.cc
    Authorization: Credentials, Cookies
    Security: FLAC@L3/SL2K
    """

    ALIASES = ("AMZM", "amazonmusic", "amusic")
    GROUP_AUDIO_DOWNLOADS = True

    TITLE_RE = r"^(?:https?://)?(?:music\.)?amazon\.(?:com|co\.uk|de|co\.jp|com\.mx|com\.br|fr)/.*?/(?:albums|tracks)/(?P<id>[A-Z0-9]{10,})"

    @staticmethod
    @click.command(name="AMZM", short_help="https://music.amazon.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-c", "--codec", "codec", default=None,
                  type=click.Choice(["FLAC", "AAC", "EC3", "AC4", "OPUS", "MP3"], case_sensitive=False),
                  help="Force an audio codec instead of picking the best available.")
    @click.option("-r", "--region", "region", default=None,
                  help="Account region, one of the keys under 'regions' in config.yaml. "
                       "Defaults to config 'region', else us.")
    @click.option("--single", is_flag=True, default=False,
                  help="For a /tracks/ URL, get just that track instead of its whole album.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return AMZM(ctx, **kwargs)

    def __init__(self, ctx, title: str, codec: Optional[str], region: Optional[str], single: bool):
        super().__init__(ctx)
        self.title = title
        self.forced_codec = (codec or "").lower() or None
        self.single = single

        if not self.config:
            self.log.error(" - Config is missing or empty.")
            raise SystemExit(1)

        regions = self.config.get("regions") or {}
        region = self._resolve_region(region, regions)
        if region not in regions:
            self.log.error(f" - Unknown region {region!r}. Choose from: {', '.join(sorted(regions)) or 'none'}")
            raise SystemExit(1)
        self.region = region
        region_info = regions[region]
        self.base_url = region_info["base"]
        self.activation_url = region_info["activation"]
        self.api_location = region_info["location"]
        self.tvmesk_host = (self.config.get("tvmesk_hosts") or {})[self.api_location]
        self.marketplace_id = region_info["marketplace"]
        self.territory_id = region.upper()

        self.device = self.config.get("device") or {}
        self.device_type_id = self.device["type_id"]
        self.timeout = self.config.get("request_timeout") or 30
        self.registration_timeout = self.config.get("registration_timeout") or 60
        self.codec_priority = self.config.get("codec_priority") or []
        self.device_id: Optional[str] = None
        self.customer_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self.video_player_token: Optional[str] = None
        self.session_handoff_token: Optional[str] = None
        self.quality: str = ""
        self._mpd_cache: dict[str, str] = {}
        self.cdm = getattr(ctx.obj, "cdm", None)
        self.is_playready = is_playready_cdm(self.cdm) if self.cdm else False

    def _resolve_region(self, flag: Optional[str], regions: dict) -> str:
        if flag:
            return flag.strip().lower()

        by_domain = self.config.get("region_from_domain") or {}
        host = (urlparse(self.title if "//" in self.title else f"https://{self.title}").hostname or "").lower()
        from_url = by_domain.get(host.removeprefix("www."))
        configured = str(self.config.get("region") or "").strip().lower()

        if from_url and configured and from_url != configured:
            self.log.info(f" + Using region {from_url.upper()} from the URL "
                          f"(Config says {configured.upper()}). Pass -r to override.")
        return from_url or configured or "us"

    def _endpoint(self, name: str, **extra: str) -> str:
        template = (self.config.get("endpoints") or {}).get(name)
        if not template:
            self.log.error(f" - config.yaml is missing endpoints.{name}")
            raise SystemExit(1)
        return template.format(base=self.base_url, location=self.api_location,
                               tvmesk=self.tvmesk_host, activation=self.activation_url, **extra)

    def _target(self, name: str, **extra: str) -> str:
        target = (self.config.get("amz_targets") or {}).get(name)
        if not target:
            self.log.error(f" - config.yaml is missing amz_targets.{name}")
            raise SystemExit(1)
        return target.format(**extra) if extra else target

    def _music_agent(self, asin: str) -> str:
        agent = self.device.get("music_agent") or ""
        return agent.format(request_id=uuid.uuid4(), asin=asin)

    @property
    def tokens_path(self):
        return self.cache_dir / f"tokens_{self.region}.json"

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.session.headers.update(self.device.get("headers") or {})
        region_name = (self.config["regions"][self.region].get("name") or "").strip()
        self.log.info(f" + Region: {self.region.upper()} ({region_name})")

        tokens = self._load_tokens()
        if not tokens:
            self.log.info(" + No cached tokens, registering a new device.")
            tokens = self._register_device()

        if self._token_expired(tokens):
            self.log.info(" + Access token expired, refreshing.")
            if not self._refresh_token(tokens):
                self.log.warning(" - Refresh failed, re-registering device.")
                tokens = self._register_device()

        self.device_id = tokens.get("device_id")
        self.access_token = tokens.get("x-amz-access-token")
        self.marketplace_id = tokens.get("marketplaceId") or self.marketplace_id
        self.territory_id = tokens.get("musicTerritory") or self.territory_id
        self.video_player_token = self._extract_video_player_token(tokens)

        claims = self._player_token_claims(self.video_player_token)
        self.customer_id = claims.get("customerId")
        self.device_id = claims.get("deviceId") or self.device_id
        self.marketplace_id = claims.get("marketplaceId") or self.marketplace_id
        self.territory_id = claims.get("territoryId") or self.territory_id
        if claims.get("deviceTypeId") and claims["deviceTypeId"] != self.device_type_id:
            self.log.debug(f"Registered deviceTypeId is {claims['deviceTypeId']}, "
                           f"config says {self.device_type_id}; using the registered one")
            self.device_type_id = claims["deviceTypeId"]

        if not self.access_token:
            self.log.error(" - No Amazon Music access token."); raise SystemExit(1)

        self.session.headers.update({
            "x-amz-access-token": self.access_token,
            "x-amzn-device-id": self.device_id or "",
        })
        if self.video_player_token:
            self.session.headers["x-amzn-video-player-token"] = self.video_player_token

        self.log.info(f" + Authenticated with Amazon Music ({self.territory_id})")
        self.log.info(f" + DRM: {'PlayReady' if self.is_playready else 'Widevine'}")

    _PLAYER_TOKEN_CLAIMS = ("customerId", "marketplaceId", "territoryId",
                            "deviceId", "deviceTypeId")

    @classmethod
    def _player_token_claims(cls, token: Optional[str]) -> dict:
        if not token or token.count(".") < 2:
            return {}
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload.encode()).decode("latin-1", errors="ignore")
        except Exception:
            return {}
        claims = {}
        for name in cls._PLAYER_TOKEN_CLAIMS:
            match = re.search(rf'"{name}"\s*:\s*"([^"]+)"', decoded)
            if match:
                claims[name] = match.group(1)
        return claims

    def _load_tokens(self) -> Optional[dict]:
        if not self.tokens_path.exists():
            return None
        try:
            tokens = json.loads(self.tokens_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.log.warning(f" - Could not read cached tokens: {e}")
            return None
        if not tokens.get("service_token") or not tokens.get("device_id"):
            return None
        if isinstance(tokens["service_token"], dict):
            tokens["service_token"] = json.dumps(tokens["service_token"])
        return tokens

    def _save_tokens(self, tokens: dict) -> None:
        try:
            self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
            self.tokens_path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        except Exception as e:
            self.log.warning(f" - Could not cache tokens: {e}")

    @staticmethod
    def _token_expired(tokens: dict) -> bool:
        try:
            expires_at = int(tokens.get("expires_at") or 0)
            if not expires_at and tokens.get("service_token"):
                expires_at = int(json.loads(tokens["service_token"]).get("expiresAtMillis") or 0)
        except Exception:
            return True
        return not expires_at or expires_at <= int(time.time() * 1000) + 60_000

    @staticmethod
    def _extract_video_player_token(tokens: dict) -> Optional[str]:
        header = tokens.get("x-amzn-video-player-token")
        if not header:
            return None
        if isinstance(header, str):
            try:
                return json.loads(header).get("token")
            except Exception:
                return header
        return None

    def _tvmesk_headers(self, device_id: str, **extra: Optional[str]) -> dict:
        headers = {
            "x-amzn-request-id": str(uuid.uuid4()),
            "x-amzn-timestamp": str(int(time.time() * 1000)),
            "x-amzn-device-id": device_id,
        }
        headers.update({k: v for k, v in extra.items() if v})
        return headers

    @staticmethod
    def _find_method(payload: dict, interface: str, key: str) -> Optional[str]:
        for item in payload.get("methods") or []:
            if item.get("interface") == interface and item.get(key):
                return item[key]
        return None

    def _interface(self, name: str) -> str:
        interface = (self.config.get("interfaces") or {}).get(name)
        if not interface:
            self.log.error(f" - config.yaml is missing interfaces.{name}")
            raise SystemExit(1)
        return interface

    def _store_service_token(self, tokens: dict, service_token: Any, video_token: Optional[str]) -> dict:
        if isinstance(service_token, str):
            token_data = json.loads(service_token)
        else:
            token_data = service_token
            service_token = json.dumps(service_token)

        tokens["service_token"] = service_token
        tokens["x-amz-access-token"] = token_data.get("accessToken")
        if token_data.get("marketplaceId"):
            tokens["marketplaceId"] = token_data["marketplaceId"]
        if token_data.get("expiresAtMillis"):
            tokens["expires_at"] = token_data["expiresAtMillis"]
        if video_token:
            tokens["x-amzn-video-player-token"] = video_token
        return tokens

    def _register_device(self) -> dict:
        device_id = secrets.token_hex(8)

        try:
            code_res = self.session.post(
                self._endpoint("show_home"),
                json={"userHash": ""},
                headers=self._tvmesk_headers(device_id),
                timeout=self.registration_timeout,
            )
            code_res.raise_for_status()
            code_json = code_res.json()
        except requests.RequestException as e:
            self.log.error(f" - Could not reach Amazon to start device pairing: {e}")
            raise SystemExit(1)

        template = ((code_json.get("methods") or [{}])[0]).get("template") or {}
        public_code = template.get("code")
        if not public_code:
            self.log.error(f" - No pairing code in response: {json.dumps(code_json)[:300]}")
            raise SystemExit(1)

        register_code = public_code
        try:
            poll_url = template["onPollingIntervalElapsed"][0]["url"]
            if "code=" in poll_url:
                register_code = unquote(poll_url.rsplit("code=", 1)[-1])
        except Exception:
            pass

        self.log.info("")
        self.log.info(" + DEVICE REGISTRATION")
        self.log.info(f"  1. Open: {self._endpoint('activation')}")
        self.log.info(f"  2. Enter: {public_code}")
        self.log.info("  3. Press Enter")
        try:
            input()
        except EOFError:
            self.log.error(" - Registration needs an interactive terminal for the pairing step.")
            raise SystemExit(1)

        reg_json = None
        for attempt in range(1, 4):
            try:
                reg_res = self.session.post(
                    self._endpoint("show_home"),
                    data=json.dumps({"code": register_code}, separators=(",", ":")),
                    headers=self._tvmesk_headers(device_id),
                    timeout=self.registration_timeout,
                )
                reg_res.raise_for_status()
                reg_json = reg_res.json()
                break
            except requests.RequestException as e:
                if attempt == 3:
                    self.log.error(f" - Registration failed after {attempt} attempts: {e}")
                    self.log.error(f" - Code {public_code} may still be valid.")
                    raise SystemExit(1)
                self.log.warning(f" - Registration attempt {attempt} failed ({e}), retrying.")

        service_token = self._find_method(reg_json, self._interface("authentication"), "authentication")
        if not service_token:
            self.log.error(" - Registration did not return a service token.")
            raise SystemExit(1)
        video_token = self._find_method(reg_json, self._interface("video_player"), "header")

        tokens = self._store_service_token(
            {"device_id": device_id, "musicTerritory": self.territory_id}, service_token, video_token
        )
        self._save_tokens(tokens)
        self.log.info(" + Device registered")
        return tokens

    def _refresh_token(self, tokens: dict) -> bool:
        device_id, service_token = tokens.get("device_id"), tokens.get("service_token")
        if not device_id or not service_token:
            return False
        try:
            res = self.session.post(
                self._endpoint("transfer_playback"),
                json={"showNowPlaying": "false", "newMediaRequired": "true", "userHash": ""},
                headers=self._tvmesk_headers(device_id, **{"x-amzn-authentication": service_token}),
                timeout=self.registration_timeout,
            )
            if res.status_code != 200:
                self.log.warning(f" - Token refresh returned HTTP {res.status_code}")
                return False
            data = res.json()
            new_token = self._find_method(data, self._interface("authentication"), "authentication")
            if not new_token:
                return False
            video_token = self._find_method(data, self._interface("video_player"), "header")
            self._save_tokens(self._store_service_token(tokens, new_token, video_token))
            return True
        except Exception as e:
            self.log.warning(f" - Token refresh failed: {e}")
            return False

    def _muse(self, target: str, endpoint: str, payload: dict) -> Optional[dict]:
        res = self.session.post(
            self._endpoint("muse", operation=endpoint),
            json=payload,
            headers={
                "x-amzn-requestid": str(uuid.uuid4()),
                "X-Amz-Target": self._target("muse", operation=target),
            },
            timeout=self.timeout,
        )
        if res.status_code != 200:
            self.log.debug(f"muse/{endpoint} -> HTTP {res.status_code}: {res.text[:200]}")
            return None
        return res.json()

    def _lookup_album(self, asin: str) -> Optional[dict]:
        data = self._muse("lookup", "lookup", {
            "asins": [asin],
            "features": ["popularity", "expandTracklist", "trackLibraryAvailability",
                         "collectionLibraryAvailability"],
            "requestedContent": "MUSIC_SUBSCRIPTION",
            "musicTerritory": self.territory_id,
            "deviceId": self.device_id or "",
            "deviceType": self.device_type_id,
        })
        if not data:
            return None
        return (data.get("albumList") or [None])[0]

    def get_titles(self) -> Titles_T:
        asin = self._extract_asin(self.title)
        if not asin:
            self.log.error(" - Could not find an ASIN in that URL."); raise SystemExit(1)

        album = self._lookup_album(asin)
        if album and album.get("tracks") and not self.single:
            return self._album_titles(asin, album)

        return self._single_title(asin)

    def _album_titles(self, asin: str, album: dict) -> Music:
        album_title = self._clean(album.get("title") or album.get("name")) or asin
        album_artist = self._clean((album.get("artist") or {}).get("name")
                                   or album.get("artistName")) or "Unknown Artist"
        year = self._year(album)
        if not year:
            self.log.debug(f"No release year on album. Keys: {sorted(album)}")
        artwork = self._cover(album.get("image"))
        entries = [t for t in album.get("tracks") or [] if isinstance(t, dict) and t.get("asin")]

        songs = []
        for index, track in enumerate(entries, 1):
            songs.append(self._build_song(track, album, album_title, album_artist, year,
                                          artwork, index, len(entries)))
        if not songs:
            self.log.error(" - Album has no playable tracks."); raise SystemExit(1)

        return Music(songs, kind="album", title=album_title, artist=album_artist,
                     year=year or None, total_tracks=len(songs), artwork_url=artwork)

    def _single_title(self, asin: str) -> Music:
        data = self._muse("catalog", "catalog", {
            "asin": asin, "features": ["trackMetadata"], "musicTerritory": self.territory_id,
        })
        track = (data or {}).get("track")
        if not track:
            self.log.error(f" - Track {asin} not found in the {self.territory_id} catalogue.")
            raise SystemExit(1)

        album_title = self._clean((track.get("album") or {}).get("name")) or self._clean(track.get("title"))
        artist = self._clean((track.get("artist") or {}).get("name") or track.get("artistName")
                             or track.get("primaryArtistName")) or "Unknown Artist"
        year = self._year(track) or self._year(track.get("album") or {})
        artwork = self._cover(track.get("image") or (track.get("album") or {}).get("image"))

        song = self._build_song(track, track.get("album") or {}, album_title, artist, year, artwork, 1, 1)
        return Music([song], kind="single", title=album_title, artist=artist,
                     year=year or None, total_tracks=1, artwork_url=artwork)

    def _build_song(self, track: dict, album: dict, album_title: str, album_artist: str,
                    year: int, artwork: Optional[str], position: int, total: int) -> Song:
        title = self._clean(track.get("title") or track.get("name")) or "Unknown"
        artist = self._clean((track.get("artist") or {}).get("name")
                             or track.get("artistName")) or album_artist
        genre = self._clean(track.get("primaryGenre") or album.get("primaryGenre")) or None
        isrc = track.get("isrc") or None
        label = self._clean(track.get("label") or album.get("label")) or None
        track_number = int(track.get("trackNum") or position)
        disc_number = int(track.get("discNum") or track.get("discNumber") or 1)
        asin = track["asin"]
        year = self._year(track) or self._year(album) or year or 1

        data = {
            "service": self.ALIASES[0],
            "source": self.ALIASES[0],
            "track_id": asin,
            "track_url": f"{self.base_url}albums/{album.get('asin') or asin}",
            "title": title,
            "artist": artist,
            "performer": artist,
            "album": album_title,
            "album_artist": album_artist,
            "track_number": track_number,
            "total_tracks": total,
            "disc_number": disc_number,
            "genre": genre,
            "isrc": isrc,
            "label": label,
            "year": year,
            "copyright": self._clean(album.get("copyright")) or None,
            "artwork_url": artwork,
            "duration": int(track.get("duration") or 0) or None,
            "channels": 2,
        }
        if config.tag:
            data["comment"] = config.tag

        return Song(
            id_=asin,
            service=self.__class__,
            name=title,
            artist=artist,
            album=album_title,
            track=track_number,
            disc=disc_number,
            year=year,
            album_artist=album_artist,
            release_type="album" if total > 1 else "single",
            total_tracks=total if total > 1 else None,
            genre=genre,
            isrc=isrc if isinstance(isrc, str) else None,
            label=label,
            artwork_url=artwork,
            data=data,
        )

    def _get_mpd(self, asin: str) -> str:
        if asin in self._mpd_cache:
            return self._mpd_cache[asin]

        manifest_cfg = self.config.get("manifest") or {}
        res = self.session.post(
            self._endpoint("dmls"),
            json={
                "deviceToken": {"deviceTypeId": self.device_type_id, "deviceId": self.device_id or ""},
                "appInfo": {"musicAgent": self._music_agent(asin)},
                **({"customerId": self.customer_id} if self.customer_id else {}),
                "contentIdList": [{"identifier": asin, "identifierType": "ASIN"}],
                "musicDashVersionList": manifest_cfg.get("dash_versions") or [],
                "contentProtectionList": manifest_cfg.get("content_protection") or [],
                "customerInfo": {"marketplaceId": self.marketplace_id, "territoryId": self.territory_id},
                "try3dAsinSubstitution": True,
                "tryAsinSubstitution": True,
            },
            headers={
                "X-Amz-RequestId": str(uuid.uuid4()),
                "X-Amz-Target": self._target("manifest"),
                "x-amz-access-token": self.access_token,
                "x-amzn-timestamp": str(int(time.time() * 1000)),
                "x-amzn-requestid": str(uuid.uuid4()),
                "Content-Encoding": "amz-1.0",
            },
            timeout=self.timeout,
        )
        if res.status_code != 200:
            if res.status_code == 403:
                self.log.error(" - Manifest denied (HTTP 403). Check your subscription.")
            else:
                self.log.error(f" - Manifest request failed: HTTP {res.status_code} {res.text[:200]}")
            return ""

        try:
            data = res.json()
        except Exception as e:
            self.log.error(f" - Could not parse manifest response: {e}")
            return ""

        if data.get("sessionHandoffToken"):
            self.session_handoff_token = data["sessionHandoffToken"]
        mpd = ((data.get("contentResponseList") or [{}])[0]).get("manifest") or ""
        self._mpd_cache[asin] = mpd
        return mpd

    def _representations(self, mpd: str) -> list[dict]:
        reps = []
        for block in re.findall(r"<AdaptationSet\b[\s\S]*?</AdaptationSet>", mpd):
            kid = self._search(r'cenc:default_KID="([^"]+)"', block)
            psshs = self._content_protection(block)
            track_type = self._search(r'schemeIdUri="amz-music:trackType"\s+value="([^"]+)"', block) or "UNKNOWN"

            for rep in re.findall(r"<Representation\b[\s\S]*?</Representation>", block):
                base_url = (self._search(r"<BaseURL>([\s\S]*?)</BaseURL>", rep) or "").strip()
                if not base_url:
                    continue
                codec = self._search(r'codecs="([^"]+)"', rep) or "unknown"
                bandwidth = int(self._search(r'bandwidth="(\d+)"', rep) or 0)
                sample_rate = int(self._search(r'audioSamplingRate="(\d+)"', rep) or 0)
                bit_depth = int(self._search(r'schemeIdUri="amz-music:bitDepth"\s+value="(\d+)"', rep) or 0)
                reps.append({
                    "url": base_url,
                    "codec": codec,
                    "family": self._codec_family(codec),
                    "bandwidth": bandwidth,
                    "sample_rate": sample_rate,
                    "bit_depth": bit_depth,
                    "kid": (kid or "").replace("-", ""),
                    "pssh": psshs,
                    "track_type": track_type,
                })
        reps.sort(key=self._rep_rank, reverse=True)
        return reps

    def _rep_rank(self, rep: dict) -> tuple:
        try:
            codec_score = len(self.codec_priority) - self.codec_priority.index(rep["family"])
        except ValueError:
            codec_score = 0
        return (codec_score, rep["bit_depth"], rep["sample_rate"], rep["bandwidth"])

    @staticmethod
    def _codec_family(codec: str) -> str:
        codec = codec.lower()
        if "flac" in codec:
            return "flac"
        if codec.startswith("ec-3") or "eac3" in codec:
            return "ec-3"
        if codec.startswith("ac-4"):
            return "ac-4"
        if codec.startswith("mp4a.40.34") or codec == "mp3":
            return "mp3"
        if codec.startswith("mp4a"):
            return "mp4a"
        if "opus" in codec:
            return "opus"
        return codec

    _CODEC_ALIASES = {"aac": "mp4a", "ec3": "ec-3", "ac4": "ac-4",
                      "flac": "flac", "opus": "opus", "mp3": "mp3"}

    def _pick_representation(self, reps: list[dict]) -> Optional[dict]:
        if self.forced_codec:
            wanted = self._CODEC_ALIASES.get(self.forced_codec, self.forced_codec)
            matches = [r for r in reps if r["family"] == wanted]
            if not matches:
                available = ", ".join(sorted({r["family"] for r in reps})) or "none"
                self.log.error(f" - No {self.forced_codec.upper()} stream for this track. Available: {available}")
                return None
            return matches[0]
        return reps[0] if reps else None

    def get_music_track_options(self, song: Song) -> list[MusicTrackOption]:
        reps = self._representations(self._get_mpd(str(song.id)))
        options = []
        for rep in reps:
            family = rep["family"]
            options.append(MusicTrackOption(
                codec={"flac": "FLAC", "mp4a": "AAC", "ec-3": "EC3",
                       "ac-4": "AC4", "opus": "OPUS", "mp3": "MP3"}.get(family, family.upper()),
                bit_depth=rep["bit_depth"] or None,
                sample_rate=rep["sample_rate"] or None,
                bitrate=rep["bandwidth"] or None,
                channels=5.1 if family in ("ec-3", "ac-4") else 2.0,
                lossless=family == "flac",
                hires=family == "flac" and (rep["bit_depth"] > 16 or rep["sample_rate"] > 48000),
                duration=(song.data or {}).get("duration"),
                quality_label=self._quality_label(rep),
            ))
        return options

    def get_tracks(self, title: Song) -> Tracks:
        asin = str(title.id)
        mpd = self._get_mpd(asin)
        if not mpd:
            self.log.error(f" - No manifest for track {asin}."); raise SystemExit(1)

        reps = self._representations(mpd)
        if not reps:
            self.log.error(f" - No audio in the manifest for {asin}."); raise SystemExit(1)

        rep = self._pick_representation(reps)
        if not rep:
            raise SystemExit(1)

        self.quality = self._quality_label(rep)
        self.log.debug(f" + Selected {rep['codec']} @ {rep['bandwidth']}bps "
                       f"({rep['bit_depth'] or '?'}-bit/{rep['sample_rate'] or '?'}Hz)")

        drm = self._drm_for(rep)
        family = rep["family"]

        audio = Audio(
            rep["url"],
            language=title.language or "en",
            codec={"flac": Audio.Codec.FLAC, "mp4a": Audio.Codec.AAC, "ec-3": Audio.Codec.EC3,
                   "ac-4": Audio.Codec.AC4, "opus": Audio.Codec.OPUS}.get(family),
            bitrate=rep["bandwidth"] or None,
            channels=6 if family in ("ec-3", "ac-4") else 2,
            descriptor=Track.Descriptor.URL,
            id_=asin,
            drm=[drm] if drm else None,
            data={"ext": "flac" if family == "flac" else "m4a", "rep": rep},
        )
        return Tracks([audio])

    def get_chapters(self, title: Song) -> Chapters:
        return Chapters()

    def _content_protection(self, block: str) -> dict:
        system_ids = ((self.config.get("drm") or {}).get("system_ids") or {})
        widevine_id = (system_ids.get("widevine") or "").lower()
        playready_id = (system_ids.get("playready") or "").lower()

        found: dict[str, str] = {}
        for cp in re.findall(r"<ContentProtection\b[\s\S]*?(?:/>|</ContentProtection>)", block):
            scheme = (self._search(r'schemeIdUri="([^"]+)"', cp) or "").lower()
            pssh = self._search(r"<cenc:pssh[^>]*>([\s\S]*?)</cenc:pssh>", cp)
            if not pssh:
                continue
            if widevine_id and widevine_id in scheme:
                found["widevine"] = pssh.strip()
            elif (playready_id and playready_id in scheme) or "playready" in scheme:
                found["playready"] = pssh.strip()
        return found

    def _drm_for(self, rep: dict):
        psshs = rep.get("pssh") or {}
        system = "playready" if self.is_playready else "widevine"
        pssh_b64 = psshs.get(system)

        if pssh_b64:
            try:
                if self.is_playready:
                    return PlayReady(pssh=PlayReadyPSSH(pssh_b64), pssh_b64=pssh_b64)
                return Widevine(pssh=WidevinePSSH(pssh_b64))
            except Exception as e:
                self.log.debug(f"Manifest {system} PSSH unusable ({e}), building one from the KID")

        if not rep.get("kid"):
            self.log.error(" - Track is encrypted but the manifest exposed no KID or PSSH.")
            return None

        if self.is_playready:
            pro = self._build_playready_object(rep["kid"])
            return PlayReady(pssh=PlayReadyPSSH(pro), pssh_b64=base64.b64encode(pro).decode())
        return Widevine(pssh=WidevinePSSH.new(
            system_id=WidevinePSSH.SystemId.Widevine, key_ids=[rep["kid"]], version=1
        ))

    @staticmethod
    def _build_playready_object(kid_hex: str) -> bytes:
        kid = uuid.UUID(hex=kid_hex)
        kid_b64 = base64.b64encode(kid.bytes_le).decode()
        xml = (
            '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" '
            'version="4.0.0.0"><DATA><PROTECTINFO><KEYLEN>16</KEYLEN>'
            f"<ALGID>AESCTR</ALGID></PROTECTINFO><KID>{kid_b64}</KID></DATA></WRMHEADER>"
        )
        record = xml.encode("utf-16-le")
        body = struct.pack("<HH", 1, len(record)) + record
        return struct.pack("<IH", len(body) + 6, 1) + body

    DENIAL_HINTS = {
        "BLOCKLISTED_DEVICE": "Amazon has revoked this CDM's device certificate.",
    }

    def get_playready_license(self, *, challenge: Any, title: Song, track: Any = None,
                              **_) -> Optional[bytes]:
        return self._request_license(challenge, title, "PLAYREADY")

    def get_widevine_license(self, *, challenge: Any, title: Song, track: Any = None,
                             **_) -> Optional[bytes]:
        return self._request_license(challenge, title, "WIDEVINE")

    def _request_license(self, challenge: Any, title: Song, drm_type: str) -> Optional[bytes]:
        challenge_bytes = challenge if isinstance(challenge, bytes) else str(challenge).encode("utf-8")
        body = {
            "deviceToken": {"deviceTypeId": self.device_type_id, "deviceId": self.device_id or ""},
            "appInfo": {"musicAgent": self._music_agent(str(title.id))},
            "DrmType": drm_type,
            "licenseChallenge": base64.b64encode(challenge_bytes).decode(),
        }
        if self.customer_id:
            body["customerId"] = self.customer_id
        if self.session_handoff_token:
            body["sessionHandoffToken"] = self.session_handoff_token

        res = self.session.post(
            self._endpoint("dmls"),
            json=body,
            headers={
                "x-amzn-requestid": str(uuid.uuid4()),
                "X-Amz-Target": self._target("license"),
                "x-amz-access-token": self.access_token,
                "x-amzn-timestamp": str(int(time.time() * 1000)),
                "Content-Encoding": "amz-1.0",
            },
            timeout=self.timeout,
        )
        try:
            data = res.json()
        except Exception:
            data = {}

        denial = str(data.get("denialReason") or "")
        if denial or str(data.get("__type", "")).endswith("DrmLicenseDeniedException"):
            hint = self.DENIAL_HINTS.get(denial, "Check the subscription level and the CDM.")
            request_id = data.get("requestId") or "?"
            raise ValueError(f"{drm_type} licence denied by Amazon "
                             f"[{denial or 'no reason given'}]. {hint} (requestId {request_id})")

        if res.status_code != 200:
            raise ValueError(f"{drm_type} licence request failed: "
                             f"HTTP {res.status_code} {res.text[:300]}")
        if not data.get("license"):
            raise ValueError(f"No licence in {drm_type} response: {json.dumps(data)[:300]}")

        return base64.b64decode(data["license"])

    def on_track_downloaded(self, track: Any) -> None:
        if getattr(track, "drm", None):
            return
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

            if ext == "flac" and not self._remux(path, new_path):
                self.log.warning(" - Could not remux to FLAC.")
                return

            if not new_path.exists():
                path.rename(new_path)
            elif path.exists():
                path.unlink()
            track.path = new_path
        except Exception as e:
            self.log.debug(f"Container fix-up skipped: {e}")

    def _remux(self, src, dst) -> bool:
        if not binaries.FFMPEG:
            self.log.warning(" - ffmpeg not found, cannot remux.")
            return False
        proc = subprocess.run(
            [str(binaries.FFMPEG), "-nostdin", "-hide_banner", "-loglevel", "error",
             "-y", "-i", str(src), "-map", "0:a:0", "-c:a", "copy", str(dst)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size <= 3:
            self.log.debug(f"ffmpeg remux failed ({proc.returncode}): {proc.stderr[:300]}")
            if dst.exists():
                dst.unlink()
            return False
        return True

    @staticmethod
    def _search(pattern: str, text: str) -> Optional[str]:
        match = re.search(pattern, text)
        return match.group(1) if match else None

    @staticmethod
    def _extract_asin(url: str) -> Optional[str]:
        match = re.search(r"/(?:albums|tracks)/([A-Z0-9]{10,})", url, re.I)
        if match:
            return match.group(1).upper()
        return url.upper() if re.fullmatch(r"[A-Z0-9]{10,}", url, re.I) else None

    @staticmethod
    def _clean(value: Any) -> str:
        if not value:
            return ""
        return _INVISIBLE.sub("", str(value)).strip()

    @staticmethod
    def _year(obj: dict) -> int:
        for key in ("releaseYear", "originalReleaseDate", "releaseDate",
                    "albumReleaseDate", "publishDate"):
            value = obj.get(key)
            if not value:
                continue
            if isinstance(value, (int, float)):
                value = int(value)
                if 1000 <= value <= 2999:
                    return value
                try:
                    return int(time.gmtime(value / 1000 if value > 1e11 else value).tm_year)
                except Exception:
                    continue
            match = re.search(r"(\d{4})", str(value))
            if match:
                return int(match.group(1))
        return 0

    @staticmethod
    def _cover(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        return re.sub(r"\._[A-Z0-9_,]+_\.", ".", url)

    @staticmethod
    def _quality_label(rep: dict) -> str:
        family = rep["family"]
        if family == "flac":
            bits, rate = rep["bit_depth"] or 16, (rep["sample_rate"] or 44100) / 1000
            return f"FLAC {bits}-bit/{rate:g} kHz"
        name = {"mp4a": "AAC", "ec-3": "EC-3", "ac-4": "AC-4", "opus": "Opus", "mp3": "MP3"}.get(
            family, family.upper())
        kbps = (rep["bandwidth"] or 0) // 1000
        return f"{name} {kbps} kb/s" if kbps else name
