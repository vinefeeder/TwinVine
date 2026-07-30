from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sys
import time
import uuid
from collections import defaultdict
from copy import deepcopy
from http.cookiejar import CookieJar
from typing import Any, Optional, Generator
from urllib.parse import urlparse, urlencode
from zlib import crc32
from lxml import etree

import click
from langcodes import Language

from envied.core.search_result import SearchResult
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Movie, Movies, Series, Episode, Title_T, Titles_T
from envied.core.tracks import Chapter, Tracks, Subtitle, Track, Video
from envied.core.utilities import is_close_match


class SkySignature:
    """SkyShowtime API signature generator."""
    
    def __init__(self, app_id: str, signature_key: str, version: str = "1.0"):
        self.app_id = app_id
        self.signature_key = signature_key.encode('utf-8')
        self.sig_version = version
    
    def calculate_signature(self, method: str, url: str, headers: dict, 
                          payload: bytes = b'', timestamp: Optional[int] = None) -> dict:
        if timestamp is None:
            timestamp = int(time.time())
        
        if url.startswith('http'):
            parsed_url = urlparse(url)
            path = parsed_url.path
            if parsed_url.query:
                path += '?' + parsed_url.query
        else:
            path = url
        
        text_headers = ''
        for key in sorted(headers.keys()):
            if key.lower().startswith('x-skyott') or key.lower().startswith('x-showmax'): 
                text_headers += key.lower() + ': ' + str(headers[key]) + '\n'
        
        headers_md5 = hashlib.md5(text_headers.encode()).hexdigest()
        
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        payload_md5 = hashlib.md5(payload).hexdigest()
        
        to_hash = (
            f'{method}\n'
            f'{path}\n'
            f'\n'
            f'{self.app_id}\n'
            f'{self.sig_version}\n'
            f'{headers_md5}\n'
            f'{timestamp}\n'
            f'{payload_md5}\n'
        )
        
        hashed = hmac.new(self.signature_key, to_hash.encode('utf8'), hashlib.sha1).digest()
        signature = base64.b64encode(hashed).decode('utf8')
        
        return {
            'x-sky-signature': f'SkyOTT client="{self.app_id}",signature="{signature}",timestamp="{timestamp}",version="{self.sig_version}"'
        }


