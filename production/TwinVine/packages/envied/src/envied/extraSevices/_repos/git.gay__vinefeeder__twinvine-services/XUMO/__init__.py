from __future__ import annotations
import re
from binascii import crc32
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional
import click
from langcodes import Language
from envied.core.credential import Credential
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapters, Subtitle, Tracks


class XUMO(Service):
    """
    Service code for XUMO Play (https://play.xumo.com)
    www.nostalgic.cc
    Authorization: None
    Geofence: US
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?play\.xumo\.com/[^/]+/[^/]+/)?(?P<id>XM0[A-Z0-9]{11})"
    GEOFENCE = ("US",)

    ASSET_FIELDS = [
        "title",
        "providers",
        "captions",
        "descriptions",
        "runtime",
        "originalReleaseYear",
        "contentType",
        "availableSince",
        "season:all",
        "episodes.episodeTitle",
        "episodes.runtime",
        "episodes.descriptions",
    ]

    @staticmethod
    @click.command(name="XUMO", short_help="https://play.xumo.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return XUMO(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        m = re.match(self.TITLE_RE, title)
        if not m:
            raise ValueError(f"Could not parse XUMO URL or ID: {title!r}")
        self.asset_id = m.group("id")

    def authenticate(
        self,
        cookies: Optional[CookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        self.session.headers.update({
            "User-Agent": self.config["client"]["web"]["user_agent"],
            "Origin": "https://play.xumo.com",
            "Referer": "https://play.xumo.com/",
        })

    def search(self) -> Generator:
        return
        yield

    def get_titles(self) -> Titles_T:
        asset = self._fetch_asset(self.asset_id)
        content_type = asset.get("contentType", "MOVIE")

        if content_type in ("SHOW", "SERIES"):
            return self._titles_from_show(asset)
        elif content_type in ("EPISODE", "EPISODIC"):
            return self._titles_from_episode(asset)
        else:
            return self._titles_from_movie(asset)

    def _titles_from_movie(self, asset: dict) -> Movies:
        name = self._get_title(asset)
        year = asset.get("originalReleaseYear") or self._parse_year(asset.get("availableSince"))
        desc = self._get_description(asset)

        return Movies([Movie(
            id_=asset["id"],
            service=self.__class__,
            name=name,
            year=year,
            description=desc,
            language=Language.get("en"),
            data={"asset": asset},
        )])

    def _titles_from_episode(self, asset: dict) -> Series:
        show_title = self._get_title(asset)
        ep_name = asset.get("episodeTitle") or show_title
        season_num = int(asset.get("seasonNumber") or 0)
        ep_num = int(asset.get("episodeNumber") or 0)
        year = asset.get("originalReleaseYear") or self._parse_year(asset.get("availableSince"))
        desc = self._get_description(asset)

        return Series([Episode(
            id_=asset["id"],
            service=self.__class__,
            title=show_title,
            season=season_num,
            number=ep_num,
            name=ep_name,
            description=desc,
            year=year,
            language=Language.get("en"),
            data={"asset": asset},
        )])

    def _titles_from_show(self, asset: dict) -> Series:
        show_title = self._get_title(asset)
        episodes = []

        seasons = asset.get("season") or asset.get("seasons") or []
        for season in seasons:
            season_num = int(season.get("seasonNumber") or 0)
            for ep in season.get("episodes") or []:
                ep_id = ep.get("id")
                if not ep_id:
                    continue
                ep_num = int(ep.get("episodeNumber") or 0)
                ep_title = self._get_title(ep) or show_title
                year = ep.get("originalReleaseYear") or self._parse_year(ep.get("availableSince"))
                desc = self._get_description(ep)

                episodes.append(Episode(
                    id_=ep_id,
                    service=self.__class__,
                    title=show_title,
                    season=season_num,
                    number=ep_num,
                    name=ep_title,
                    description=desc,
                    year=year,
                    language=Language.get("en"),
                    data={},
                ))

        if not episodes:
            raise ValueError(f"No episodes found for '{show_title}' ({self.asset_id}).")

        return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        asset = title.data.get("asset") or self._fetch_asset(title.id)
        m3u8_url = self._get_stream_url(asset)
        self.log.debug(f"HLS master: {m3u8_url}")

        tracks = HLS.from_url(url=m3u8_url, session=self.session).to_tracks(language=title.language)

        for sub in self._build_subtitle_tracks(asset, title.language):
            tracks.add(sub)

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> None:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: Any) -> None:
        return None

    def _fetch_asset(self, asset_id: str) -> dict:
        url = self.config["endpoints"]["asset"].format(asset_id=asset_id)
        r = self.session.get(url, params=[("f", f) for f in self.ASSET_FIELDS])
        r.raise_for_status()
        return r.json()

    def _get_stream_url(self, asset: dict) -> str:
        providers = asset.get("providers", [])
        if not providers:
            raise ValueError(f"No providers found for '{asset.get('id')}'.")

        sources = providers[0].get("sources", [])

        for source in sources:
            if source.get("produces") == "application/x-mpegURL":
                return source["uri"]

        if sources:
            uri = sources[0].get("uri")
            if uri:
                return uri

        raise ValueError(f"No stream found for '{asset.get('id')}'.")

    def _build_subtitle_tracks(self, asset: dict, language: Language) -> list[Subtitle]:
        providers = asset.get("providers", [])
        if not providers:
            self.log.warning("Skipping subtitle lookup.")
            return []

        captions = providers[0].get("captions") or asset.get("captions") or []
        self.log.debug(f"Found {len(captions)} caption entry/entries.")

        CODEC_MAP = {
            "text/vtt":              Subtitle.Codec.WebVTT,
            "application/ttml+xml":  Subtitle.Codec.TimedTextMarkupLang,
            "text/srt":              Subtitle.Codec.SubRip,
        }
        FORMAT_PRIORITY = {"text/vtt": 0, "application/ttml+xml": 1, "text/srt": 2}

        best: dict[str, dict] = {}
        for cap in captions:
            lang = cap.get("language") or "und"
            key = lang if lang != "und" else f"und:{cap.get('type', '')}"
            mime = cap.get("type", "")
            priority = FORMAT_PRIORITY.get(mime, 99)
            if key not in best or priority < FORMAT_PRIORITY.get(best[key].get("type", ""), 99):
                best[key] = cap

        tracks = []
        for key, cap in best.items():
            uri = cap.get("uri") or cap.get("url") or cap.get("src")
            if not uri:
                continue
            mime = cap.get("type", "text/vtt")
            codec = CODEC_MAP.get(mime, Subtitle.Codec.WebVTT)
            raw_lang = cap.get("language") or ""
            try:
                lang_obj = Language.get(raw_lang) if raw_lang and raw_lang != "und" else language
            except Exception:
                lang_obj = language
            tracks.append(Subtitle(
                id_=hex(crc32(uri.encode()))[2:],
                url=uri,
                codec=codec,
                language=lang_obj,
            ))

        return tracks

    @staticmethod
    def _get_title(obj: dict) -> str:
        for key in ("title", "episodeTitle"):
            val = obj.get(key)
            if val is None:
                continue
            if isinstance(val, dict):
                return val.get("en") or next(iter(val.values()), "") or ""
            return str(val)
        return ""

    @staticmethod
    def _get_description(obj: dict) -> Optional[str]:
        descs = obj.get("descriptions")
        if not isinstance(descs, dict):
            return None
        for size in ("tiny", "short", "long"):
            d = descs.get(size)
            if not d:
                continue
            if isinstance(d, dict):
                return d.get("en") or next(iter(d.values()), None)
            return str(d)
        return None

    @staticmethod
    def _parse_year(date_str: Optional[str]) -> Optional[int]:
        if not date_str:
            return None
        try:
            return int(date_str[:4])
        except (ValueError, TypeError):
            return None
