from __future__ import annotations
import json
import re
import uuid
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional, Union
import click
from langcodes import Language
from envied.core.credential import Credential
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapters, Tracks


class PBS(Service):
    """
    Service code for PBS (https://www.pbs.org)

    Author: n0stal6ic
    Authorization: Cookies
    Geofence: US
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?pbs\.org/(?P<type>video|show)/)?(?P<id>[a-zA-Z0-9-]+)"
    GEOFENCE = ("US",)

    @staticmethod
    @click.command(name="PBS", short_help="https://www.pbs.org", help=__doc__)
    @click.argument("title", type=str)
    @click.option(
        "-a", "--all", "all_",
        is_flag=True,
        default=False,
        help="Parse input as slug to download all content.",
    )
    @click.pass_context
    def cli(ctx, **kwargs):
        return PBS(ctx, **kwargs)

    def __init__(self, ctx, title: str, all_: bool = False):
        super().__init__(ctx)
        self.title = title
        self.is_show = all_

        m = re.match(self.TITLE_RE, title)
        if not m:
            raise ValueError(f"Could not parse PBS URL or ID: {title!r}.")

        url_type = m.group("type")
        self.title_id = m.group("id")

        if url_type == "show":
            self.is_show = True

        self.uid: str = str(uuid.uuid4())
        self.passport: str = "no"
        self.callsign: Optional[str] = None
        self.station_id: Optional[str] = None

    def authenticate(
        self,
        cookies: Optional[CookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if cookies:
            uid = next((c.value for c in cookies if c.name == "pbs_uid"), None)
            if uid:
                self.uid = uid
                self.passport = "yes"
            self.callsign = next((c.value for c in cookies if c.name == "pbsol.station"), None)
            self.station_id = next((c.value for c in cookies if c.name == "pbsol.station_id"), None)

        self.session.headers.update({"User-Agent": self.config["client"]["web"]["user_agent"]})

    def search(self) -> Generator[SearchResult, None, None]:
        return
        yield

    def get_titles(self) -> Titles_T:
        if self.is_show:
            return self._get_show_titles(self.title_id)
        return self._get_video_title(self.title_id)

    def get_tracks(self, title: Title_T) -> Tracks:
        video_bridge = title.data.get("video_bridge") or self._fetch_video_bridge(title.id)

        if video_bridge.get("availability") != "available":
            raise ValueError(
                f"Video '{title.id}' is unavailable. (Status: {video_bridge.get('availability')!r}). "
                "Passport content requires cookies from a logged-in PBS account."
            )

        encodings = video_bridge.get("encodings", [])
        if not encodings:
            raise ValueError(f"No streams found for '{title.id}'.")

        m3u8_url = self._resolve_encoding(encodings[0])
        self.log.debug(f"HLS master: {m3u8_url}")

        return HLS.from_url(url=m3u8_url, session=self.session).to_tracks(language=title.language)

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> None:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: Any) -> None:
        return None

    def _get_video_title(self, video_slug: str) -> Titles_T:
        video_bridge = self._fetch_video_bridge(video_slug)
        show_slug = video_bridge.get("program", {}).get("slug")
        show_title = video_bridge.get("program", {}).get("title", "Unknown Show")

        episode_data = self._find_episode_in_show(show_slug, video_slug) if show_slug else None

        if episode_data:
            parent = episode_data.get("parent", {})
            season_data = parent.get("season", {})
            show_display = season_data.get("show", {}).get("title", show_title)
            season_num = int(season_data.get("ordinal") or 0)
            ep_num = int(parent.get("ordinal") or 0)
            year = self._parse_year(episode_data.get("premiere_date"))

            return Series([
                Episode(
                    id_=video_slug,
                    service=self.__class__,
                    title=show_display,
                    season=season_num,
                    number=ep_num,
                    name=episode_data.get("title", video_slug),
                    description=episode_data.get("description_short"),
                    year=year,
                    language=Language.get("en"),
                    data={"video_bridge": video_bridge, "episode": episode_data},
                )
            ])

        self.log.warning("Unable to find episode metadata.")
        fallback_name = video_bridge.get("_page_title") or video_slug.replace("-", " ").title()
        return Series([
            Episode(
                id_=video_slug,
                service=self.__class__,
                title=show_title,
                season=0,
                number=0,
                name=fallback_name,
                language=Language.get("en"),
                data={"video_bridge": video_bridge},
            )
        ])

    def _get_show_titles(self, show_slug: str) -> Titles_T:
        self.log.info(f"Fetching season data for {show_slug}.")
        seasons = self._fetch_show_seasons(show_slug)
        specials = self._fetch_show_specials(show_slug)
        show_display = show_slug.replace("-", " ").title()
        episodes = []

        if seasons:
            self.log.info(f"Found {len(seasons)} season(s)")
            for season_cid, season_ordinal in seasons:
                self.log.info(f"Season {season_ordinal}")
                for ep in self._fetch_season_episodes(show_slug, season_cid):
                    parent = ep.get("parent", {})
                    season_data = parent.get("season", {})
                    show_display = season_data.get("show", {}).get("title") or show_display
                    season_num = int(season_data.get("ordinal") or season_ordinal)
                    ep_num = int(parent.get("ordinal") or 0)
                    year = self._parse_year(ep.get("premiere_date"))

                    episodes.append(Episode(
                        id_=ep["slug"],
                        service=self.__class__,
                        title=show_display,
                        season=season_num,
                        number=ep_num,
                        name=ep.get("title", ep["slug"]),
                        description=ep.get("description_short"),
                        year=year,
                        language=Language.get("en"),
                        data={"episode": ep},
                    ))

        if specials:
            self.log.info(f"Found {len(specials)} special(s)")
            specials.sort(key=lambda x: x.get("premiere_date") or "")
            for i, sp in enumerate(specials, start=1):
                self.log.info(f"Special {i}")
                parent = sp.get("parent", {})
                show_display = parent.get("show", {}).get("title") or show_display
                year = self._parse_year(sp.get("premiere_date"))

                episodes.append(Episode(
                    id_=sp["slug"],
                    service=self.__class__,
                    title=show_display,
                    season=0,
                    number=i,
                    name=sp.get("title", sp["slug"]),
                    description=sp.get("description_short"),
                    year=year,
                    language=Language.get("en"),
                    data={"episode": sp},
                ))

        if not episodes:
            raise ValueError(f"No episodes or specials found for {show_slug!r}.")

        return Series(episodes)

    def _fetch_video_bridge(self, video_slug: str) -> dict:
        params: dict[str, str] = {
            "uid": self.uid,
            "userPassportStatus": self.passport,
            "autoplay": "true",
            "unsafeDisableUpsellHref": "true",
        }
        if self.callsign:
            params["callsign"] = self.callsign
        if self.station_id:
            params["station_id"] = self.station_id

        r = self.session.get(
            self.config["endpoints"]["portalplayer"] + video_slug + "/",
            params=params,
        )
        r.raise_for_status()

        idx = r.text.find("window.videoBridge = ")
        if idx == -1:
            raise ValueError(
                f"videoBridge not found in portalplayer response for {video_slug!r}.\n"
                "Double check your input URL and make sure it is correct."
            )

        json_start = r.text.index("{", idx)
        video_bridge, _ = json.JSONDecoder().raw_decode(r.text, json_start)
        title_m = re.search(r"<title>\s*Video:\s*(.*?)\s*\|\s*Watch", r.text, re.DOTALL)
        if title_m:
            video_bridge["_page_title"] = " ".join(title_m.group(1).split())

        return video_bridge

    def _resolve_encoding(self, encoding_url: str) -> str:
        r = self.session.get(encoding_url, params={"format": "jsonp", "callback": "__jp0"})
        r.raise_for_status()

        m = re.search(r"__jp0\((.+)\)\s*$", r.text.strip())
        if not m:
            raise ValueError(f"Unable to parse URS JSONP response: {r.text[:200]!r}")

        data = json.loads(m.group(1))
        url = data.get("url")
        if not url:
            raise ValueError(f"URS redirect returned no URL: {data}")
        return url

    def _fetch_show_seasons(self, show_slug: str) -> list[tuple[str, int]]:
        r = self.session.get(f"https://www.pbs.org/show/{show_slug}/", timeout=15)
        if not r.ok:
            self.log.warning(f"Page returned {r.status_code} for '{show_slug}'.")
            return []
        seasons = self._parse_seasons_from_html(r.text)
        if not seasons:
            self.log.warning(f"Unable to find season data for '{show_slug}': {r.status_code}.")
        return seasons

    def _parse_seasons_from_html(self, html: str) -> list[tuple[str, int]]:
        UUID_PAT = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
        seasons: list[tuple[str, int]] = []
        seen: set[str] = set()

        for url_m in re.finditer(
            r'content\.services\.pbs\.org/v3/pbsorg/screens/shows/[^/]+/seasons/(' + UUID_PAT + r')/',
            html,
        ):
            cid = url_m.group(1)
            if cid in seen:
                continue
            seen.add(cid)

            window = html[max(0, url_m.start() - 800):url_m.start()]
            all_ords = re.findall(r'ordinal[\\\"]*\s*:\s*(\d+)', window)
            ordinal = int(all_ords[-1]) if all_ords else (len(seasons) + 1)
            seasons.append((cid, ordinal))

        return sorted(seasons, key=lambda x: x[1], reverse=True)

    def _fetch_show_specials(self, show_slug: str) -> list[dict]:
        url = self.config["endpoints"]["show_specials"].format(show_slug=show_slug)
        r = self.session.get(url)
        if not r.ok:
            self.log.warning(f"Unable to find specials for '{show_slug}': {r.status_code}.")
            return []
        return [
            sp for sp in r.json()
            if sp.get("slug") != sp.get("parent", {}).get("slug")
        ]

    def _fetch_season_episodes(self, show_slug: str, season_cid: str) -> list[dict]:
        url = self.config["endpoints"]["show_episodes"].format(
            show_slug=show_slug,
            season_cid=season_cid,
        )
        r = self.session.get(url)
        if not r.ok:
            self.log.warning(f"Unable to find episodes for season {season_cid}: {r.status_code}.")
            return []
        return r.json()

    def _find_episode_in_show(self, show_slug: str, video_slug: str) -> Optional[dict]:
        r = self.session.get(f"https://www.pbs.org/video/{video_slug}/", timeout=15)
        seasons = self._parse_seasons_from_html(r.text) if r.ok else []
        if not seasons:
            seasons = self._fetch_show_seasons(show_slug)
        for season_cid, _ in seasons:
            for ep in self._fetch_season_episodes(show_slug, season_cid):
                if ep.get("slug") == video_slug:
                    return ep
        for sp in self._fetch_show_specials(show_slug):
            if sp.get("slug") == video_slug:
                return sp
        return None

    @staticmethod
    def _parse_year(date_str: Optional[str]) -> Optional[int]:
        if not date_str:
            return None
        try:
            return int(date_str[:4])
        except (ValueError, TypeError):
            return None