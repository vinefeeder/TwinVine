from __future__ import annotations
import base64
import json
import re
import uuid
from http.cookiejar import CookieJar
from typing import Any, Iterable, Optional, Union
import click
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Tracks


class PHLO(Service):
    """
    Service code for Philo (https://www.philo.com).
    www.nostalgic.cc
    Authorization: Cookies
    Security: FHD@L3
    """

    ALIASES = ("PHLO", "philo")

    TITLE_RE = (
        r"^(?:https?://(?:www\.)?philo\.com/(?:[^?#]+/)?)?"
        r"(?P<id>[A-Za-z0-9_\-=]{10,})(?:[/?#].*)?$"
    )

    LANGUAGE = "en"

    CAPABILITIES = [
        "COLLECTION_TILE_GROUPS", "HERO_PROMOTION", "MOVIE_SHOWINGS", "GUIDE_FILTERS",
        "SEARCH_PAGE_RECS", "UNIFIED_SHOWS_MOVIES_SEARCH_RESULTS", "EXTERNAL_CONTENT",
        "SHOW_PAGE_V2", "COLLECTION_GROUPS", "OUT_OF_PLAN_CONTENT", "CHANNEL_TILE_GROUPS_V2",
    ]

    @staticmethod
    @click.command(name="PHLO", short_help="https://www.philo.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("--no-ads", is_flag=True, default=False,
                  help="Skip the ad-break chapter markers.")
    @click.option("--single", is_flag=True, default=False,
                  help="Only take the single title the URL points at.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return PHLO(ctx, **kwargs)

    def __init__(self, ctx, title: str, no_ads: bool, single: bool):
        super().__init__(ctx)
        self.title = title
        self.no_ads = no_ads
        self.single = single

        if not self.config:
            self.log.error(" - config.yaml is missing or empty")
            raise SystemExit(1)

        self.timeout = self.config.get("request_timeout") or 30
        self.ccextract = bool(int(self.config.get("ccextract") or 0))
        self.player_id: Optional[str] = None
        self.session_data: dict = {}
        self._session_cache: dict[str, dict] = {}
        self._manifest_cache: dict[str, Any] = {}

    @property
    def player_path(self):
        return self.cache_dir / "player.json"

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            self.log.error(" - Philo needs browser cookies.")
            raise SystemExit(1)

        self.session.headers.update(self.config.get("headers") or {})

        user = self.session.get(self.config["endpoints"]["user"], timeout=self.timeout)
        if user.status_code != 200:
            self.log.error(f" - Could not read the Philo Account (HTTP {user.status_code}). "
                           "The cookies may be expired.")
            raise SystemExit(1)

        subscription = self._graphql("userSubscription", {})
        if subscription:
            has_access = subscription.get("hasContentAccess")
            self.log.info(f" + Subscription: {subscription.get('state', 'Unknown')} "
                          f"(Access: {'Yes' if has_access else 'No'})")
            if has_access is False:
                self.log.warning(" - This account has no content access.")

        self.player_id = self._register_player()
        self.log.info(" + Authenticated with Philo")

    def _register_player(self) -> str:
        cached = {}
        if self.player_path.exists():
            try:
                cached = json.loads(self.player_path.read_text(encoding="utf-8"))
            except Exception as e:
                self.log.debug(f"Could not read cached player: {e}")

        device_ident = cached.get("deviceIdent") or str(uuid.uuid4())

        profile = dict(self.config.get("player") or {})
        profile["deviceIdent"] = device_ident

        data = self._graphql("registerPlayerV2", profile)
        player = (data or {}).get("player") or {}
        player_id = player.get("id")
        if not player_id:
            self.log.error(f" - registerPlayerV2 returned no player id: {json.dumps(data)[:300]}")
            raise SystemExit(1)

        try:
            self.player_path.parent.mkdir(parents=True, exist_ok=True)
            self.player_path.write_text(
                json.dumps({"deviceIdent": device_ident, "playerId": player_id}, indent=2),
                encoding="utf-8")
        except Exception as e:
            self.log.debug(f"Could not cache player: {e}")

        return player_id

    def _graphql(self, operation: str, variables: dict, data_key: Optional[str] = None,
                 soft: bool = False) -> Optional[dict]:
        def fail(message: str) -> Optional[dict]:
            if soft:
                self.log.debug(f"{operation}: {message}")
                return None
            self.log.error(f" - {message}")
            raise SystemExit(1)

        queries = self.config.get("persisted_queries") or {}
        sha = queries.get(operation)
        if not sha:
            return fail(f"config.yaml has no persisted_queries.{operation}")

        res = self.session.post(
            self.config["endpoints"]["graphql"],
            json=[{
                "operationName": operation,
                "variables": variables,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
            }],
            timeout=self.timeout,
        )
        if res.status_code != 200:
            return fail(f"GraphQL {operation} failed: HTTP {res.status_code} {res.text[:200]}")

        try:
            payload = res.json()
        except Exception as e:
            return fail(f"GraphQL {operation} returned non-JSON: {e}")

        entry = (payload[0] if isinstance(payload, list) and payload else payload) or {}

        for error in entry.get("errors") or []:
            code = ((error.get("extensions") or {}).get("code") or "").upper()
            if code == "PERSISTED_QUERY_NOT_FOUND":
                return fail(f"Philo no longer recognises the {operation} query hash. ")
            return fail(f"GraphQL {operation} error: {error.get('message') or error}")

        return (entry.get("data") or {}).get(data_key or operation)

    _DECODED_ID = re.compile(r"^(?P<kind>[A-Z][A-Za-z0-9]{1,30}):(?P<value>[A-Za-z0-9_.:+/=-]+)$")

    @classmethod
    def _extract_id(cls, title: str) -> Optional[str]:
        text = (title or "").strip()
        if not text:
            return None
        for part in reversed([p for p in re.split(r"[/\\]", text.split("#")[0].split("?")[0]) if p]):
            if cls._decode_kind(part):
                return part
        return None

    @classmethod
    def _decode_kind(cls, node_id: str) -> Optional[str]:
        if not re.fullmatch(r"[A-Za-z0-9_\-=]{10,}", node_id or ""):
            return None
        match = cls._DECODED_ID.match(cls._decode_node_id(node_id))
        return match.group("kind") if match else None

    @classmethod
    def _decode_value(cls, node_id: str) -> Optional[str]:
        match = cls._DECODED_ID.match(cls._decode_node_id(node_id))
        return match.group("value") if match else None

    @staticmethod
    def _decode_node_id(node_id: str) -> str:
        try:
            padded = node_id + "=" * (-len(node_id) % 4)
            return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    @staticmethod
    def _first_int(*values: Any) -> Optional[int]:
        for value in values:
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def _playback_session(self, title_id: str) -> dict:
        if title_id in self._session_cache:
            return self._session_cache[title_id]

        data = self._graphql("createPlaybackSessionV2", {
            "id": title_id,
            "playerId": self.player_id,
            "idfa": None,
            "lat": None,
            "givn": None,
            "tileGroupId": None,
            "broadcastAt": None,
            "startAtOverride": None,
            "isPreload": False,
        })
        if not data:
            self.log.error(f" - No playback session for {title_id}. The title may not be in "
                           "your plan, or the ID is wrong.")
            raise SystemExit(1)

        self._session_cache[title_id] = data
        return data

    def get_titles(self) -> Titles_T:
        title_id = self._extract_id(self.title)
        if not title_id:
            self.log.error(" - Could not find a Philo title ID in that URL. "
                           "Expected a player link or a Bare ID.")
            raise SystemExit(1)

        kind = self._decode_kind(title_id) or "?"
        self.log.debug(f" + Title ID {title_id} ({kind})")

        if kind == "Show" and not self.single:
            titles = self._show_titles(title_id)
            if titles:
                return titles
            self.log.debug("Falling back to the playback session for this Show ID.")

        return self._single_title(title_id)

    def _single_title(self, title_id: str) -> Titles_T:
        session = self._playback_session(title_id)
        node = session.get("node") or {}
        holder = self._presentation(node)

        show = holder.get("show") or {}
        episode = holder.get("episode") or {}

        name = show.get("title") or holder.get("title") or "Unknown"
        description = show.get("longDescription") or show.get("shortDescription")
        year = self._year(show)
        show_type = (show.get("type") or "").upper()

        if show_type == "MOVIE" or (not show_type and not episode):
            return Movies([
                Movie(
                    id_=title_id,
                    service=self.__class__,
                    name=name,
                    year=year,
                    description=description,
                    language=self.LANGUAGE,
                    data={"session": session},
                )
            ])

        season = self._first_int(episode.get("seasonNum"), episode.get("seasonNumber"), episode.get("season"))
        number = self._first_int(episode.get("episodeNum"), episode.get("episodeNumber"), episode.get("number"))
        air_date = episode.get("originalAirDate") or holder.get("startsAt")

        return Series([
            Episode(
                id_=title_id,
                service=self.__class__,
                title=name,
                season=season or 0,
                number=number or 0,
                name=episode.get("subtitle") or episode.get("title") or episode.get("name"),
                description=episode.get("longDescription") or description,
                year=year,
                air_date=air_date if number is None else None,
                language=self.LANGUAGE,
                data={"session": session},
            )
        ])

    @staticmethod
    def _presentation(node: dict) -> dict:
        current = node or {}
        for _ in range(4):
            if current.get("show") or current.get("episode"):
                return current
            for key in ("broadcast", "recording", "vod", "presentation", "node"):
                nested = current.get(key)
                if isinstance(nested, dict):
                    current = nested
                    break
            else:
                break
        return current or {}

    @staticmethod
    def _year(show: dict) -> Optional[int]:
        for key in ("movieReleaseYear", "releaseYear", "year", "originalAirDate"):
            value = show.get(key)
            if not value:
                continue
            match = re.search(r"(\d{4})", str(value))
            if match:
                return int(match.group(1))
        return None

    _SEASON_FILTER = re.compile(r"^SeasonPageFilter:(?P<show>[^:]+):(?P<season>-?\d+)$")

    def _show_titles(self, show_id: str) -> Optional[Titles_T]:
        page = self._graphql("page", {
            "pageType": "SHOW",
            "typeId": show_id,
            "filterId": None,
            "filter": None,
            "sorterId": None,
            "endCursor": None,
            "startCursor": None,
            "firstGroups": 5,
            "initialTiles": 12,
            "lastGroups": None,
            "numSparseGroups": None,
            "includeTileChannel": False,
            "iconFormat": "SVG",
            "capabilities": self.CAPABILITIES,
            "startTime": None,
            "endTime": None,
        }, soft=True)
        if not page:
            return None

        tile = page.get("tile") or {}
        show = tile.get("show") or {}
        show_type = (show.get("type") or "").upper()
        name = page.get("title") or tile.get("title") or show.get("title") or "Unknown"
        description = (page.get("node") or {}).get("longDescription") or tile.get("longDescription")
        year = self._year(show)

        if show_type == "MOVIE":
            return Movies([
                Movie(
                    id_=tile.get("playableAssetId") or show_id,
                    service=self.__class__,
                    name=name,
                    year=year,
                    description=description,
                    language=self.LANGUAGE,
                )
            ])

        episodes = self._season_episodes(show_id, page, name, year, description)
        if not episodes:
            return None

        seasons = sorted({episode.season for episode in episodes})
        self.log.info(f" + {len(episodes)} episodes across {len(seasons)} "
                      f"season{'s'[:len(seasons) ^ 1]}")
        return Series(episodes)

    def _season_episodes(self, show_id: str, page: dict, name: str, year: Optional[int],
                         description: Optional[str]) -> list[Episode]:
        show_value = self._decode_value(show_id)
        episodes: list[Episode] = []
        seen: set[str] = set()

        seasons: list[int] = []
        for season_filter in page.get("filters") or []:
            match = self._SEASON_FILTER.match(self._decode_node_id(season_filter.get("id") or ""))
            if not match:
                continue
            season = int(match.group("season"))
            if season < 0 or not season_filter.get("hasPlayablePresentations"):
                continue
            show_value = show_value or match.group("show")
            seasons.append(season)

        for season in sorted(seasons):
            if not show_value:
                break
            group = self._graphql("tileGroup", {
                "tileGroupId": self._season_tile_group_id(show_value, season),
                "initialTiles": 100,
                "includeTileChannel": False,
                "iconFormat": "SVG",
                "startTime": None,
                "endTime": None,
            }, data_key="node", soft=True)
            tiles = self._all_tiles(group)
            if not tiles:
                self.log.debug(f"Season {season} returned no tiles.")
                continue
            episodes += self._episodes_from_tiles(tiles, name, year, description, seen)

        if not episodes:
            for edge in ((page.get("groups") or {}).get("edges") or []):
                node = edge.get("node") or {}
                if (node.get("type") or "").upper() != "PLAYABLE":
                    continue
                if "Season" not in self._tile_group_name(node.get("id") or ""):
                    continue
                episodes += self._episodes_from_tiles(
                    self._all_tiles(node), name, year, description, seen)

        return episodes

    def _all_tiles(self, group: Optional[dict]) -> list[dict]:
        tiles = (group or {}).get("tiles") or {}
        nodes = [edge.get("node") or {} for edge in (tiles.get("edges") or [])]

        page_info = tiles.get("pageInfo") or {}
        cursor = page_info.get("endCursor")
        while page_info.get("hasNextPage") and cursor:
            more = self._graphql("tiles", {
                "endCursor": cursor,
                "startCursor": None,
                "first": 50,
                "last": None,
                "initialCursor": None,
                "includeTileDescription": True,
                "iconFormat": "SVG",
            }, soft=True)
            if not more:
                break
            nodes += [edge.get("node") or {} for edge in (more.get("edges") or [])]
            page_info = more.get("pageInfo") or {}
            next_cursor = page_info.get("endCursor")
            if next_cursor == cursor:
                break
            cursor = next_cursor

        return nodes

    def _episodes_from_tiles(self, tiles: Iterable[dict], name: str, year: Optional[int],
                             description: Optional[str], seen: set[str]) -> list[Episode]:
        episodes = []
        for tile in tiles:
            asset = tile.get("playableAssetId")
            if not asset or not tile.get("hasPlayable") or tile.get("isInPlan") is False:
                continue
            if asset in seen:
                continue
            seen.add(asset)

            episode = tile.get("episode") or {}
            season = self._first_int(episode.get("seasonNum"), episode.get("seasonNumber"))
            number = self._first_int(episode.get("episodeNum"), episode.get("episodeNumber"))

            episodes.append(Episode(
                id_=asset,
                service=self.__class__,
                title=tile.get("title") or name,
                season=season or 0,
                number=number or 0,
                name=tile.get("subtitle"),
                description=tile.get("longDescription") or tile.get("description") or description,
                year=year,
                air_date=episode.get("originalAirDate") if number is None else None,
                language=self.LANGUAGE,
            ))
        return episodes

    @staticmethod
    def _season_tile_group_id(show_value: str, season: int) -> str:
        inner = json.dumps(
            {"name": "Season V2", "id": f"{show_value}:{season}", "includeExternalAssets": True},
            separators=(",", ":"))
        encoded = base64.urlsafe_b64encode(inner.encode()).decode().rstrip("=")
        return base64.urlsafe_b64encode(f"TileGroup:{encoded}".encode()).decode().rstrip("=")

    @classmethod
    def _tile_group_name(cls, tile_group_id: str) -> str:
        decoded = cls._decode_node_id(tile_group_id)
        if not decoded.startswith("TileGroup:"):
            return ""
        try:
            return json.loads(cls._decode_node_id(decoded.split(":", 1)[1])).get("name") or ""
        except Exception:
            return ""

    def get_tracks(self, title: Title_T) -> Tracks:
        title_id = str(title.id)
        session = self._playback_session(title_id)

        dash_url = session.get("dashURL")
        if not dash_url:
            self.log.error(" - The playback session carried no dashURL. Returns: "
                           f"hlsURL/dashJSONURL: {sorted(session)}")
            raise SystemExit(1)

        self.session_data = session.get("drmProvider") or {}

        manifest_text = self._fetch_manifest(dash_url)
        dash = DASH.from_text(manifest_text, dash_url) if manifest_text \
            else DASH.from_url(url=dash_url, session=self.session)
        self._manifest_cache[title_id] = dash.manifest

        ad_periods = self._ad_period_ids(dash.manifest, session)

        tracks = dash.to_tracks(
            language=title.language or self.LANGUAGE,
            period_filter=(lambda period: (period.get("id") or "").strip() in ad_periods)
            if ad_periods else None,
        )

        if ad_periods:
            for track in tracks:
                data = track.data.get("dash")
                if isinstance(data, dict):
                    data["filtered_period_ids"] = sorted(ad_periods)
            self.log.info(f" + Dropped {len(ad_periods)} ad periods")

        for track in tracks.audio:
            track.language = track.language or title.language or self.LANGUAGE

        if not self.ccextract:
            for track in tracks.videos:
                self._disable_ccextractor(track)
            self.log.info(" + Closed captions disabled.")

        return tracks

    def _disable_ccextractor(self, track) -> None:
        track.closed_captions = []

        def skipped(*_: Any, **__: Any) -> None:
            self.log.debug(f"ccextractor skipped for {getattr(track, 'id', '?')} ")
            return None

        track.ccextractor = skipped

    def _fetch_manifest(self, dash_url: str) -> Optional[str]:
        try:
            res = self.session.get(dash_url, timeout=self.timeout)
            if res.status_code != 200:
                self.log.debug(f"Could not fetch the MPD: HTTP {res.status_code}")
                return None
        except Exception as e:
            self.log.debug(f"Could not fetch the MPD: {e}")
            return None

        try:
            path = self.cache_dir / "last_manifest.mpd"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(res.text, encoding="utf-8")
            self.log.debug(f" + Saved MPD to {path}")
        except Exception as e:
            self.log.debug(f"Could not save the MPD: {e}")

        return res.text

    _AD_PERIOD_ID = re.compile(r"^\d+(?:\.\d+)+$")

    def _ad_period_ids(self, manifest, session: dict) -> set:
        periods = manifest.findall("Period")
        if len(periods) < 2:
            return set()

        windows = self._ad_windows(session)
        break_ids = self._ad_break_ids(session)
        protected = [bool(period.xpath(".//ContentProtection")) for period in periods]
        any_protected = any(protected)

        ads: set = set()
        content = 0

        for period, is_protected in zip(periods, protected):
            period_id = (period.get("id") or "").strip()
            start = self._period_start(period)

            reason = None
            if period_id and self._AD_PERIOD_ID.match(period_id):
                reason = "dotted period id"
            elif period_id and "." in period_id and period_id.split(".")[0] in break_ids:
                reason = "adBreaks id"
            elif any_protected and not is_protected:
                reason = "no ContentProtection"
            elif start is not None and any(s - 0.5 <= start < e - 0.5 for s, e in windows):
                reason = "adBreaks window"

            if not reason:
                content += 1
                continue
            if not period_id:
                self.log.debug(f"Ad period at {start} has no ID and can't be filtered ({reason}).")
                content += 1
                continue

            self.log.debug(f"Period {period_id} is an ad ({reason}).")
            ads.add(period_id)

        if not content:
            self.log.warning(" - Every MPD period seems like an ad.")
            return set()

        return ads

    @staticmethod
    def _ad_break_ids(session: dict) -> set:
        breaks = ((session.get("manifestMetadata") or {}).get("adBreaks")) or []
        return {str(b.get("id")) for b in breaks if b.get("id") is not None}

    @staticmethod
    def _ad_windows(session: dict) -> list:
        breaks = ((session.get("manifestMetadata") or {}).get("adBreaks")) or []
        windows = []
        for ad in breaks:
            start, end = ad.get("start"), ad.get("end")
            if start is None or end is None:
                continue
            windows.append((float(start), float(end)))
        return windows

    _ISO8601 = re.compile(
        r"^P(?:(?P<d>[\d.]+)D)?T?(?:(?P<h>[\d.]+)H)?(?:(?P<m>[\d.]+)M)?(?:(?P<s>[\d.]+)S)?$")

    @classmethod
    def _duration(cls, raw: Optional[str]) -> Optional[float]:
        match = cls._ISO8601.match((raw or "").strip())
        if not match:
            return None
        parts = match.groupdict()
        if not any(parts.values()):
            return None
        return (float(parts["d"] or 0) * 86400 + float(parts["h"] or 0) * 3600
                + float(parts["m"] or 0) * 60 + float(parts["s"] or 0))

    @classmethod
    def _period_start(cls, period) -> Optional[float]:
        return cls._duration(period.get("start"))

    def get_chapters(self, title: Title_T) -> Chapters:
        if self.no_ads:
            return Chapters()

        title_id = str(title.id)
        session = self._playback_session(title_id)

        manifest = self._manifest_cache.get(title_id)
        if manifest is not None:
            chapters = self._chapters_from_periods(manifest, session)
            if chapters is not None:
                return chapters

        return self._chapters_from_ad_breaks(session)

    def _chapters_from_periods(self, manifest, session: dict) -> Optional[Chapters]:
        periods = manifest.findall("Period")
        if len(periods) < 2:
            return None

        ad_periods = self._ad_period_ids(manifest, session)
        if not ad_periods:
            return None

        starts = []
        for index, period in enumerate(periods):
            start = self._period_start(period)
            starts.append(0.0 if start is None and index == 0 else start)
        if any(start is None for start in starts):
            return None

        total = self._duration(manifest.get("mediaPresentationDuration"))

        marks: list[float] = []
        elapsed = 0.0
        previous_was_ad = False

        for index, period in enumerate(periods):
            end = starts[index + 1] if index + 1 < len(periods) else total
            is_ad = (period.get("id") or "").strip() in ad_periods
            if is_ad:
                if not previous_was_ad and elapsed > 0:
                    marks.append(elapsed)
            elif end is not None:
                elapsed += max(0.0, end - starts[index])
            previous_was_ad = is_ad

        marks = [mark for mark in marks if mark < elapsed]
        if not marks:
            return None

        chapters = Chapters()
        for index, mark in enumerate(marks, 1):
            chapters.add(Chapter(timestamp=mark, name=f"Ad Break {index}"))
        return chapters

    def _chapters_from_ad_breaks(self, session: dict) -> Chapters:
        breaks = ((session.get("manifestMetadata") or {}).get("adBreaks")) or []

        chapters = Chapters()
        removed = 0.0
        for index, ad in enumerate(breaks, 1):
            start, end = ad.get("start"), ad.get("end")
            if start is None:
                continue
            chapters.add(Chapter(timestamp=max(0.0, float(start) - removed),
                                 name=f"Ad Break {index}"))
            if end is not None:
                removed += float(end) - float(start)
        return chapters

    def get_widevine_service_certificate(self, **_: Any) -> Optional[str]:
        return self.config.get("certificate")

    def get_widevine_license(self, *, challenge: bytes, title: Title_T,
                             track: AnyTrack = None, **_) -> Optional[Union[bytes, str]]:
        drm = {}
        if title is not None:
            drm = self._playback_session(str(title.id)).get("drmProvider") or {}
        drm = drm or self.session_data

        license_url = next(
            (s.get("licenseURL") for s in (drm.get("drmSystems") or [])
             if (s.get("system") or "").upper() == "WIDEVINE" and s.get("licenseURL")),
            self.config["endpoints"].get("widevine_license"),
        )
        auth_token = drm.get("authToken")
        if not auth_token:
            self.log.error(" - The playback session carried no DRMtoday auth token.")
            raise SystemExit(1)

        res = self.session.post(
            license_url,
            data=challenge,
            headers={"x-dt-auth-token": auth_token, "content-type": "application/octet-stream"},
            timeout=self.timeout,
        )
        if res.status_code != 200:
            raise ValueError(f"Widevine licence denied: HTTP {res.status_code} {res.text[:300]}")

        try:
            payload = res.json()
        except Exception:
            return res.content

        if payload.get("status") not in (None, "OK", "SUCCESS"):
            raise ValueError(f"Widevine licence denied by DRMtoday: {json.dumps(payload)[:300]}")
        if not payload.get("license"):
            raise ValueError(f"No licence in DRMtoday response: {json.dumps(payload)[:300]}")

        return payload["license"]
