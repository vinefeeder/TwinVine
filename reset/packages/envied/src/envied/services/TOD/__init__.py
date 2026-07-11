import re
import html
import json
import click
import base64
import unicodedata

from langcodes import Language
from typing import Optional, Union
from http.cookiejar import CookieJar
from urllib.parse import urlparse, parse_qsl, urlencode

from envied.core.service import Service
from envied.core.constants import AnyTrack
from envied.core.manifests import DASH, HLS
from envied.core.credential import Credential
from envied.core.tracks import Audio, Chapters, Tracks, Video
from envied.core.session import session as create_curl_session
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T


class TOD(Service):
    """
    Service code for TODTV (TR) [https://todtv.com.tr]
    Version: 2.0.0
    Author: PREDATOR
    Authorization: Cookie
    Author Recommendation: Recommend using the "requests" download method.
    Security: FHD@L3 [Widevine]
    """

    TITLE_RE = r'^(?:https?://(?:www\.)?todtv\.com\.tr/)?(?:(?P<type>diziler|dizi|film|tod-studios|bein-series|belgesel)/)?(?P<slug>[^/\?]+)(?:/(?P<season>[^/\?]+))?(?:/(?P<episode>[^/\?]+))?'
    ID_RE = r'^(?P<prefix>PS|PZ|PT)(?P<id>\d+)$'
    PLAYER_CONFIG_RE = r'var\s+playerConfig\s*=\s*(\{[\s\S]*?\});'
    SEASONS_RE = r'var\s+seasons\s*=\s*(\[[\s\S]*?\]);'
    SEASON_HREF_RE = r'href="([^"]*?(\d+)sezon-[^"]*)"'

    @staticmethod
    @click.command(name="TOD", short_help="https://todtv.com.tr", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return TOD(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.title = title
        self.movie = False
        self.content_id = None
        self.content_type = None
        self.slug = None
        self.license_url = None
        self.drm_token = None
        self.castleblack_token = None

        if (id_match := re.match(self.ID_RE, self.title)):
            self.content_id = self.title
            prefix = id_match.group("prefix")
            self.content_type = {"PS": "series", "PZ": "season", "PT": "episode"}.get(prefix)
            if prefix == "PT":
                self.movie = True
        elif (url_match := re.match(self.TITLE_RE, self.title)):
            self.content_type = url_match.group("type") or "diziler"
            self.slug = url_match.group("slug")
            if s := url_match.group("season"):
                self.slug += f"/{s}"
            if e := url_match.group("episode"):
                self.slug += f"/{e}"
            if self.content_type == "film":
                self.movie = True
        else:
            self.slug = self.title
            self.content_type = "film" if self.movie else "diziler"

    # ──────────────────────────── Auth ──────────────────────────── #

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("TODTV Requires Cookies for Authentication...")

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.todtv.com.tr/",
            "X-Requested-With": "XMLHttpRequest",
        })
        self._sanitize_session()
        self.log.info("Session Authenticated using Cookies...")

    # ──────────────────────────── Titles ──────────────────────────── #

    def get_titles(self) -> Titles_T:
        return self._fetch_movie_titles() if self.movie else self._fetch_series_titles()

    def _fetch_movie_titles(self) -> Movies:
        if self.content_id and self.content_id.startswith("PT"):
            return Movies([Movie(
                id_=self.content_id, service=self.__class__,
                name=self.content_id, language=Language.get("tr"),
                data={"contentId": self.content_id},
            )])

        url = self._build_url(self.slug, "film")
        response = self.session.get(url)
        response.raise_for_status()

        data = self._extract_content_info(response.text)
        if not data:
            raise ValueError(f"Movie Info not Found: {self.slug}")

        return Movies([Movie(
            id_=data.get("contentId", self.slug),
            service=self.__class__,
            name=data.get("title", self.slug),
            description=data.get("description", ""),
            year=data.get("year"),
            language=Language.get("tr"),
            data=data,
        )])

    def _fetch_series_titles(self) -> Series:
        base_path = self.content_type
        response = None
        fallback_types = self.config.get("fallback_types", ["diziler", "dizi", "tod-studios", "bein-series", "belgesel"])

        paths_to_try = [base_path] if base_path else []
        paths_to_try.extend([t for t in fallback_types if t != base_path])

        for path in paths_to_try:
            if not path:
                continue
            try:
                url = self._build_url(self.slug, path)
                resp = self.session.get(url)
                resp.raise_for_status()
                response = resp
                base_path = path
                break
            except Exception:
                continue

        if not response:
            raise ValueError(f"Series Page not Found: {self.slug}")

        series_data = self._extract_series_data(response.text)

        if not series_data["episodes"] and (ep_link := re.search(rf'href="(/[^/]+/{re.escape(self.slug)}/[^"]+)"', response.text)):
            self.log.debug("Trying Data from Episode Page...")
            base_url = self.config.get("endpoints", {}).get("base_url", "https://www.todtv.com.tr")
            if (ep_resp := self.session.get(f"{base_url}{ep_link.group(1)}")) and ep_resp.status_code == 200:
                series_data = self._extract_series_data(ep_resp.text)

        existing_ids = {ep.get("contentId") for ep in series_data["episodes"]}

        sorted_metadata = sorted(
            series_data.get("seasons_metadata", []),
            key=lambda x: int(x["no"]) if str(x["no"]).isdigit() else 999,
        )

        for meta in sorted_metadata:
            s_no, s_id, s_slug = meta["no"], meta["id"], meta.get("slug")
            cached_count = sum(1 for ep in series_data["episodes"] if str(ep.get("season")) == str(s_no))

            if cached_count > 0:
                self.log.info(f"Fetching Season {s_no} data (ID: {s_id or 'Cached'})...")
                self.log.info(f"Season {s_no}: {cached_count} new episodes added.")
                continue

            if not s_id:
                continue

            self.log.info(f"Fetching Season {s_no} data (ID: {s_id})...")

            if s_slug:
                configs_to_try = [self._build_url(s_slug)]
            else:
                page_v_ids = set(re.findall(r'\b(v\d+)\b', response.text))
                series_base = self.slug.split('/')[0]

                configs_to_try = []
                for vid in page_v_ids:
                    configs_to_try.append(self._build_url(f"{series_base}/{s_no}-sezon-{vid}", base_path))
                    configs_to_try.append(self._build_url(f"{series_base}/{s_no}sezon-{vid}", base_path))

                configs_to_try.append(self._build_url(f"{series_base}/{s_no}sezon-{s_id}", base_path))

                if len(configs_to_try) > 2:
                    self.log.info(f"Auto-discovering Season {s_no} URL (scanning {len(page_v_ids)} candidates)...")

            found_season = False
            for url in configs_to_try:
                if found_season:
                    break
                try:
                    if (s_resp := self.session.get(url)) and s_resp.status_code == 200:
                        s_data = self._extract_series_data(s_resp.text)
                        added_count = 0
                        for ep in s_data["episodes"]:
                            if str(ep.get("season")) != str(s_no):
                                continue
                            if ep["contentId"] not in existing_ids:
                                series_data["episodes"].append(ep)
                                existing_ids.add(ep["contentId"])
                                added_count += 1
                        if added_count:
                            self.log.info(f"Season {s_no}: {added_count} new episodes added.")
                            found_season = True
                except Exception:
                    pass

            if not found_season:
                self.log.warning(f"Could not find valid episodes for Season {s_no}")

        if not series_data["episodes"]:
            raise ValueError(f"Episode not found: {self.slug}")

        show_name = series_data.get("title", self.slug)
        episodes = [
            Episode(
                id_=ep["contentId"],
                service=self.__class__,
                title=show_name,
                season=ep["season"],
                number=ep["episode"],
                name=ep.get("name") or f"Bölüm {ep['episode']}",
                description=ep.get("data", {}).get("description", ""),
                year=ep.get("data", {}).get("year"),
                language=Language.get("tr"),
                data=ep["data"],
            ) for ep in series_data["episodes"]
        ]

        return Series(episodes)

    # ──────────────────────────── Tracks ──────────────────────────── #

    def get_tracks(self, title: Title_T) -> Tracks:
        if isinstance(title, Movie):
            page_url = title.data.get("pageUrl") or self._build_url(title.data.get("slug") or self.slug, "film")
        else:
            slug = (title.data or {}).get("slug") or ((title.data or {}).get("customData") or {}).get("slug")
            page_url = self._build_url(slug) if slug else None

        if not page_url:
            raise ValueError(f"URL not found: {title.id}")

        self.log.info(f"🔗 Content Page: {page_url}")
        resp = self.session.get(page_url)
        resp.raise_for_status()

        info = self._extract_stream_info(resp.text)
        manifest_url = info.get("manifestUrl")
        license_url = info.get("licenseUrl")

        if all(info.get(k) for k in ["contentId", "assetId", "usageSpecId"]):
            try:
                self.log.info("▶ Sending play request (playRequest)...")
                play_data = self._play_request(resp.text, info)

                if play_data.get("CdnUrl"):
                    manifest_url = play_data["CdnUrl"]
                    self.session.get(manifest_url, allow_redirects=True)

                if play_data.get("DrmTicket"):
                    self.drm_token = play_data["DrmTicket"].replace("ticket=", "")
                if play_data.get("manifestUrl"):
                    manifest_url = play_data["manifestUrl"]
                if play_data.get("licenseUrl"):
                    license_url = play_data["licenseUrl"]
            except Exception as e:
                self.log.warning(f"Play request failed: {e}")

        if not manifest_url:
            raise ValueError(f"No Manifest URL: {title.id}")

        self.license_url = license_url
        self.log.info(f"Manifest URL: {manifest_url}")

        manifest = HLS if "HLS" in manifest_url or manifest_url.endswith(".m3u8") else DASH
        tracks = manifest.from_url(manifest_url, self.session).to_tracks(language=title.language)

        parsed_manifest = urlparse(manifest_url)
        manifest_query = dict(parse_qsl(parsed_manifest.query))

        if token := manifest_query.get("hdnts"):
            for track in tracks:
                if isinstance(track, (Video, Audio)):
                    t_parsed = urlparse(track.url)
                    t_query = dict(parse_qsl(t_parsed.query))
                    if "hdnts" not in t_query:
                        t_query["hdnts"] = token
                        track.url = t_parsed._replace(query=urlencode(t_query)).geturl()

        for track in tracks:
            if isinstance(track, (Video, Audio)):
                track.license_url = license_url

        self._sanitize_session()
        return tracks

    # ──────────────────────────── DRM ──────────────────────────── #

    def get_widevine_service_certificate(self, **_) -> Optional[str]:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        endpoints = self.config.get("endpoints", {})

        if self.castleblack_token:
            license_url = endpoints.get("license_castleblack", "https://castleblack.digiturk.com.tr/api/widevine/license?version=1.0")
            headers = {
                "Authorization": f"Bearer {self.castleblack_token}",
                "X-CB-Ticket": self.drm_token,
            }
        else:
            if not self.license_url:
                raise ValueError("No License URL.")
            license_url = self.license_url
            headers = {"Content-Type": "application/octet-stream"}
            if self.drm_token:
                headers["X-ErDRM-Message"] = self.drm_token

        resp = self.session.post(license_url, data=challenge, headers=headers)

        if "<License>" in resp.text:
            if match := re.search(r"<License>(.*?)</License>", resp.text):
                return base64.b64decode(match.group(1))

        return resp.content

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    # ──────────────────────────── Helpers ──────────────────────────── #

    def _sanitize_session(self):
        """Normalize non-latin-1 characters in session headers and cookies.

        The requests download worker encodes headers/cookies as latin-1.
        Characters like œ (\u0153) in cookie values set by the TOD server cause
        UnicodeEncodeError.  We strip them here at the source so core
        downloaders stay untouched.
        """
        for key in list(self.session.headers.keys()):
            val = self.session.headers[key]
            if isinstance(val, str):
                try:
                    val.encode("latin-1")
                except UnicodeEncodeError:
                    self.session.headers[key] = (
                        unicodedata.normalize("NFKD", val)
                        .encode("ascii", "ignore")
                        .decode("ascii")
                    )

        # curl_cffi.Cookies → .jar  /  stdlib CookieJar → iterate directly
        cookie_jar = getattr(self.session.cookies, "jar", None) or self.session.cookies
        for cookie in cookie_jar:
            if hasattr(cookie, "value") and isinstance(cookie.value, str):
                try:
                    cookie.value.encode("latin-1")
                except UnicodeEncodeError:
                    cookie.value = (
                        unicodedata.normalize("NFKD", cookie.value)
                        .encode("ascii", "ignore")
                        .decode("ascii")
                    )

    def _play_request(self, html_content: str, info: dict) -> dict:
        verify_token = ""
        if match := re.search(r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"', html_content):
            verify_token = match.group(1)

        form_data = {
            "__RequestVerificationToken": verify_token,
            "contentId": info["contentId"],
            "versionId": info.get("versionId", ""),
            "assetId": info["assetId"],
            "usageSpecId": info["usageSpecId"],
            "contentType": info.get("contentType", "Movie"),
            "assetType": info.get("assetType", "MUL"),
            "videoType": "1",
            "updateWatchingOptions": "True",
            "restart": "False",
        }

        play_request_url = self.config.get("endpoints", {}).get("play_request", "https://www.todtv.com.tr/content/playRequest")
        resp = self.session.post(
            play_request_url,
            data=form_data,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        resp.raise_for_status()

        data = resp.json()
        if data.get("MessageCode") != 0 and not data.get("CdnUrl"):
            raise ValueError(f"PlayRequest error: {data.get('Message', 'Unknown error')} (Code: {data.get('MessageCode')})")

        self.drm_token = (data.get("DrmTicket") or "").replace("ticket=", "")
        self.castleblack_token = data.get("CastleBlackToken")
        return data

    def _extract_stream_info(self, html_content: str) -> dict:
        info = {}

        patterns = {
            "contentId": [r'data-cms-id=["\']([^"\']+)["\']', r'data-content-id=["\']([^"\']+)["\']'],
            "assetId": [r'data-play-asset-id=["\']([^"\']+)["\']', r'data-id=["\']([^"\']+)["\']'],
            "usageSpecId": [r'data-usage-spec=["\']([^"\']+)["\']', r'data-usage-id=["\']([^"\']+)["\']'],
            "versionId": [r'data-version-id=["\']([^"\']+)["\']'],
            "assetType": [r'data-play-asset-asset-type=["\']([^"\']+)["\']', r'data-assettype=["\']([^"\']+)["\']'],
        }

        for key, regexes in patterns.items():
            for regex in regexes:
                if match := re.search(regex, html_content):
                    val = match.group(1)
                    if "{{" not in val:
                        info[key] = val
                        break

        if not info.get("assetId") and (match := re.search(r'data-asset-list=["\'](\[.*?\])["\']', html_content, re.DOTALL)):
            if assets := self._parse_json_safe(match.group(1).replace('&quot;', '"')):
                asset = assets[0]
                info.setdefault("assetId", asset.get("AssetId"))
                info.setdefault("usageSpecId", asset.get("UsageSpecId"))

        return info

    def _build_url(self, slug: str, content_type: str = None) -> str:
        base_url = self.config.get("endpoints", {}).get("base_url", "https://www.todtv.com.tr")
        if slug.startswith("http"):
            return slug
        if slug.startswith("/"):
            return f"{base_url}{slug}"
        base = content_type or self.content_type or "diziler"
        if base in ("series", "season", "episode"):
            base = "diziler"
        return f"{base_url}/{base}/{slug}"

    def _clean_title(self, raw_title: str) -> str:
        if not raw_title:
            return ""
        title = html.unescape(raw_title.split("|")[0].strip())
        title = re.sub(r'\s*-\s*\d+\.\s*Sezon.*', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s*-\s*(?:TOD|beIN|Fantastik|Aksiyon|Dram|Komedi|Bilim Kurgu).*', '', title, flags=re.IGNORECASE)
        return title.strip()

    @staticmethod
    def _parse_json_safe(text: str) -> dict:
        try:
            return json.loads(re.sub(r'//[^\n]*', '', text))
        except Exception:
            return {}

    def _extract_season_slugs(self, html_content: str) -> dict:
        slugs = {}
        for m in re.finditer(self.SEASON_HREF_RE, html_content):
            slugs[int(m.group(2) or m.group(3))] = m.group(1)
        return slugs

    def _process_seasons_list(self, seasons: list, info: dict, source: str, seen_ids: set) -> None:
        if "seasons_metadata" not in info:
            info["seasons_metadata"] = []

        for s_data in seasons:
            s_id = s_data.get("id")
            s_no = int(s_data.get("no", 1))
            episodes = s_data.get("episodes")

            if not any(m["id"] == s_id for m in info["seasons_metadata"]):
                info["seasons_metadata"].append({
                    "id": s_id,
                    "no": s_no,
                    "slug": (s_data.get("customData") or {}).get("slug"),
                    "populated": bool(episodes),
                })

            for ep in episodes or []:
                ep_id = ep.get("id")
                if ep_id in seen_ids:
                    continue
                seen_ids.add(ep_id)

                info["episodes"].append({
                    "contentId": ep_id,
                    "name": ep.get("title"),
                    "season": s_no,
                    "episode": int(ep.get("no", len(info["episodes"]) + 1)),
                    "slug": (ep.get("customData") or {}).get("slug"),
                    "data": ep,
                })

        if info["episodes"]:
            self.log.debug(f"{len(info['episodes'])} episodes found ({source})")

    def _extract_series_data(self, html_content: str) -> dict:
        info = {"episodes": []}
        seen_ids = set()
        turkish_title = None

        if title_match := re.search(r'<title>([^<]+)</title>', html_content):
            turkish_title = title_match.group(1)

        if (match := re.search(self.SEASONS_RE, html_content)) and (seasons := self._parse_json_safe(match.group(1))):
            self._process_seasons_list(seasons, info, "window.seasons", seen_ids)

        if match := re.search(self.PLAYER_CONFIG_RE, html_content):
            config = self._parse_json_safe(match.group(1))
            if config.get("title"):
                turkish_title = config["title"]
            if seasons := config.get("seasons") or config.get("seriesSettings", {}).get("seasons"):
                self._process_seasons_list(seasons, info, "playerConfig", seen_ids)

        found_slugs = self._extract_season_slugs(html_content)
        for meta in info.get("seasons_metadata", []):
            if meta["no"] in found_slugs and not meta.get("slug"):
                meta["slug"] = found_slugs[meta["no"]]

        original_title = self._extract_original_title_from_breadcrumbs(html_content)
        info["title"] = self._format_title(turkish_title, original_title)
        return info

    def _extract_original_title_from_breadcrumbs(self, html_content: str) -> Optional[str]:
        if match := re.search(r'(<script type="application/ld\+json">[^<]*"BreadcrumbList"[^<]*</script>)', html_content, re.DOTALL):
            try:
                json_str = match.group(1).replace('<script type="application/ld+json">', '').replace('</script>', '')
                data = json.loads(json_str)
                items = data.get("itemListElement", [])
                if items:
                    return items[-1].get("item", {}).get("name")
            except Exception:
                pass
        return None

    def _format_title(self, turkish_title: str, original_title: Optional[str]) -> str:
        if not turkish_title:
            return ""
        turkish_title = self._clean_title(turkish_title)

        if original_title:
            original_title = self._clean_title(original_title)
            if original_title.lower() != turkish_title.lower():
                safe = unicodedata.normalize("NFKD", original_title).encode("ascii", "ignore").decode("ascii").strip()
                return f"{turkish_title} ({safe if safe else original_title})"

        return turkish_title

    def _extract_content_info(self, html_content: str) -> dict:
        info = {}

        if match := re.search(self.PLAYER_CONFIG_RE, html_content):
            info.update(self._parse_json_safe(match.group(1)))
        if match := re.search(r'"contentId"\s*:\s*"([^"]+)"', html_content):
            info["contentId"] = match.group(1)
        if match := re.search(r'var\s+videoEventObject\s*=\s*(\{[^;]+\});', html_content):
            info.update(self._parse_json_safe(match.group(1)))

        turkish_title = info.get("title")
        if not turkish_title:
            if match := re.search(r'<title>([^<]+)</title>', html_content):
                turkish_title = match.group(1)

        original_title = self._extract_original_title_from_breadcrumbs(html_content)
        info["title"] = self._format_title(turkish_title, original_title)
        return info

    # ──────────────────────────── Session ──────────────────────────── #
    @staticmethod
    def get_session():
        """Creates a session with retries and browser impersonation."""
        return create_curl_session("chrome124", max_retries=3, status_forcelist=[429, 500, 502, 503, 504])