class SKST(Service):
    """
    \b
    Service code for SkyShowtime streaming service (https://skyshowtime.com).

    \b
    Author: FairTrade
    Authorization: Cookies or Credentials
    Robustness:
        Widevine:
            L3: 1080p

    \b
    Tips:
        - Use -t/--territory to specify your region (e.g., -t ES for Spain)
        - Use -p/--profile to select a specific profile by name or ID
        - Use --list-profiles to see all available profiles
    """

    ALIASES = ("skyshowtime", "sst")
    TITLE_RE = r"^(?:https?://(?:www\.)?skyshowtime\.com)?/(?:[a-z]{2}/)?watch/asset/(?P<type>tv|movies?)/(?P<slug>[a-z0-9-]+)/(?P<uuid>[a-f0-9-]+).*$"

    @staticmethod
    @click.command(name="SKST", short_help="https://skyshowtime.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-t", "--territory", type=str, default="PL", help="Territory code (e.g., PL, NL, ES)")
    @click.option("-p", "--profile", type=str, default=None, help="Profile name or ID to use")
    @click.option("--list-profiles", is_flag=True, default=False, help="List all available profiles and exit")
    @click.pass_context
    def cli(ctx, **kwargs):
        return SKST(ctx, **kwargs)

    def __init__(self, ctx, title: str, territory: str = "PL", profile: Optional[str] = None, list_profiles: bool = False):
        super().__init__(ctx)
        
        self.territory = territory.upper()
        self.language = self._get_language_for_territory(territory)
        self.requested_profile = profile
        self.list_profiles_only = list_profiles
        
        # Initialize signature generator from config
        sig_config = self.config.get("signature", {})
        self.signer = SkySignature(
            app_id=sig_config.get("app_id", "SHOWMAX-ANDROID-v1"),
            signature_key=sig_config.get("key", ""),
            version=sig_config.get("version", "1.0")
        )
        
        m = re.match(self.TITLE_RE, title, re.IGNORECASE)
        if not m:
            self.search_term = title
            self.title_url = None
            self.content_slug = None
            self.content_uuid = None
            self.content_type = None
            return

        content_type = m.group("type").lower()
        self.content_type = "movie" if content_type.startswith("movie") else "tv"
        self.content_slug = m.group("slug")
        self.content_uuid = m.group("uuid")
        self.title_url = title
        self.search_term = None
        
        self.user_token: Optional[str] = None
        self.device_id: Optional[str] = None
        self.persona_id: Optional[str] = None
        self.persona_data: Optional[dict] = None
        self.all_personas: list[dict] = []
        
        self.drm_license_url: Optional[str] = None
        self.license_token: Optional[str] = None

        self.cdm = ctx.obj.cdm
        _vcodec = ctx.parent.params.get("vcodec")
        self.vcodec = "H265" if _vcodec and "HEVC" in str(_vcodec) else "H264"

    def _get_language_for_territory(self, territory: str) -> str:
        territory_languages = {
            "PL": "pl-PL", "NL": "nl-NL", "ES": "es-ES", "PT": "pt-PT",
            "SE": "sv-SE", "NO": "nb-NO", "DK": "da-DK", "FI": "fi-FI",
            "CZ": "cs-CZ", "SK": "sk-SK", "HU": "hu-HU", "RO": "ro-RO",
            "BG": "bg-BG", "HR": "hr-HR", "SI": "sl-SI", "BA": "bs-BA",
            "RS": "sr-RS", "ME": "sr-ME", "MK": "mk-MK", "AL": "sq-AL",
        }
        return territory_languages.get(territory.upper(), "en-US")

    def _get_common_headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Origin": "https://www.skyshowtime.com",
            "Referer": "https://www.skyshowtime.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

    def _get_skyott_headers(self, extra: Optional[dict] = None) -> dict:
        params = self.config.get("params", {})
        headers = {
            "X-SkyOTT-Provider": params.get("provider", "SKYSHOWTIME"),
            "X-SkyOTT-Territory": self.territory,
            "X-SkyOTT-Proposition": params.get("proposition", "SKYSHOWTIME"),
            "X-SkyOTT-Platform": params.get("platform", "PC"),
            "X-SkyOTT-Device": params.get("device", "COMPUTER"),
            "X-SkyOTT-ActiveTerritory": self.territory,
        }
        if extra:
            headers.update(extra)
        return headers

    def _get_atom_headers(self) -> dict:
        params = self.config.get("params", {})
        headers = self._get_common_headers()
        headers.update(self._get_skyott_headers({
            "X-SkyOTT-Language": "en-US",
            "X-SkyOTT-Client-Version": params.get("client_version", "6.11.21-gsp"),
        }))
        return headers

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        
        self.device_id = self._get_cookie_value(cookies, "deviceid") or str(uuid.uuid4())
        
        if cookies:
            sky_umv = self._get_cookie_value(cookies, "skyUMV")
            if sky_umv:
                self.user_token = sky_umv
                self.log.info("Using existing session from cookies.")
            else:
                raise PermissionError("skyUMV cookie not found. Please provide fresh cookies.")
        elif credential:
            self._authenticate_with_credentials(credential)
            self._get_user_token()
        else:
            raise PermissionError("SKST requires either cookies or credentials for authentication.")
        
        self._fetch_personas()
        
        if self.list_profiles_only:
            self._display_profiles()
            sys.exit(0)
        
        self._select_profile()
        
        self.log.info("SkyShowtime authentication successful.")

    def _get_cookie_value(self, cookies: Optional[CookieJar], name: str) -> Optional[str]:
        if not cookies:
            return None
        for cookie in cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def _authenticate_with_credentials(self, credential: Credential) -> None:
        self.log.info(f"Logging in as {credential.username}...")
        
        signin_url = self.config["endpoints"]["signin"]
        
        headers = self._get_common_headers()
        headers.update({
            "Accept": "application/vnd.siren+json",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        headers.update(self._get_skyott_headers())
        
        r = self.session.post(signin_url, headers=headers, data=urlencode({
            "userIdentifier": credential.username,
            "password": credential.password,
            "rememberMe": "true",
            "isWeb": "true"
        }))
        
        if r.status_code != 200:
            raise PermissionError(f"Sign-in failed: {r.status_code} - {r.text}")
        
        signin_response = r.json()
        
        if signin_response.get("class") != ["success"]:
            raise PermissionError(f"Sign-in failed: {signin_response}")
        
        if "properties" in signin_response and "data" in signin_response["properties"]:
            self.device_id = signin_response["properties"]["data"].get("deviceid", self.device_id)
        
        self.log.debug(f"Sign-in successful. Device ID: {self.device_id}")

    def _get_user_token(self) -> None:
        token_url = self.config["endpoints"]["tokens"]
        params = self.config.get("params", {})
        
        skyott_headers = self._get_skyott_headers({
            "X-SkyOTT-Language": self.language,
        })
        
        payload = {
            "auth": {
                "authScheme": "MESSO",
                "authIssuer": "NOWTV",
                "provider": params.get("provider", "SKYSHOWTIME"),
                "providerTerritory": self.territory,
                "proposition": params.get("proposition", "SKYSHOWTIME")
            },
            "device": {
                "type": params.get("device", "COMPUTER"),
                "platform": params.get("platform", "PC"),
                "id": self.device_id[:20] if self.device_id else str(uuid.uuid4())[:20],
                "drmDeviceId": "UNKNOWN"
            }
        }
        
        payload_str = json.dumps(payload, separators=(',', ':'))
        
        sig_result = self.signer.calculate_signature(
            method="POST",
            url=token_url,
            headers=skyott_headers,
            payload=payload_str.encode('utf-8')
        )
        
        headers = self._get_common_headers()
        headers.update({
            "Accept": "application/vnd.tokens.v1+json",
            "Content-Type": "application/vnd.tokens.v1+json",
        })
        headers.update(skyott_headers)
        headers.update(sig_result)
        
        r = self.session.post(token_url, headers=headers, data=payload_str)
        
        if r.status_code != 200:
            self.log.warning(f"Token request failed: {r.status_code}")
            return
        
        token_data = r.json()
        self.user_token = token_data.get("userToken")
        
        if self.user_token:
            self.log.debug("User token obtained successfully.")

    def _fetch_personas(self) -> None:
        persona_url = self.config["endpoints"]["personas"]
        params = self.config.get("params", {})
        
        query_params = {
            "personaType": "Adult",
            "in_setup": "false"
        }
        
        headers = self._get_common_headers()
        headers.update(self._get_skyott_headers({
            "X-SkyOTT-Language": "en-US",
            "X-SkyOTT-Client-Version": params.get("client_version", "6.11.21-gsp"),
        }))
        headers["Accept"] = "application/json"
        headers["content-type"] = "application/json"
        
        r = self.session.post(persona_url, headers=headers, params=query_params)
        
        if r.status_code != 200:
            self.log.warning(f"Failed to get personas: {r.status_code}")
            return
        
        persona_data = r.json()
        self.all_personas = persona_data.get("personas", [])

    def _display_profiles(self) -> None:
        if not self.all_personas:
            self.log.info("No profiles available.")
            return
        
        self.log.info("\n" + "=" * 60)
        self.log.info("Available Profiles:")
        self.log.info("=" * 60)
        
        for idx, persona in enumerate(self.all_personas, 1):
            display_name = persona.get("displayName", "Unknown")
            persona_id = persona.get("id", "N/A")
            is_account_holder = persona.get("isAccountHolder", False)
            
            controls = persona.get("controls", {})
            maturity_rating = controls.get("maturityRatingLabel", controls.get("maturityRating", "N/A"))
            
            holder_badge = " [Account Holder]" if is_account_holder else ""
            
            self.log.info(f"  [{idx}] {display_name}{holder_badge}")
            self.log.info(f"      ID: {persona_id}")
            self.log.info(f"      Maturity Rating: {maturity_rating}")
        
        self.log.info("=" * 60)

    def _select_profile(self) -> None:
        if not self.all_personas:
            return
        
        selected_persona = None
        
        if self.requested_profile:
            for persona in self.all_personas:
                if persona.get("displayName", "").lower() == self.requested_profile.lower():
                    selected_persona = persona
                    break
            
            if not selected_persona:
                for persona in self.all_personas:
                    if persona.get("id") == self.requested_profile:
                        selected_persona = persona
                        break
            
            if not selected_persona:
                try:
                    idx = int(self.requested_profile) - 1
                    if 0 <= idx < len(self.all_personas):
                        selected_persona = self.all_personas[idx]
                except ValueError:
                    pass
            
            if not selected_persona:
                self._display_profiles()
                raise ValueError(f"Profile '{self.requested_profile}' not found.")
        else:
            selected_persona = self.all_personas[0]
        
        self.persona_id = selected_persona.get("id")
        self.persona_data = selected_persona
        
        display_name = selected_persona.get("displayName", "Unknown")
        self.log.info(f"Using profile: {display_name}")

    def get_titles(self) -> Titles_T:
        if not self.content_uuid:
            raise ValueError("No content UUID found.")
        
        headers = self._get_atom_headers()
        atom_url = self.config["endpoints"]["atom_node"]
        
        if self.content_type == "movie":
            slug_paths = [
                f"/movies/{self.content_slug}/{self.content_uuid}",
                f"/movie/{self.content_slug}/{self.content_uuid}",
                f"/film/{self.content_slug}/{self.content_uuid}",
            ]
        else:
            slug_paths = [
                f"/tv/{self.content_slug}/{self.content_uuid}",
            ]
        
        data = None
        for slug_path in slug_paths:
            params = {"slug": slug_path}
            r = self.session.get(atom_url, headers=headers, params=params)
            
            if r.status_code == 200:
                data = r.json()
                break
        
        if not data:
            alt_url = f"{atom_url}/provider_variant_id/{self.content_uuid}"
            r = self.session.get(alt_url, headers=headers)
            
            if r.status_code == 200:
                data = r.json()
            else:
                uuid_url = f"{atom_url}/uuid/{self.content_uuid}"
                r = self.session.get(uuid_url, headers=headers)
                
                if r.status_code == 200:
                    data = r.json()
                else:
                    raise RuntimeError(f"Failed to get content details. UUID: {self.content_uuid}")
        
        content_type = data.get("type", "")
        
        if "SERIES" in content_type:
            return self._parse_series(data)
        elif "MOVIE" in content_type or "FILM" in content_type:
            return self._parse_movie(data)
        else:
            attrs = data.get("attributes", {})
            if attrs.get("availableSeasonCount") or attrs.get("availableEpisodeCount"):
                return self._parse_series(data)
            else:
                return self._parse_movie(data)

    def _parse_movie(self, data: dict) -> Movies:
        attrs = data.get("attributes", {})
        
        title = attrs.get("title", attrs.get("titleMedium", "Unknown Title"))
        year = attrs.get("year")
        
        formats = attrs.get("formats", {})
        content_id = None
        
        for fmt_key in ["UHDHDR", "UHD4K", "HDSDR", "HD", "SD"]:
            if fmt_key in formats:
                fmt_data = formats[fmt_key]
                if "contentId" in fmt_data:
                    content_id = fmt_data["contentId"]
                    break
        
        if not content_id:
            for fmt_key, fmt_data in formats.items():
                if isinstance(fmt_data, dict) and "contentId" in fmt_data:
                    content_id = fmt_data["contentId"]
                    break
        
        provider_variant_id = attrs.get("providerVariantId", attrs.get("programmeUuid", self.content_uuid))
        
        if not content_id:
            content_id = data.get("id")
        
        original_lang = attrs.get("mainOriginalLanguage", attrs.get("productionLanguage", "en"))
        if "-" in str(original_lang):
            original_lang = original_lang.split("-")[0]
        
        return Movies([
            Movie(
                id_=content_id,
                service=self.__class__,
                name=title,
                year=year,
                language=Language.get(original_lang),
                data={
                    "content_id": content_id,
                    "provider_variant_id": provider_variant_id,
                    "attrs": attrs
                }
            )
        ])

    def _parse_series(self, data: dict) -> Series:
        attrs = data.get("attributes", {})
        
        series_title = attrs.get("title", attrs.get("titleMedium", "Unknown Series"))
        series_uuid = attrs.get("seriesUuid", attrs.get("providerSeriesId", self.content_uuid))
        
        original_lang = attrs.get("mainOriginalLanguage", attrs.get("productionLanguage", "en"))
        if "-" in str(original_lang):
            original_lang = original_lang.split("-")[0]
        
        episodes = self._fetch_all_episodes(series_uuid, series_title, original_lang)
        
        return Series(episodes)

    def _fetch_all_episodes(self, series_uuid: str, series_title: str, original_lang: str) -> list[Episode]:
        episodes = []
        
        atom_url = f"{self.config['endpoints']['atom_node']}/provider_series_id/{series_uuid}"
        
        params = {
            "slug": f"/tv/{self.content_slug}/{series_uuid}",
            "represent": "(items(items),recs[take=8],collections(items(items[take=8])),trailers)"
        }
        
        headers = self._get_atom_headers()
        r = self.session.get(atom_url, headers=headers, params=params)
        
        if r.status_code != 200:
            self.log.warning(f"Failed to fetch series details: {r.status_code}")
            return episodes
        
        data = r.json()
        
        relationships = data.get("relationships", {})
        items = relationships.get("items", {}).get("data", [])
        
        for season_data in items:
            if season_data.get("type") != "CATALOGUE/SEASON":
                continue
            
            season_attrs = season_data.get("attributes", {})
            season_number = season_attrs.get("seasonNumber", 1)
            
            season_relationships = season_data.get("relationships", {})
            season_items = season_relationships.get("items", {}).get("data", [])
            
            for ep_data in season_items:
                if ep_data.get("type") != "ASSET/EPISODE":
                    continue
                
                ep_attrs = ep_data.get("attributes", {})
                ep_number = ep_attrs.get("episodeNumber", 1)
                ep_title = ep_attrs.get("title", ep_attrs.get("episodeName", f"Episode {ep_number}"))
                
                formats = ep_attrs.get("formats", {})
                content_id = None
                for fmt_key, fmt_data in formats.items():
                    if isinstance(fmt_data, dict) and "contentId" in fmt_data:
                        content_id = fmt_data["contentId"]
                        break
                
                provider_variant_id = ep_attrs.get("providerVariantId", ep_attrs.get("programmeUuid"))
                
                episodes.append(
                    Episode(
                        id_=content_id or ep_data.get("id"),
                        service=self.__class__,
                        title=series_title,
                        season=season_number,
                        number=ep_number,
                        name=ep_title,
                        language=Language.get(original_lang),
                        data={
                            "content_id": content_id,
                            "provider_variant_id": provider_variant_id,
                            "attrs": ep_attrs
                        }
                    )
                )
        
        return episodes

    def get_tracks(self, title: Title_T) -> Tracks:
        content_id = title.data.get("content_id")
        provider_variant_id = title.data.get("provider_variant_id")
        
        if not content_id:
            raise ValueError("No content_id found for this title")
        
        want_uhd = self.vcodec == "H265"
        vcodec_str = "H265" if want_uhd else "H264"
        if self.content_type == "movie" and ":" in content_id:
            if want_uhd and "_UHD" not in content_id and "_HDSDR" not in content_id:
                content_id = content_id + "_UHDHDR"
            elif not want_uhd and "_HDSDR" not in content_id and "_UHD" not in content_id:
                content_id = content_id + "_HDSDR"
        
        playback_url = self.config["endpoints"]["playback"]
        
        skyott_headers = self._get_skyott_headers({
            "X-SkyOTT-PinOverride": "false",
            "X-SkyOTT-UserToken": self.user_token,
            "X-SkyOTT-COPPA": "false",
            "X-SkyOTT-JourneyContext": "PRE_FETCH",
            "X-SkyOTT-PrePlayout": "true",
            "X-SkyOTT-Language": "en-US",
        })
        
        attrs = title.data.get("attrs", {})
        if attrs.get("isKidsContent", False):
            skyott_headers["X-SkyOTT-COPPA"] = "true"
        
        persona_maturity = "9"
        if self.persona_data:
            controls = self.persona_data.get("controls", {})
            persona_maturity = controls.get("maturityRating", "9")
        
        payload = {
            "device": {
                "capabilities": [
                    {
                        "protection": "WIDEVINE",
                        "container": "ISOBMFF",
                        "transport": "DASH",
                        "acodec": "AAC",
                        "vcodec": vcodec_str
                    },
                    {
                        "protection": "NONE",
                        "container": "ISOBMFF",
                        "transport": "DASH",
                        "acodec": "AAC",
                        "vcodec": vcodec_str
                    }
                ],
                "maxVideoFormat": "UHD" if want_uhd else "HD",
                "supportedColourSpaces": ["HDR10", "HLG", "DOLBY_VISION", "SDR"] if want_uhd else ["SDR"],
                "model": "PC",
                "hdcpEnabled": True
            },
            "client": {
                "thirdParties": ["FREEWHEEL", "MEDIATAILOR", "CONVIVA"],
                "variantCapable": True
            },
            "contentId": content_id,
            "providerVariantId": provider_variant_id,
            "parentalControlPin": None,
            "personaParentalControlRating": persona_maturity
        }
        
        payload_str = json.dumps(payload, separators=(',', ':'))
        
        sig_result = self.signer.calculate_signature(
            method="POST",
            url=playback_url,
            headers=skyott_headers,
            payload=payload_str.encode('utf-8')
        )
        
        headers = self._get_common_headers()
        headers.update({
            "Accept": "application/vnd.playvod.v1+json",
            "Content-Type": "application/vnd.playvod.v1+json",
        })
        headers.update(skyott_headers)
        headers.update(sig_result)
        
        r = self.session.post(playback_url, headers=headers, data=payload_str)
        
        if r.status_code != 200:
            raise RuntimeError(f"Failed to get playback info: {r.status_code} - {r.text}")
        
        playback_data = r.json()
        
        asset = playback_data.get("asset", {})
        endpoints = asset.get("endpoints", [])
        
        manifest_url = None
        for endpoint in endpoints:
            if endpoint.get("cdn") == "CLOUDFRONT":
                manifest_url = endpoint.get("url")
                break
        
        if not manifest_url and endpoints:
            manifest_url = endpoints[0].get("url")
        
        if not manifest_url:
            raise ValueError("No manifest URL found in playback response")

        protection = playback_data.get("protection", {})
        self.drm_license_url = protection.get("licenceAcquisitionUrl")
        self.license_token = protection.get("licenceToken") 

        manifest_url = manifest_url + "&audio=all&subtitle=all"
        
        dash = DASH.from_url(manifest_url, session=self.session)
        tracks = dash.to_tracks(language=title.language)

        colour_space = playback_data.get("asset", {}).get("format", {}).get("colourSpace", "")
        range_map = {
            "HDR10": Video.Range.HDR10,
            "HLG": Video.Range.HLG,
            "DOLBY_VISION": Video.Range.DV,
            "DV": Video.Range.DV,
        }
        forced_range = range_map.get(colour_space)
        if forced_range:
            for video_track in tracks.videos:
                video_track.range = forced_range

        return tracks

    @staticmethod
    def _process_subtitles(dash: DASH, language: str) -> list[Subtitle]:
        subtitle_groups = defaultdict(list)
        manifest = dash.manifest
        # Define namespace map for DASH MPD
        nsmap = {
            'mpd': 'urn:mpeg:dash:schema:mpd:2011',
            'cenc': 'urn:mpeg:cenc:2013',
        }
        
        # Try to find periods with and without namespace
        periods = manifest.findall("Period", namespaces=None)
        if not periods:
            periods = manifest.findall("{urn:mpeg:dash:schema:mpd:2011}Period")
        if not periods:
            # Try xpath with namespace
            periods = manifest.xpath("//mpd:Period", namespaces={'mpd': 'urn:mpeg:dash:schema:mpd:2011'})
        if not periods:
            # Last resort: find all Period elements regardless of namespace
            periods = manifest.iter()
            periods = [el for el in manifest.iter() if el.tag.endswith('Period')]

        for period in periods:
            # Find AdaptationSets - try multiple methods
            adapt_sets = period.findall("AdaptationSet", namespaces=None)
            if not adapt_sets:
                adapt_sets = period.findall("{urn:mpeg:dash:schema:mpd:2011}AdaptationSet")
            if not adapt_sets:
                adapt_sets = [el for el in period.iter() if el.tag.endswith('AdaptationSet')]
            
            for adapt_set in adapt_sets:
                content_type = adapt_set.get("contentType", "")
                mime_type = adapt_set.get("mimeType", "")
                lang = adapt_set.get("lang")
                
                # Check if this is a text/subtitle adaptation set
                is_text = (
                    content_type == "text" or 
                    "text/vtt" in mime_type or 
                    "application/ttml" in mime_type or
                    adapt_set.get("group") == "3"  # Based on your MPD, group 3 is subtitles
                )
                
                if not is_text or not lang:
                    continue

                # Find Role element
                role = adapt_set.find("Role", namespaces=None)
                if role is None:
                    role = adapt_set.find("{urn:mpeg:dash:schema:mpd:2011}Role")
                if role is None:
                    for el in adapt_set.iter():
                        if el.tag.endswith('Role'):
                            role = el
                            break
                
                # Find Label element
                label = adapt_set.find("Label", namespaces=None)
                if label is None:
                    label = adapt_set.find("{urn:mpeg:dash:schema:mpd:2011}Label")
                if label is None:
                    for el in adapt_set.iter():
                        if el.tag.endswith('Label'):
                            label = el
                            break
                
                # Also check for Label attribute (some MPDs use it as attribute)
                label_text = ""
                if label is not None and label.text:
                    label_text = label.text
                elif adapt_set.get("Label"):
                    label_text = adapt_set.get("Label")
                
                role_value = role.get("value") if role is not None else "subtitle"
                
                key = (lang, role_value, label_text)
                subtitle_groups[key].append((period, adapt_set))

        final_tracks = []
        for (lang, role_value, label_text), adapt_set_group in subtitle_groups.items():
            first_period, first_adapt = adapt_set_group[0]
            
            # Find Representation
            rep = first_adapt.find("Representation", namespaces=None)
            if rep is None:
                rep = first_adapt.find("{urn:mpeg:dash:schema:mpd:2011}Representation")
            if rep is None:
                for el in first_adapt.iter():
                    if el.tag.endswith('Representation'):
                        rep = el
                        break
            
            if rep is None:
                continue

            s_elements_with_context = []
            for _, adapt_set in adapt_set_group:
                # Find Representation in this adapt_set
                current_rep = adapt_set.find("Representation", namespaces=None)
                if current_rep is None:
                    current_rep = adapt_set.find("{urn:mpeg:dash:schema:mpd:2011}Representation")
                if current_rep is None:
                    for el in adapt_set.iter():
                        if el.tag.endswith('Representation'):
                            current_rep = el
                            break
                
                if current_rep is None:
                    continue

                # Find SegmentTemplate - check both Representation and AdaptationSet level
                template = None
                for parent in [current_rep, adapt_set]:
                    template = parent.find("SegmentTemplate", namespaces=None)
                    if template is None:
                        template = parent.find("{urn:mpeg:dash:schema:mpd:2011}SegmentTemplate")
                    if template is None:
                        for el in parent.iter():
                            if el.tag.endswith('SegmentTemplate'):
                                template = el
                                break
                    if template is not None:
                        break
                
                if template is None:
                    continue
                
                # Find SegmentTimeline
                timeline = template.find("SegmentTimeline", namespaces=None)
                if timeline is None:
                    timeline = template.find("{urn:mpeg:dash:schema:mpd:2011}SegmentTimeline")
                if timeline is None:
                    for el in template.iter():
                        if el.tag.endswith('SegmentTimeline'):
                            timeline = el
                            break

                if timeline is not None:
                    start_num = int(template.get("startNumber", 0))
                    
                    # Find S elements
                    s_elements = timeline.findall("S", namespaces=None)
                    if not s_elements:
                        s_elements = timeline.findall("{urn:mpeg:dash:schema:mpd:2011}S")
                    if not s_elements:
                        s_elements = [el for el in timeline.iter() if el.tag.endswith('}S') or el.tag == 'S']
                    
                    s_elements_with_context.extend((start_num, s_elem) for s_elem in s_elements)

            if not s_elements_with_context:
                # No timeline found, but we might still have a valid subtitle track
                # Continue with empty timeline handling
                pass

            s_elements_with_context.sort(key=lambda x: x[0])

            combined_adapt = deepcopy(first_adapt)
            
            # Find combined_rep
            combined_rep = combined_adapt.find("Representation", namespaces=None)
            if combined_rep is None:
                combined_rep = combined_adapt.find("{urn:mpeg:dash:schema:mpd:2011}Representation")
            if combined_rep is None:
                for el in combined_adapt.iter():
                    if el.tag.endswith('Representation'):
                        combined_rep = el
                        break

            if combined_rep is None:
                continue

            # Find or create SegmentTemplate
            seg_template = None
            for parent in [combined_rep, combined_adapt]:
                seg_template = parent.find("SegmentTemplate", namespaces=None)
                if seg_template is None:
                    seg_template = parent.find("{urn:mpeg:dash:schema:mpd:2011}SegmentTemplate")
                if seg_template is None:
                    for el in parent.iter():
                        if el.tag.endswith('SegmentTemplate'):
                            seg_template = el
                            break
                if seg_template is not None:
                    break

            if seg_template is None:
                # Try to find at AdaptationSet level and move to Representation
                template_at_adapt = None
                for el in combined_adapt.iter():
                    if el.tag.endswith('SegmentTemplate'):
                        template_at_adapt = el
                        break
                
                if template_at_adapt is not None:
                    seg_template = deepcopy(template_at_adapt)
                    combined_rep.append(seg_template)
                    try:
                        combined_adapt.remove(template_at_adapt)
                    except ValueError:
                        pass
                else:
                    continue

            # Remove existing SegmentTimeline if present
            existing_timeline = None
            for el in seg_template.iter():
                if el.tag.endswith('SegmentTimeline'):
                    existing_timeline = el
                    break
            
            if existing_timeline is not None:
                try:
                    seg_template.remove(existing_timeline)
                except ValueError:
                    pass

            # Create new timeline with collected S elements
            if s_elements_with_context:
                new_timeline = etree.Element("SegmentTimeline")
                new_timeline.extend(deepcopy(s) for _, s in s_elements_with_context)
                seg_template.append(new_timeline)

            seg_template.set("startNumber", "0")
            if "endNumber" in seg_template.attrib:
                del seg_template.attrib["endNumber"]

            track_id = hex(crc32(f"sub-{lang}-{role_value}-{label_text}".encode()) & 0xFFFFFFFF)[2:]
            lang_obj = Language.get(lang)
            track_name = "Original" if (language and is_close_match(lang_obj, [language])) else lang_obj.display_name()

            # Determine codec from mimeType
            mime_type = first_adapt.get("mimeType", "text/vtt")
            if "ttml" in mime_type.lower():
                codec = Subtitle.Codec.TimedTextMarkupLang
            else:
                codec = Subtitle.Codec.WebVTT

            final_tracks.append(
                Subtitle(
                    id_=track_id,
                    url=dash.url,
                    codec=codec,
                    language=lang_obj,
                    is_original_lang=bool(language and is_close_match(lang_obj, [language])),
                    descriptor=Track.Descriptor.DASH,
                    sdh="sdh" in label_text.lower() or role_value == "caption",
                    forced="forced" in label_text.lower() or "forced" in role_value.lower(),
                    name=track_name,
                    data={
                        "dash": {
                            "manifest": manifest,
                            "period": first_period,
                            "adaptation_set": combined_adapt,
                            "representation": combined_rep,
                        }
                    },
                )
            )

        return final_tracks

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.drm_license_url:
            raise ValueError("DRM license URL not available.")
        
        sig_result = self.signer.calculate_signature(
            method="POST",
            url=self.drm_license_url,
            headers={},
            payload=challenge
        )
        
        headers = self._get_common_headers()
        headers.update({
            "Content-Type": "text/plain",
        })
        headers.update(sig_result)
        
        r = self.session.post(
            self.drm_license_url,
            data=challenge,
            headers=headers
        )
        
        if r.status_code != 200:
            raise RuntimeError(f"License request failed: {r.status_code} - {r.text}")
        
        return r.content

    # def search(self) -> Generator[SearchResult, None, None]:
    #     if not self.search_term:
    #         return
        
    #     search_url = self.config["endpoints"]["atom_search"]
        
    #     params = {
    #         "q": self.search_term,
    #         "take": 20,
    #         "skip": 0
    #     }
        
    #     headers = self._get_atom_headers()
    #     r = self.session.get(search_url, headers=headers, params=params)
        
    #     if r.status_code != 200:
    #         return
        
    #     data = r.json()
    #     results = data.get("results", data.get("data", []))
        
    #     for result in results:
    #         attrs = result.get("attributes", {})
    #         content_type = result.get("type", "").lower()
            
    #         title = attrs.get("title", attrs.get("titleMedium", "Unknown"))
    #         year = attrs.get("year")
    #         slug = attrs.get("slug", "")
            
    #         if "series" in content_type:
    #             type_str = "series"
    #         elif "movie" in content_type or "film" in content_type:
    #             type_str = "movie"
    #         else:
    #             type_str = "unknown"
            
    #         yield SearchResult(
    #             id_=result.get("id"),
    #             title=title,
    #             year=year,
    #             type_=type_str,
    #             url=f"https://www.skyshowtime.com{slug}" if slug else None
    #         )

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []
