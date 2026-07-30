import base64
import json
import re
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import List, Optional
from collections.abc import Generator
import click
import jwt
from langcodes import Language
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Subtitle, Tracks


class KNPY(Service):
    """
    Service code for Kanopy (https://kanopy.com).
    www.nostalgic.cc
    Authorization: Cookies, Credentials
    Security: FHD@L3
    Geofence: US, CA, UK, AU, NZ
    """

    TITLE_RE = r"^https?://(?:www\.)?kanopy\.com/.+/(?P<id>\d+)$"
    GEOFENCE = ()
    NO_SUBTITLES = False

    @staticmethod
    @click.command(name="KNPY", short_help="https://kanopy.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return KNPY(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        if not self.config:
            raise ValueError("KNPY configuration not found.")

        self.cdm = ctx.obj.cdm

        match = re.match(self.TITLE_RE, title)
        if match:
            self.content_id = match.group("id")
        else:
            self.content_id = None
            self.search_query = title

        self.API_VERSION = self.config["client"]["api_version"]
        self.USER_AGENT = self.config["client"]["user_agent"]
        self.WIDEVINE_UA = self.config["client"]["widevine_ua"]

        self.session.headers.update({
            "x-version": self.API_VERSION,
            "user-agent": self.USER_AGENT,
        })

        subdomain_match = re.search(r'kanopy\.com/[a-z]{2}/([^/]+)', title)
        self._subdomain = subdomain_match.group(1) if subdomain_match else None

        try:
            from pyplayready.cdm import Cdm as PlayReadyCdm
            self.use_playready: bool = isinstance(ctx.obj.cdm, PlayReadyCdm)
        except ImportError:
            self.use_playready = False

        self._jwt = None
        self._visitor_id = None
        self._user_id = None
        self._domain_id = None
        self.widevine_license_url = None
        self.playready_license_url = None

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        if cookies:
            jwt_token = None
            cookie_visitor_id = None
            cookie_uid = None

            for cookie in cookies:
                if cookie.name == "kapi_token":
                    jwt_token = cookie.value
                elif cookie.name == "visitor_id":
                    cookie_visitor_id = cookie.value
                elif cookie.name == "uid":
                    cookie_uid = cookie.value

            if jwt_token:
                self.log.info("Attempting cookie-based authentication.")
                self._jwt = jwt_token
                self.session.headers.update({"authorization": f"Bearer {self._jwt}"})

                try:
                    decoded_jwt = jwt.decode(self._jwt, options={"verify_signature": False})

                    exp_timestamp = decoded_jwt.get("exp")
                    if exp_timestamp and exp_timestamp < datetime.now(timezone.utc).timestamp():
                        self.log.warning("Cookie token has expired.")
                        if credential:
                            self.log.info("Falling back to credential-based authentication.")
                        else:
                            raise ValueError("Cookie token expired and no credentials provided.")
                    else:
                        jwt_data = decoded_jwt.get("data", {})
                        identity_id = jwt_data.get("identity_id")
                        uid = jwt_data.get("uid")
                        self._user_id = (identity_id if identity_id and str(identity_id) != "0" else None) \
                                     or (uid if uid and str(uid) != "0" else None) \
                                     or cookie_uid
                        self._visitor_id = jwt_data.get("visitor_id") or cookie_visitor_id

                        self.log.info(f"Successfully authenticated via cookies (user_id: {self._user_id or 0})")
                        self._fetch_user_details()
                        return

                except jwt.DecodeError as e:
                    self.log.error(f"Failed to decode cookie token: {e}")
                    if credential:
                        self.log.info("Falling back to credential-based authentication.")
                    else:
                        raise ValueError(f"Invalid kapi_token cookie: {e}")
                except KeyError as e:
                    self.log.error(f"Missing expected field in cookie token: {e}")
                    if credential:
                        self.log.info("Falling back to credential-based authentication.")
                    else:
                        raise ValueError(f"Invalid kapi_token structure: {e}")
            else:
                self.log.info("No kapi_token found in cookies.")
                if not credential:
                    raise ValueError("No kapi_token cookie found and no credentials provided.")
                self.log.info("Falling back to credential-based authentication.")

        if not self._jwt:
            if not credential or not credential.username or not credential.password:
                raise ValueError("Kanopy requires either cookies (with kapi_token) or email/password for authentication.")

            cache = self.cache.get("auth_token")

            if cache and not cache.expired:
                cached_data = cache.data
                valid_token = None

                if isinstance(cached_data, dict) and "token" in cached_data:
                    if cached_data.get("username") == credential.username:
                        valid_token = cached_data["token"]
                        self.log.info("Using cached authentication token")
                    else:
                        self.log.info(f"Cached token belongs to '{cached_data.get('username')}', but logging in as '{credential.username}'.")

                elif isinstance(cached_data, str):
                    self.log.info("Found legacy cached token format.")

                if valid_token:
                    self._jwt = valid_token
                    self.session.headers.update({"authorization": f"Bearer {self._jwt}"})

                    if not self._user_id or not self._domain_id or not self._visitor_id:
                        try:
                            decoded_jwt = jwt.decode(self._jwt, options={"verify_signature": False})
                            self._user_id = decoded_jwt["data"]["uid"]
                            self._visitor_id = decoded_jwt["data"]["visitor_id"]
                            self.log.info("Extracted user_id and visitor_id from cached token.")
                            self._fetch_user_details()
                            return
                        except (KeyError, jwt.DecodeError) as e:
                            self.log.error(f"Could not decode cached token: {e}.")

            self.log.info("Performing handshake to get visitor token.")
            r = self.session.get(self.config["endpoints"]["handshake"])
            r.raise_for_status()
            handshake_data = r.json()
            self._visitor_id = handshake_data["visitorId"]
            initial_jwt = handshake_data["jwt"]

            self.log.info(f"Logging in as {credential.username}.")
            login_payload = {
                "credentialType": "email",
                "emailUser": {
                    "email": credential.username,
                    "password": credential.password,
                },
            }
            r = self.session.post(
                self.config["endpoints"]["login"],
                json=login_payload,
                headers={"authorization": f"Bearer {initial_jwt}"},
            )
            r.raise_for_status()
            login_data = r.json()
            self._jwt = login_data["jwt"]
            self._user_id = login_data["userId"]

            self.session.headers.update({"authorization": f"Bearer {self._jwt}"})
            self.log.info(f"Successfully authenticated as {credential.username}")

            self._fetch_user_details()

            try:
                decoded_jwt = jwt.decode(self._jwt, options={"verify_signature": False})
                exp_timestamp = decoded_jwt.get("exp")
                cache_payload = {"token": self._jwt, "username": credential.username}

                if exp_timestamp:
                    expiration_in_seconds = int(exp_timestamp - datetime.now(timezone.utc).timestamp())
                    self.log.info(f"Caching token for {expiration_in_seconds / 60:.2f} minutes.")
                    cache.set(data=cache_payload, expiration=expiration_in_seconds)
                else:
                    self.log.warning("JWT has no 'exp' claim, caching for 1 hour as a fallback.")
                    cache.set(data=cache_payload, expiration=3600)
            except Exception as e:
                self.log.error(f"Failed to decode JWT for caching: {e}. Caching for 1 hour as a fallback.")
                cache.set(data={"token": self._jwt, "username": credential.username}, expiration=3600)

    def _fetch_user_details(self):
        if not self._user_id or str(self._user_id) == "0":
            if not self._subdomain:
                raise ValueError(
                    "Cannot determine library domain."
                )
            self.log.info(f"Looking up institution by subdomain: {self._subdomain}")
            r = self.session.get(self.config["endpoints"]["institutions"].format(subdomain=self._subdomain))
            r.raise_for_status()
            inst = r.json()
            self._domain_id = str(inst["domainId"])
            self.log.info(f"Found library: {inst.get('sitename', self._subdomain)} (domain ID: {self._domain_id})")
            return

        self.log.info("Fetching user library memberships...")
        r = self.session.get(self.config["endpoints"]["memberships"].format(user_id=self._user_id))
        r.raise_for_status()
        memberships = r.json()

        for membership in memberships.get("list", []):
            if membership.get("status") == "active" and membership.get("isDefault", False):
                self._domain_id = str(membership["domainId"])
                self.log.info(f"Using default library: {membership.get('sitename', 'Unknown')} (ID: {self._domain_id})")
                return

        for membership in memberships.get("list", []):
            if membership.get("status") == "active":
                self._domain_id = str(membership["domainId"])
                self.log.warning(f"No default library found. Using first active domain: {self._domain_id}")
                return

        if memberships.get("list"):
            self._domain_id = str(memberships["list"][0]["domainId"])
            self.log.warning(f"No active library found. Using first available domain: {self._domain_id}")
        else:
            raise ValueError("No library memberships found for this user.")

    def get_titles(self) -> Titles_T:
        if not self.content_id:
            raise ValueError("A content ID is required to get titles.")
        if not self._domain_id:
            raise ValueError("Domain ID not set.")

        r = self.session.get(self.config["endpoints"]["video_info"].format(video_id=self.content_id, domain_id=self._domain_id))
        r.raise_for_status()
        content_data = r.json()

        content_type = content_data.get("type")

        def parse_lang(taxonomies_data: dict) -> Language:
            try:
                langs = taxonomies_data.get("languages", [])
                if langs:
                    lang_name = langs[0].get("name")
                    if lang_name:
                        return Language.find(lang_name)
            except (IndexError, AttributeError, TypeError):
                pass
            return Language.get("en")

        if content_type == "video":
            video_data = content_data["video"]
            return Movies([Movie(
                id_=str(video_data["videoId"]),
                service=self.__class__,
                name=video_data["title"],
                year=video_data.get("productionYear"),
                description=video_data.get("descriptionHtml", ""),
                language=parse_lang(video_data.get("taxonomies", {})),
                data=video_data,
            )])

        elif content_type == "playlist":
            playlist_data = content_data.get("playlist")
            if not playlist_data:
                raise ValueError("Could not find 'playlist' data dictionary.")

            series_title = playlist_data["title"]
            series_year = playlist_data.get("productionYear")

            season_match = re.search(r'(?:Season|S)\s*(\d+)', series_title, re.IGNORECASE)
            season_num = int(season_match.group(1)) if season_match else 1

            r_items = self.session.get(self.config["endpoints"]["video_items"].format(video_id=self.content_id, domain_id=self._domain_id))
            r_items.raise_for_status()
            items_data = r_items.json()

            episodes = []
            for i, item in enumerate(items_data.get("list", [])):
                if item.get("type") != "video":
                    continue
                video_data = item["video"]
                ep_num = i + 1
                ep_match = re.search(r'Ep(?:isode)?\.?\s*(\d+)', video_data.get("title", ""), re.IGNORECASE)
                if ep_match:
                    ep_num = int(ep_match.group(1))
                episodes.append(Episode(
                    id_=str(video_data["videoId"]),
                    service=self.__class__,
                    title=series_title,
                    season=season_num,
                    number=ep_num,
                    name=video_data["title"],
                    description=video_data.get("descriptionHtml", ""),
                    year=video_data.get("productionYear", series_year),
                    language=parse_lang(video_data.get("taxonomies", {})),
                    data=video_data,
                ))

            series = Series(episodes)
            series.name = series_title
            series.description = playlist_data.get("descriptionHtml", "")
            series.year = series_year
            return series

        elif content_type == "collection":
            collection_data = content_data.get("collection")
            if not collection_data:
                raise ValueError("Could not find 'collection' data dictionary.")

            series_title_main = collection_data["title"]
            series_description_main = collection_data.get("descriptionHtml", "")
            series_year_main = collection_data.get("productionYear")

            r_seasons = self.session.get(self.config["endpoints"]["video_items"].format(video_id=self.content_id, domain_id=self._domain_id))
            r_seasons.raise_for_status()
            seasons_data = r_seasons.json()

            all_episodes = []
            self.log.info(f"Processing collection '{series_title_main}', found {len(seasons_data.get('list', []))} seasons.")

            season_counter = 1
            for season_item in seasons_data.get("list", []):
                if season_item.get("type") != "playlist":
                    self.log.warning(f"Skipping unexpected item of type '{season_item.get('type')}' in collection.")
                    continue

                season_playlist_data = season_item["playlist"]
                season_id = season_playlist_data["videoId"]
                season_title = season_playlist_data["title"]

                self.log.info(f"Fetching episodes for season: {season_title}")

                season_match = re.search(r'(?:Season|S)\s*(\d+)', season_title, re.IGNORECASE)
                if season_match:
                    season_num = int(season_match.group(1))
                else:
                    self.log.warning(f"Could not parse season number from '{season_title}'. Using sequential number {season_counter}.")
                    season_num = season_counter
                    season_counter += 1

                r_episodes = self.session.get(self.config["endpoints"]["video_items"].format(video_id=season_id, domain_id=self._domain_id))
                r_episodes.raise_for_status()
                episodes_data = r_episodes.json()

                for i, episode_item in enumerate(episodes_data.get("list", [])):
                    if episode_item.get("type") != "video":
                        continue
                    video_data = episode_item["video"]
                    ep_num = i + 1
                    ep_match = re.search(r'Ep(?:isode)?\.?\s*(\d+)', video_data.get("title", ""), re.IGNORECASE)
                    if ep_match:
                        ep_num = int(ep_match.group(1))
                    all_episodes.append(Episode(
                        id_=str(video_data["videoId"]),
                        service=self.__class__,
                        title=series_title_main,
                        season=season_num,
                        number=ep_num,
                        name=video_data["title"],
                        description=video_data.get("descriptionHtml", ""),
                        year=video_data.get("productionYear", series_year_main),
                        language=parse_lang(video_data.get("taxonomies", {})),
                        data=video_data,
                    ))

            if not all_episodes:
                self.log.error(f"Collection '{series_title_main}' did not show any episodes.")
                return Series([])

            series = Series(all_episodes)
            series.name = series_title_main
            series.description = series_description_main
            series.year = series_year_main
            return series

        else:
            raise ValueError(f"Unsupported content type: {content_type}")

    def get_tracks(self, title: Title_T) -> Tracks:
        play_payload = {
            "videoId": int(title.id),
            "domainId": int(self._domain_id),
            "visitorId": self._visitor_id,
        }

        self.session.headers.setdefault("authorization", f"Bearer {self._jwt}")
        self.session.headers.setdefault("x-version", self.API_VERSION)
        self.session.headers.setdefault("user-agent", self.USER_AGENT)

        r = self.session.post(self.config["endpoints"]["plays"], json=play_payload)
        response_json = None
        try:
            response_json = r.json()
        except Exception:
            pass

        if r.status_code == 403:
            if response_json and response_json.get("errorSubcode") == "playRegionRestricted":
                self.log.error("This video is not available in your country.")
                raise PermissionError(
                    "Playback blocked by region restriction."
                )
            else:
                self.log.error(f"Access forbidden. Response: {response_json}")
                raise PermissionError("Kanopy denied access to this video.")

        r.raise_for_status()
        play_data = response_json or r.json()

        manifest_url = None
        manifest_type = None
        drm_info = {}

        for manifest in play_data.get("manifests", []):
            manifest_type_raw = manifest["manifestType"]
            url = manifest["url"].strip()

            if url.startswith("/"):
                url = f"https://www.kanopy.com{url}"

            drm_type = manifest.get("drmType")

            if manifest_type_raw == "dash":
                manifest_url = url
                manifest_type = "dash"

                if drm_type in ("kanopyDrm", "studioDrm"):
                    license_id = manifest.get("drmLicenseID") or f"{play_data.get('playId')}-0"
                    self.widevine_license_url = self.config["endpoints"]["widevine_license"].format(
                        license_id=license_id
                    )
                    self.playready_license_url = self.config["endpoints"]["playready_license"].format(
                        license_id=license_id
                    )
                else:
                    self.log.warning(f"Unknown DASH drmType: {drm_type}")
                    self.widevine_license_url = None
                    self.playready_license_url = None
                break

            elif manifest_type_raw == "hls" and not manifest_url:
                manifest_url = url
                manifest_type = "hls"

                if drm_type == "fairplay":
                    self.log.warning("HLS with FairPlay DRM is not supported.")
                    self.widevine_license_url = None
                    drm_info["fairplay"] = True
                else:
                    self.widevine_license_url = None
                    drm_info["clear"] = True

        if not manifest_url:
            raise ValueError("Could not find a DASH or HLS manifest for this title.")
        if manifest_type == "dash" and not self.widevine_license_url and not self.playready_license_url:
            raise ValueError("Could not construct a license URL for DASH manifest.")

        self.log.info(f"Fetching {manifest_type.upper()} manifest from: {manifest_url}")
        r = self.session.get(manifest_url)
        r.raise_for_status()

        if manifest_type == "dash":
            if not self.use_playready:
                import xml.etree.ElementTree as ET
                ET.register_namespace('', 'urn:mpeg:dash:schema:mpd:2011')
                ET.register_namespace('cenc', 'urn:mpeg:cenc:2013')
                ET.register_namespace('mspr', 'urn:microsoft:playready')
                root = ET.fromstring(r.text)
                for adaptation_set in root.findall('.//{urn:mpeg:dash:schema:mpd:2011}AdaptationSet'):
                    for cp in list(adaptation_set.findall('{urn:mpeg:dash:schema:mpd:2011}ContentProtection')):
                        if '9a04f079-9840-4286-ab92-e65be0885f95' in cp.get('schemeIdUri', ''):
                            adaptation_set.remove(cp)
                mpd_text = ET.tostring(root, encoding='unicode')
            else:
                mpd_text = r.text
            tracks = DASH.from_text(mpd_text, url=manifest_url).to_tracks(language=title.language)
        elif manifest_type == "hls":
            try:
                from envied.core.manifests import HLS
                tracks = HLS.from_text(r.text, url=manifest_url).to_tracks(language=title.language)
                self.log.info("Successfully parsed HLS manifest")
            except ImportError:
                self.log.error(
                    "HLS manifest parser not available in envied. "
                    "Ensure your unshackle installation supports HLS."
                )
                raise
            except Exception as e:
                self.log.error(f"Failed to parse HLS manifest: {e}")
                raise
        else:
            raise ValueError(f"Unsupported manifest type: {manifest_type}")

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.kanopy.com",
            "Referer": "https://www.kanopy.com/",
        })
        self.session.headers.pop("x-version", None)
        self.session.headers.pop("authorization", None)

        for caption_data in play_data.get("captions", []):
            lang = caption_data.get("language", "en")
            label = caption_data.get("label", lang)

            slug = label.lower()
            slug = re.sub(r'[\s\[\]\(\)]+', '-', slug)
            slug = re.sub(r'[^a-z0-9-]', '', slug)
            slug = slug.strip('-')

            track_id = f"caption-{lang}-{slug}"

            for file_info in caption_data.get("files", []):
                if file_info.get("type") == "webvtt":
                    tracks.add(Subtitle(
                        id_=track_id,
                        name=label,
                        url=file_info["url"].strip(),
                        codec=Subtitle.Codec.WebVTT,
                        language=Language.get(lang),
                    ))
                    break

        return tracks

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.widevine_license_url:
            raise ValueError("Widevine license URL was not set.")

        r = self.session.post(
            self.widevine_license_url,
            data=challenge,
            headers={
                "Content-Type": "application/octet-stream",
                "User-Agent": self.WIDEVINE_UA,
                "Authorization": f"Bearer {self._jwt}",
                "X-Version": self.API_VERSION,
            },
        )
        r.raise_for_status()
        return r.content

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.playready_license_url:
            raise ValueError("PlayReady license URL was not set.")

        self.log.info(f"Requesting PlayReady license from: {self.playready_license_url}")
        r = self.session.post(
            self.playready_license_url,
            data=challenge,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "User-Agent": self.WIDEVINE_UA,
                "Authorization": f"Bearer {self._jwt}",
                "X-Version": self.API_VERSION,
            },
        )
        self.log.info(f"PlayReady license response: HTTP {r.status_code}")
        if not r.ok:
            self.log.error(f"PlayReady license error body: {r.text[:500]}")
        r.raise_for_status()
        return r.content

    def search(self) -> Generator[SearchResult, None, None]:
        if not hasattr(self, 'search_query') or not self.search_query:
            self.log.error("Search query not set.")
            return

        self.log.info(f"Searching for '{self.search_query}'...")

        if not self._domain_id:
            self._fetch_user_details()

        params = {
            "query": self.search_query,
            "sort": "relevance",
            "domainId": self._domain_id,
            "isKids": "false",
            "page": 0,
            "perPage": 40,
        }

        r = self.session.get(self.config["endpoints"]["search"], params=params)
        r.raise_for_status()
        search_data = r.json()

        results_list = search_data.get("list", [])

        if not results_list:
            self.log.warning(f"No results found for '{self.search_query}'")
            return

        for item in results_list:
            video_id = item.get("videoId")
            if not video_id:
                continue
            title = item.get("title", "Unknown Title")
            yield SearchResult(
                id_=str(video_id),
                title=title,
                label="VIDEO/SERIES",
                url=f"https://www.kanopy.com/video/{video_id}",
            )

    def get_chapters(self, title: Title_T) -> list:
        return []
