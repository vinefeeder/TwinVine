from __future__ import annotations
import base64
import hashlib
import re
import secrets
from http.cookiejar import CookieJar
from typing import Any, Optional
import click
from click.core import ParameterSource
from langcodes import Language
from lxml import etree
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Subtitle, Tracks, Video


class HULU(Service):
    """
    Service code for Hulu (https://hulu.com)
    www.nostalgic.cc
    Authorization: Cookies
    Geofence: US
    """

    ALIASES = ("HULU", "hulu")
    GEOFENCE = ("US",)
    TITLE_RE = (
        r"^(?:https?://(?:www\.)?hulu\.com/(?P<type>movie|series)/)?"
        r"(?:[a-z0-9-]+-)?(?P<id>[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})"
    )
    HULU_RANGE_MAP = {
        "SDR": "SDR",
        "HLG": "DOLBY_VISION",
        "HDR10": "DOLBY_VISION",
        "HDR10P": "DOLBY_VISION",
        "DV": "DOLBY_VISION",
    }

    @staticmethod
    @click.command(name="HULU", short_help="hulu.com")
    @click.argument("title", type=str)
    @click.option(
        "-mt", "--mpd-type",
        type=click.Choice(["new", "old"], case_sensitive=False),
        default="new",
        help="Device profile to request (Old=166, New=210).",
    )
    @click.option("-m", "--movie", is_flag=True, default=False,
                  help="Force the title to be treated as a movie.")
    @click.option("-sh", "--show", "force_series", is_flag=True, default=False,
                  help="Force the title to be treated as a show.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return HULU(ctx, **kwargs)

    def __init__(self, ctx, title: str, mpd_type: str, movie: bool, force_series: bool):
        self.title = title
        self.mpd_type = mpd_type
        self.movie = movie
        self.force_series = force_series
        self.vcodec = ctx.parent.params.get("vcodec") or []
        self.acodec = ctx.parent.params.get("acodec") or []
        range_ = ctx.parent.params.get("range_")
        self.range = range_[0].name if range_ else "SDR"
        self.range_source = ctx.get_parameter_source("range_")
        self.license_url_widevine: Optional[str] = None
        self.license_url_playready: Optional[str] = None
        self._playlist: Optional[dict] = None
        self.device_identifier = secrets.token_hex(16).upper()

        try:
            from pyplayready.cdm import Cdm as PlayReadyCdm
            self.use_playready: bool = isinstance(ctx.obj.cdm, PlayReadyCdm)
        except ImportError:
            self.use_playready = False

        super().__init__(ctx)

    def get_titles(self) -> Titles_T:
        m = re.search(self.TITLE_RE, self.title)
        if not m:
            raise ValueError(f"Could not parse title ID from: {self.title!r}")

        title_id = m.group("id")
        detected_type = m.group("type")
        if self.movie:
            return self._get_movie(title_id)
        if self.force_series:
            return self._get_series(title_id, allow_movie_fallback=False)
        if detected_type == "movie":
            return self._get_movie(title_id)
        return self._get_series(title_id, allow_movie_fallback=True)

    def _get_movie(self, title_id: str) -> Movies:
        resp = self.session.get(self.config["endpoints"]["movie"].format(id=title_id))
        try:
            resp.raise_for_status()
        except Exception as e:
            info = self._safe_json(resp)
            raise ValueError(f"Failed to get movie {title_id}: {info.get('message', e)}")

        title_data = resp.json()["details"]["vod_items"]["focus"]["entity"]
        return Movies([Movie(
            id_=title_id,
            service=self.__class__,
            name=title_data.get("name"),
            year=int(title_data["premiere_date"][:4]) if title_data.get("premiere_date") else None,
            language="en",
            data=title_data,
        )])

    def _get_series(self, title_id: str, allow_movie_fallback: bool = True) -> Series:
        resp = self.session.get(self.config["endpoints"]["series"].format(id=title_id))
        try:
            resp.raise_for_status()
        except Exception as e:
            info = self._safe_json(resp)
            if allow_movie_fallback and resp.status_code == 400 and "entity type" in info.get("message", "").lower():
                self.log.info("Detected movie UUID. Retrying movie endpoint.")
                return self._get_movie(title_id)
            raise ValueError(
                f"Failed to get series {title_id}: {info.get('message', e)} [{info.get('code')}]"
            )

        res = resp.json()
        season_data = next(
            (x for x in res.get("components", []) if x.get("name") == "Episodes"),
            None,
        )
        if not season_data:
            raise ValueError("Could not find episode list.")

        series = Series()
        for season in season_data.get("items", []):
            embedded = season.get("items")
            if embedded and any(ep.get("_type") == "episode" for ep in embedded):
                for episode in embedded:
                    self._add_episode(series, season, episode)
                continue

            season_id_part = season.get("id", "").rsplit("::", 1)[-1]
            season_resp = self.session.get(
                self.config["endpoints"]["season"].format(id=title_id, season=season_id_part)
            )
            try:
                season_resp.raise_for_status()
            except Exception as e:
                info = self._safe_json(season_resp)
                raise ValueError(f"Failed to get season {season_id_part}: {info.get('message', e)}")

            for episode in season_resp.json().get("items", []):
                self._add_episode(series, season, episode)

        return series

    def _add_episode(self, series: Series, season: dict, episode: dict) -> None:
        try:
            ep_season = int(episode["season"]) if episode.get("season") is not None else None
            ep_number = int(episode["number"]) if episode.get("number") is not None else None
        except (ValueError, TypeError):
            ep_season = episode.get("season")
            ep_number = episode.get("number")

        series.add(Episode(
            id_=f"{season.get('id')}::{episode.get('season')}::{episode.get('number')}",
            service=self.__class__,
            title=episode.get("series_name"),
            season=ep_season,
            number=ep_number,
            name=episode.get("name"),
            language="en",
            data=episode,
        ))

    def _dynamic_range(self) -> str:
        if self.range_source != ParameterSource.COMMANDLINE:
            return "DOLBY_VISION"
        return self.HULU_RANGE_MAP.get(self.range, "SDR")

    def _codec_preference(self) -> list:
        if Video.Codec.HEVC in self.vcodec:
            return ["H265"]
        if Video.Codec.AVC in self.vcodec:
            return ["H264"]
        return ["H265", "H264"]

    def _request_playlist(self, eab_id: str, codec: str, dynamic_range: str) -> dict:
        deejay_id = (
            self.config["device_ids"]["new"] if self.mpd_type == "new"
            else self.config["device_ids"]["old"]
        )
        version = 1 if self.mpd_type == "new" else 9999999

        resp = None
        try:
            resp = self.session.post(
                url=self.config["endpoints"]["manifest"],
                json={
                    "device_identifier": self.device_identifier,
                    "guid": self.device_identifier,
                    "deejay_device_id": deejay_id,
                    "version": version,
                    "all_cdn": False,
                    "content_eab_id": eab_id,
                    "region": "US",
                    "language": "en",
                    "unencrypted": True,
                    "network_mode": "wifi",
                    "play_intent": "resume",
                    "playback": {
                        "version": 2,
                        "video": {
                            "dynamic_range": dynamic_range,
                            "codecs": {
                                "values": [x for x in self.config["codecs"]["video"] if x["type"] == codec],
                                "selection_mode": self.config["codecs"]["video_selection"],
                            },
                        },
                        "audio": {
                            "codecs": {
                                "values": self.config["codecs"]["audio"],
                                "selection_mode": self.config["codecs"]["audio_selection"],
                            },
                        },
                        "drm": {
                            "multi_key": True,
                            "values": (
                                self.config["drm"]["schemas_pr"]
                                if self.use_playready
                                else self.config["drm"]["schemas_wv"]
                            ),
                            "selection_mode": self.config["drm"]["selection_mode"],
                            "hdcp": self.config["drm"]["hdcp"],
                        },
                        "manifest": {
                            "type": "DASH",
                            "https": True,
                            "multiple_cdns": False,
                            "patch_updates": True,
                            "hulu_types": True,
                            "live_dai": True,
                            "secondary_audio": True,
                            "live_fragment_delay": 3,
                            "forced_narratives": True,
                            "full_language_locales": True,
                        },
                        "segments": {
                            "values": [{
                                "type": "FMP4",
                                "encryption": {"mode": "CENC", "type": "CENC"},
                                "https": True,
                            }],
                            "selection_mode": "ONE",
                        },
                    },
                },
            )
            resp.raise_for_status()
            playlist = resp.json()
        except Exception as e:
            info = self._safe_json(resp) if resp is not None else {}
            raise ValueError(f"{info.get('message', e)} ({info.get('code', '')})")

        if "stream_url" not in playlist:
            raise ValueError(f"Manifest response missing 'stream_url' (Keys: {list(playlist.keys())})")
        return playlist

    def get_tracks(self, title: Title_T) -> Tracks:
        eab_id = (title.data.get("bundle") or {}).get("eab_id")
        if not eab_id:
            raise ValueError(f"Could not find eab_id in title data for '{title}'.")

        codec_chain = self._codec_preference()
        tracks: Optional[Tracks] = None
        last_reason = ""

        for codec in codec_chain:
            dynamic_range = self._dynamic_range() if codec == "H265" else "SDR"
            if len(codec_chain) > 1:
                self.log.info(f"Requesting {codec} manifest ({dynamic_range})...")

            try:
                playlist = self._request_playlist(eab_id, codec, dynamic_range)
            except ValueError as e:
                last_reason = str(e)
                self.log.warning(f" - {codec} manifest unavailable: {e}")
                continue

            self._playlist = playlist
            self.license_url_widevine = playlist.get("wv_server")
            self.license_url_playready = playlist.get("dash_pr_server")

            lang = playlist.get("video_metadata", {}).get("language") or "en"
            title.language = Language.get(lang)

            manifest_url = playlist["stream_url"]
            self.log.info(f"DASH: {manifest_url}")

            try:
                mpd_resp = self.session.get(manifest_url)
                mpd_resp.raise_for_status()
                mpd_text = mpd_resp.text
            except Exception as e:
                last_reason = f"- Failed to fetch {codec} Manifest: {e}"
                self.log.warning(f" - {last_reason}; Trying next codec.")
                continue

            if "disney" in manifest_url:
                mpd_text = self._normalize_ad_markers(mpd_text)
                mpd_text = self._strip_duplicate_representations(mpd_text)

            parsed = self._parse_dash(mpd_text, manifest_url, title.language)
            if parsed.videos:
                if codec != codec_chain[0]:
                    self.log.info(f"{codec_chain[0]} not available for this title. Using {codec}.")
                tracks = parsed
                break
            last_reason = f"no {codec} video tracks in manifest"
            self.log.warning(f" - {last_reason}; trying next codec.")

        if tracks is None:
            raise ValueError(
                f"Could not get a usable video manifest for '{title}' "
                f"(tried {', '.join(codec_chain)}). {last_reason}"
            )

        if self.acodec:
            tracks.audio = [x for x in tracks.audio if x.codec in self.acodec]

        for track in tracks.audio:
            if track.bitrate and track.bitrate > 768_000:
                track.bitrate = 768_000
            if track.channels == 6.0:
                track.channels = 5.1

        for sub_lang, sub_url in playlist.get("transcripts_urls", {}).get("webvtt", {}).items():
            tracks.add(Subtitle(
                id_=hashlib.md5(sub_url.encode()).hexdigest()[:6],
                url=sub_url,
                codec=Subtitle.Codec.from_mime("vtt"),
                language=sub_lang,
                forced=False,
                sdh=False,
            ))

        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        if not self._playlist:
            return []

        try:
            meta = self._playlist.get("video_metadata", {})
            segments_raw = meta.get("segments")
            end_credits_raw = meta.get("end_credits_time")
            frame_rate = int(meta.get("frame_rate") or 24) or 24

            if not segments_raw and not end_credits_raw:
                return []

            def smpte_to_timestamp(tc: str) -> str:
                tc = tc.strip()
                if ";" in tc:
                    time_part, frames = tc.rsplit(";", 1)
                    ms = int(round(int(frames) * 1000 / frame_rate))
                elif "." in tc:
                    time_part, frac = tc.rsplit(".", 1)
                    ms = int(frac[:3].ljust(3, "0"))
                else:
                    time_part = tc
                    ms = 0

                if ms >= 1000:
                    h, m, s = (int(x) for x in time_part.split(":"))
                    total_s = h * 3600 + m * 60 + s + ms // 1000
                    ms = ms % 1000
                    h, rem = divmod(total_s, 3600)
                    m, s = divmod(rem, 60)
                    time_part = f"{h:02d}:{m:02d}:{s:02d}"

                return f"{time_part}.{ms:03d}"

            timestamps = ["00:00:00.000"]

            if segments_raw:
                for seg in segments_raw.split(","):
                    seg = seg.strip().lstrip("T:")
                    if seg:
                        timestamps.append(smpte_to_timestamp(seg))

            if end_credits_raw:
                ts = smpte_to_timestamp(end_credits_raw)
                if ts != "00:00:00.000":
                    timestamps.append(ts)

            timestamps = sorted(set(timestamps))
            return [Chapter(timestamp=ts) for ts in timestamps]
        except Exception as e:
            self.log.warning(f"Failed to parse chapters: {e}")
            return []

    def get_widevine_service_certificate(self, **_: Any) -> None:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.license_url_widevine:
            raise ValueError("Widevine license URL not set.")
        resp = self.session.post(
            url=self.license_url_widevine,
            data=challenge,
            headers={"Content-Type": "application/octet-stream"},
        )
        resp.raise_for_status()

        ct = resp.headers.get("Content-Type", "")
        if "json" in ct or resp.content[:1] == b"{":
            try:
                data = resp.json()
                for key in ("license", "license_data", "licenseData"):
                    if key in data:
                        return base64.b64decode(data[key])
            except Exception:
                pass

        return resp.content

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.license_url_playready:
            raise ValueError("PlayReady license URL not set.")
        resp = self.session.post(
            url=self.license_url_playready,
            data=challenge,
        )
        if not resp.ok:
            self.log.error(f"PlayReady license error ({resp.status_code}): {resp.text[:500]}")
        resp.raise_for_status()
        return resp.content

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        if not cookies:
            raise EnvironmentError("Hulu requires cookies for authentication.")
        self.session.cookies.update(cookies)
        self.session.headers.update({
            "User-Agent": self.config.get(
                "user_agent",
                "Mozilla/5.0 (Linux; Android 9; AFTMM Build/PS7233; wv) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Version/4.0 Chrome/118.0.0.0 Mobile Safari/537.36",
            ),
            "Origin": "https://www.hulu.com",
            "Referer": "https://www.hulu.com/",
        })

    def _parse_dash(self, mpd_text: str, manifest_url: str, language) -> Tracks:
        try:
            return DASH.from_text(mpd_text, manifest_url).to_tracks(language)
        except ValueError as e:
            if "duplicate" not in str(e).lower():
                raise

        self.log.warning(
            "Duplicate track IDs detected in manifest "
            "Retrying with deduplication."
        )

        from envied.core.tracks import Tracks as _Tracks
        _orig_add = _Tracks.add

        def _warn_only_add(self_t, tracks_in, warn_only: bool = False):
            return _orig_add(self_t, tracks_in, warn_only=True)

        _Tracks.add = _warn_only_add
        try:
            return DASH.from_text(mpd_text, manifest_url).to_tracks(language)
        finally:
            _Tracks.add = _orig_add

    @staticmethod
    def _safe_json(resp) -> dict:
        try:
            return resp.json()
        except Exception:
            return {}

    @staticmethod
    def _normalize_ad_markers(mpd: str) -> str:
        DASH_NS = "urn:mpeg:dash:schema:mpd:2011"
        TVA_DESC = ("urn:tva:metadata:cs:AudioPurposeCS:2007", "1")
        DASH_DESC = ("urn:mpeg:dash:role:2011", "descriptive")

        root = etree.fromstring(mpd.encode("utf-8"))

        for adaptation_set in root.iter(f"{{{DASH_NS}}}AdaptationSet"):
            if "audio" not in adaptation_set.get("mimeType", ""):
                continue

            has_desc_role = any(
                role.get("schemeIdUri") == "urn:mpeg:dash:role:2011"
                and role.get("value") == "description"
                for role in adaptation_set.findall(f"{{{DASH_NS}}}Role")
            )
            if not has_desc_role:
                continue

            already_marked = any(
                (acc.get("schemeIdUri"), acc.get("value")) in (TVA_DESC, DASH_DESC)
                for acc in adaptation_set.findall(f"{{{DASH_NS}}}Accessibility")
            )
            if already_marked:
                continue

            acc = etree.SubElement(adaptation_set, f"{{{DASH_NS}}}Accessibility")
            acc.set("schemeIdUri", TVA_DESC[0])
            acc.set("value", TVA_DESC[1])

        return etree.tostring(root, encoding="unicode")

    @staticmethod
    def _strip_duplicate_representations(mpd: str) -> str:
        def _dedup_block(match: re.Match) -> str:
            seen: set[str] = set()

            def _dedupe(m: re.Match) -> str:
                rid = m.group(1)
                if rid in seen:
                    return ""
                seen.add(rid)
                return m.group(0)

            return re.sub(
                r'<Representation[^>]*\bid="([^"]+)"[^>]*>.*?</Representation>',
                _dedupe,
                match.group(0),
                flags=re.DOTALL,
            )

        return re.sub(
            r'<AdaptationSet[^>]*>.*?</AdaptationSet>',
            _dedup_block,
            mpd,
            flags=re.DOTALL,
        )
