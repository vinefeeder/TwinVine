import base64
import hashlib
import json
import os
import random
import re
import secrets
import string
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from http.cookiejar import CookieJar
from typing import Any, Optional
from urllib.parse import quote
import click
import requests
from click.core import ParameterSource
from langcodes import Language
from tldextract import tldextract
from envied.core.cacher import Cacher
from envied.core.credential import Credential
from envied.core.manifests import DASH, ISM
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Attachment, Chapter, Chapters, Subtitle, Tracks, Video
from envied.core.utils.collections import as_list


def _build_ordered_lang_map_from_mpd(mpd_text: str) -> dict:
    import xml.etree.ElementTree as ET
    import re as _re
    ns_strip = _re.compile(r"\{[^}]*\}")
    rid_lang_re = _re.compile(r"^audio_([a-zA-Z]{2,3}-[a-zA-Z0-9]{2,5})")
    result: dict = {}
    try:
        root = ET.fromstring(mpd_text)
        for elem in root.iter():
            if ns_strip.sub("", elem.tag) != "AdaptationSet":
                continue
            content_type = elem.get("contentType", "") or elem.get("mimeType", "")
            if "audio" not in content_type.lower():
                if not any("audio" in (r.get("mimeType", "")).lower() for r in elem):
                    continue
            base_lang = elem.get("lang") or elem.get("language") or ""
            if not base_lang:
                continue
            for r in elem:
                if ns_strip.sub("", r.tag) != "Representation":
                    continue
                rid = r.get("id") or ""
                m = rid_lang_re.match(rid)
                precise = m.group(1) if m else base_lang
                result.setdefault(base_lang, []).append(precise)
    except Exception:
        pass
    return result


def _apply_ordered_lang_map(audio_tracks, lang_map: dict) -> None:
    from langcodes import Language as _Lang
    counters: dict = {}
    for track in audio_tracks:
        base = str(track.language)
        if base not in lang_map:
            continue
        ordered = lang_map[base]
        if not any("-" in p for p in ordered):
            continue
        idx = counters.get(base, 0)
        if idx < len(ordered):
            track.language = _Lang.get(ordered[idx])
        counters[base] = idx + 1


def _resolve_subtitle_language(language_code: str, url: str) -> str:
    if "-" in language_code:
        return language_code
    import re as _re
    m = _re.search(
        r"(?:^|[/_-])(" + _re.escape(language_code) + r"[-_][A-Za-z0-9]{2,5})(?:[/_.-]|$)",
        url, _re.IGNORECASE
    )
    if m:
        return m.group(1).replace("_", "-")
    return language_code


class AMZN(Service):
    """
    Service code for Amazon VOD (https://amazon.com) & Amazon Prime Video (https://primevideo.com).
    www.nostalgic.cc
    Authorization: Credentials, Cookies
    Security: UHD@L1 FHD@Chrome SD@L3
    """

    ALIASES = ["AMZN", "amazon", "prime"]
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?(?P<domain>amazon\.(?P<region>com|co\.uk|de|co\.jp)|primevideo\.com)(?:/.+)?/)?(?P<id>[A-Z0-9]{10,}|amzn1\.dv\.gti\.[a-f0-9-]+)",
        r"^(?:https?://(?:www\.)?(?P<domain>amazon\.(?P<region>com|co\.uk|de|co\.jp)|primevideo\.com)(?:/[^?]*)?(?:\?gti=)?)(?P<id>[A-Z0-9]{10,}|amzn1\.dv\.gti\.[a-f0-9-]+)"
    ]

    REGION_TLD_MAP = {
        "au": "com.au",
        "br": "com.br",
        "jp": "co.jp",
        "mx": "com.mx",
        "tr": "com.tr",
        "gb": "co.uk",
        "us": "com",
    }
    VIDEO_RANGE_MAP = {
        "SDR": "None",
        "HDR10": "Hdr10",
        "DV": "DolbyVision",
    }

    @staticmethod
    @click.command(name="AMZN", short_help="https://amazon.com, https://primevideo.com", help=__doc__)
    @click.argument("title", type=str, required=False)
    @click.option("-b", "--bitrate", default="CBR",
                  type=click.Choice(["CVBR", "CBR", "CVBR+CBR"], case_sensitive=False),
                  help="Video Bitrate Mode to download in. CVBR=Constrained Variable Bitrate, CBR=Constant Bitrate.")
    @click.option("-p", "--player", default="html5",
                  type=click.Choice(["html5", "xp"], case_sensitive=False),
                  help="Video playerType to download in. html5, xp.")
    @click.option("-c", "--cdn", default=None, type=str,
                  help="CDN to download from, defaults to a random CDN from the available set.")
    @click.option("-vq", "--vquality", default="HD",
                  type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                  help="Manifest quality to request.")
    @click.option("-s", "--single", is_flag=True, default=False,
                  help="Force single episode/season instead of getting series ASIN.")
    @click.option("-am", "--amanifest", default="CVBR",
                  type=click.Choice(["CVBR", "CBR", "H265"], case_sensitive=False),
                  help="Manifest to use for audio. Defaults to H265 if the video manifest is missing 640k audio.")
    @click.option("-aq", "--aquality", default="SD",
                  type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                  help="Manifest quality to request for audio. Defaults to the same as --quality.")
    @click.option("-mt", "--manifest", "manifest_type", default="DASH",
                  type=click.Choice(["DASH", "ISM"], case_sensitive=False),
                  help="Manifest protocol for the video stream. ISM (SmoothStreaming) can carry a "
                       "higher-bitrate HDR10 video track than DASH, but requires a registered device.")
    @click.option("-drm", "--drm-system", type=click.Choice(["widevine", "playready"], case_sensitive=False),
                  default="playready",
                  help="which drm system to use")
    @click.option("-nr", "--no-true-region", "no_true_region", is_flag=True, default=False,
                  help="Skip the 'true region' playback session (keep-alive) and don't adopt the IP-geo "
                       "marketplace from the configuration endpoint. Session keep-alive is ON by default.")
    @click.option("-pl", "--playlisted", is_flag=True, default=False,
                  help="Use Amazon's newer LivingRoomPlayer 'vodPlaylistedPlaybackUrls' request path "
                       "(requires a registered device). Off by default; the classic path is used first and "
                       "this is also tried automatically as a fallback when a device is registered.")
    @click.option("-nd", "--no-device", "no_device", is_flag=True, default=False,
                  help="Skip the TV login device-code registration entirely and "
                       "authenticate with cookies only, as an unregistered WebPlayer. Trade-off: "
                       "No ISM/SmoothStreaming and UHD/HDR/DV are usually not licensed to a web player.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return AMZN(ctx, **kwargs)

    def __init__(self, ctx, title, bitrate: str, player: str, cdn: str, vquality: str, single: bool,
                 amanifest: str, aquality: str, manifest_type: str, drm_system: str,
                 no_true_region: bool, playlisted: bool, no_device: bool):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.bitrate = bitrate
        self.player = player
        self.bitrate_source = ctx.get_parameter_source("bitrate")
        self.cdn = cdn
        self.vquality = vquality
        self.vquality_source = ctx.get_parameter_source("vquality")
        self.single = single
        self.amanifest = amanifest
        self.aquality = aquality
        self.manifest_type = (manifest_type or "DASH").upper()
        self.manifest_type_source = ctx.get_parameter_source("manifest_type")
        self.drm_system = drm_system
        self.no_true_region = no_true_region
        self.playlisted = playlisted
        self.no_device = no_device

        assert ctx.parent is not None
        self.ctx = ctx

        self.chapters_only = ctx.parent.params.get("chapters_only")
        self.quality = ctx.parent.params.get("quality") or 1080

        vcodec = ctx.parent.params.get("vcodec")
        range_ = ctx.parent.params.get("range_")

        self.range = range_[0].name if range_ else "SDR"
        vstr = str(vcodec).upper() if vcodec else ""
        if "AV1" in vstr:
            self.vcodec = "AV1"
        elif "HEVC" in vstr:
            self.vcodec = "H265"
        elif "AVC" in vstr:
            self.vcodec = "H264"
        else:
            self.vcodec = "AV1"

        self.cdm = ctx.obj.cdm
        self.profile = ctx.obj.profile
        self.playready = self.drm_system == "playready"
        if ctx.get_parameter_source("drm_system") != ParameterSource.COMMANDLINE:
            try:
                from envied.core.cdm.detect import is_playready_cdm, is_widevine_cdm
                if is_widevine_cdm(self.cdm):
                    self.playready = False
                elif is_playready_cdm(self.cdm):
                    self.playready = True
            except Exception:
                pass
        self.log.info(f" + DRM system: {'PlayReady' if self.playready else 'Widevine'}")

        self.region: dict[str, str] = {}
        self.endpoints: dict[str, str] = {}
        self.device: dict[str, str] = {}

        self.pv = False
        self.event = False
        self.device_token = None
        self.device_refresh_token = None
        self.device_id = None
        self.customer_id = None
        self.client_id = "f22dbddb-ef2c-48c5-8876-bed0d47594fd"
        self.playbackEnvelope = None
        self.playbackInfo = None
        self.session_handoff_token = None
        self.actor_token = None
        self.profile_id = None
        self.living_room = False

        if self.vquality_source != ParameterSource.COMMANDLINE:
            q_check = self.quality[0] if isinstance(self.quality, list) else self.quality

            if 0 < q_check <= 576 and self.range == "SDR":
                self.log.info(" + Setting manifest quality to SD")
                self.vquality = "SD"

            if q_check > 1080:
                self.log.info(" + Setting manifest quality to UHD to be able to get 2160p video track")
                self.vquality = "UHD"

        self.vquality = self.vquality or "HD"

        if self.vquality == "UHD":
            self.vcodec = "H265"

        if self.bitrate_source != ParameterSource.COMMANDLINE:
            if self.manifest_type == "ISM":
                self.bitrate = "CBR"
                self.log.info(" + Forcing bitrate mode to CBR for ISM (SmoothStreaming)")
            elif self.vcodec == "AV1":
                self.bitrate = "CVBR"
                self.log.info(" + Forcing bitrate mode to CVBR for AV1 codec")
            elif self.vcodec == "H265" and self.range == "SDR" and self.bitrate != "CVBR+CBR":
                self.bitrate = "CVBR+CBR"
                self.log.info(" + Changed bitrate mode to CVBR+CBR to be able to get H.265 SDR video track")

            if self.manifest_type != "ISM" and self.vquality == "UHD" and self.range != "SDR" and self.bitrate != "CBR":
                self.bitrate = "CBR"
                self.log.info(f" + Changed bitrate mode to CBR to be able to get highest quality UHD {self.range} video track")

        self.orig_bitrate = self.bitrate
        self.requested_vcodec = self.vcodec

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")

        self.session.cookies.update(cookies)
        self.configure()

    def configure(self) -> None:
        if len(self.title) > 10:
            self.pv = True
        self.pv = True

        self.log.info("Getting Account Region")
        self.region = self.get_region()
        if not self.region:
            self.log.error(" - Failed to get Amazon Account region"); raise SystemExit(1)

        self.log.info(f" + Region: {self.region['code']}")

        self.endpoints = self.prepare_endpoints(self.config["endpoints"], self.region)

        self.session.headers.update({
            "Origin": f"https://{self.region['base']}"
        })

        _profile = self.profile or "default"
        self.device = (self.config.get("device") or {}).get(_profile, {})
        if self.no_device:
            if self.device:
                self.log.info(" + No-Device setting active. Using cookies only.")
            self.device = {}
            if self.manifest_type == "ISM":
                self.log.warning(
                    " - ISM/SmoothStreaming needs a registered device."
                )
                self.manifest_type = "DASH"
            if self.vquality == "UHD" or self.range != "SDR":
                self.log.warning(
                    " - UHD/HDR/DV are generally not licensed to an unregistered WebPlayer."
                )

        dtid_dict = self.config.get("dtid_dict", [])
        if self.device and dtid_dict:
            if self.device.get("device_type") not in set(dtid_dict):
                self.log.error(
                    f" - Device type '{self.device.get('device_type')}' is NOT in the approved "
                    "dtid_dict. Using it could result in an Amazon account ban. Update your config."
                )
                raise SystemExit(1)

        need_device = False
        if (isinstance(self.quality, list) and self.quality[0] > 1080) or self.vquality == "UHD" or self.range != "SDR":
            need_device = True
        if self.manifest_type == "ISM":
            need_device = True
        if self.playlisted:
            need_device = True

        if self.manifest_type == "ISM" and not self.device:
            self.log.error(
                " - ISM/SmoothStreaming requires a configured device in config.yaml (device: profile). "
                "Add one or use --manifest DASH."
            )
            raise SystemExit(1)

        if self.playlisted and not self.device:
            self.log.warning(
                " - --playlisted (LivingRoomPlayer) needs a configured device in config.yaml; "
                "falling back to the classic browser path."
            )
            self.playlisted = False

        if self.device:
            if need_device and self.vcodec == "H265":
                self.log.info("Using device to get UHD manifests")
            else:
                self.log.info(f"Using configured device for profile: {_profile}")
            res_cfg = self.session.get(
                url=self.endpoints["configuration"],
                params={"deviceTypeID": self.device["device_type"], "deviceID": "Tv"}
            )
            self._apply_verified_territory(res_cfg)
            self.register_device()
        else:
            self.log.warning(
                "No Device information was provided for %s, using browser device...",
                self.profile
            )
            self.device_id = hashlib.sha224(
                ("CustomerID" + self.session.headers["User-Agent"]).encode("utf-8")
            ).hexdigest()
            self.device = {"device_type": self.config["device_types"]["browser"]}
            res_cfg = self.session.get(
                url=self.endpoints["configuration"],
                params={"deviceTypeID": self.device["device_type"], "deviceID": "Web"}
            )
            self._apply_verified_territory(res_cfg)
        if self.playlisted and self.device_token:
            if self._ensure_actor_token():
                self.living_room = True
                self.log.info(" + LivingRoomPlayer mode active (actor-token envelope + licensing)")
            else:
                self.log.warning(
                    " - Could not acquire an actor token; --playlisted will use the classic "
                    "envelope/licensing context"
                )

    def _apply_verified_territory(self, res_cfg) -> None:
        if res_cfg.status_code != 200:
            self.log.warning(f" - Configuration endpoint returned {res_cfg.status_code}, using config values")
            return
        try:
            ctx = res_cfg.json().get("requestContext", {})
        except ValueError:
            return
        territory = ctx.get("currentTerritory")
        marketplace = ctx.get("marketplaceID")
        account_region = self.region.get("code")
        if territory and account_region and territory.lower() != account_region.lower():
            self.log.warning(
                f" - IP region '{territory}' does not match account region '{account_region}'. "
                f"Amazon geo-locates by IP. Licensing may be blocked. "
                f"If the license step fails, use a {account_region} IP (e.g. --proxy {account_region} or a VPN)."
            )
            return
        if territory:
            self.log.info(f" + Region (verified): {territory}")
        if marketplace and not self.no_true_region:
            self.region["marketplace_id"] = marketplace
        elif marketplace and self.no_true_region:
            self.log.debug(" + --no-true-region is active. Keeping marketplace from config, ignoring IP-geo value.")

    @staticmethod
    def _clean_show_name(*candidates: Optional[str]) -> str:
        season_suffix = re.compile(
            r"[\s:\-–—]+(?:Season|Series|Staffel|Saison|Temporada|Stagione|Seizoen)\s+\d+\s*$",
            re.IGNORECASE,
        )
        cleaned_fallback = ""
        for candidate in candidates:
            if not candidate:
                continue
            cleaned = season_suffix.sub("", candidate).strip()
            if cleaned and cleaned == candidate.strip():
                return cleaned
            if cleaned and not cleaned_fallback:
                cleaned_fallback = cleaned
        if cleaned_fallback:
            return cleaned_fallback
        return next((c.strip() for c in candidates if c), "")

    def get_titles(self) -> Titles_T:
        res = self.session.get(
            url=self.endpoints["details"],
            params={
                "titleID": self.title,
                "isElcano": "1",
                "sections": ["Atf", "Btf"]
            },
            headers={"Accept": "application/json"}
        )

        if not res.ok:
            self.log.error(f"Unable to get title: {res.text} [{res.status_code}]"); raise SystemExit(1)

        data = res.json()["widgets"]
        product_details = data.get("productDetails", {}).get("detail")

        if not product_details:
            error = res.json()["degradations"][0]
            self.log.error(f"Unable to get title: {error['message']} [{error['code']}]"); raise SystemExit(1)

        if data["pageContext"]["subPageType"] == "Event":
            self.event = True

        if data["pageContext"]["subPageType"] in ["Movie", "Event"]:
            card = data["productDetails"]["detail"]
            return Movies([Movie(
                id_=card["catalogId"],
                name=product_details["title"],
                year=card.get("releaseYear", ""),
                service=self.__class__,
                data=card
            )])

        episodes_list = []
        seasons = []
        for season in (data.get("seasonSelector") or []):
            sid = season.get("titleID")
            if sid and sid not in seasons:
                seasons.append(sid)
            if self.single:
                break
        if not seasons and not self.single:
            seasons = self._discover_seasons_via_spa()
        if not seasons:
            seasons = [self.title]

        for season in seasons:
            res = self.session.get(
                url=self.endpoints["details"],
                params={"titleID": season, "isElcano": "1", "sections": "Btf"},
                headers={"Accept": "application/json"},
            ).json()["widgets"]

            try:
                episode_data_list = res["episodeList"]["episodes"]
            except KeyError:
                continue

            product_details_season = res["productDetails"]["detail"]
            show_name = self._clean_show_name(
                product_details_season.get("parentTitle"),
                product_details.get("parentTitle"),
                product_details_season.get("title"),
                product_details.get("title"),
            )

            for episode in episode_data_list:
                details = episode["detail"]
                episodes_list.append(Episode(
                    id_=details["catalogId"],
                    title=show_name,
                    name=details["title"],
                    season=product_details_season["seasonNumber"],
                    number=episode["self"]["sequenceNumber"],
                    service=self.__class__,
                    data=episode
                ))

            pagination_data = res.get("episodeList", {}).get("actions", {}).get("pagination", [])
            token = next((quote(item.get("token")) for item in pagination_data if item.get("tokenType") == "NextPage"), None)

            while token:
                res_page = self.session.get(
                    url=self.endpoints["getDetailWidgets"],
                    params={
                        "titleID": self.title,
                        "isTvodOnRow": "1",
                        "widgets": f'[{{"widgetType":"EpisodeList","widgetToken":"{token}"}}]'
                    },
                    headers={"Accept": "application/json"}
                ).json()

                episodeListWidget = res_page["widgets"].get("episodeList", {})
                for item in episodeListWidget.get("episodes", []):
                    ep_num = int(item.get("self", {}).get("sequenceNumber", 0))
                    episodes_list.append(Episode(
                        id_=item["detail"]["catalogId"],
                        title=show_name,
                        name=item["detail"]["title"],
                        season=product_details_season["seasonNumber"],
                        number=ep_num,
                        service=self.__class__,
                        data=item
                    ))

                pagination_data = res_page["widgets"].get("episodeList", {}).get("actions", {}).get("pagination", [])
                token = next((quote(item.get("token")) for item in pagination_data if item.get("tokenType") == "NextPage"), None)

        return Series(episodes_list)

    def _discover_seasons_via_spa(self) -> list:
        try:
            headers = {
                "accept": "application/json",
                "device-memory": "8", "downlink": "10", "dpr": "2", "ect": "4g", "rtt": "50",
                "viewport-width": "604", "x-amzn-client-ttl-seconds": "58.999",
                "x-purpose": "navigation", "x-requested-with": "WebSPA",
            }
            base = self.region["base"]
            if self.pv:
                res_spa = self.session.get(f"https://{base}/detail/{self.title}", headers=headers)
                url = (
                    f"https://{base}{res_spa.json()['redirect']}"
                    if "redirect" in res_spa.text else res_spa.url
                )
            else:
                url = f"https://{base}/dp/{self.title}"

            headers["referer"] = url
            response = self.session.get(
                url=url, params={"dvWebSPAClientVersion": "1.0.106799.0"}, headers=headers
            )
            if response.status_code != 200 or not response.text:
                return []

            spa_data = response.json()
            seasons_data = None
            for page in spa_data.get("page", []) or []:
                assembly = page.get("assembly") or {}
                for body in assembly.get("body", []) or []:
                    seasons_data = body.get("props", {}).get("atf", {}).get("state", {}).get("seasons")
                    if seasons_data:
                        break
                if seasons_data:
                    break

            seasons: list = []
            if seasons_data:
                first_group = next(iter(seasons_data.values()), [])
                for season in first_group or []:
                    match = re.search(r"/detail/([A-Z0-9]{10,})/", season.get("seasonLink", ""))
                    if match and match.group(1) not in seasons:
                        seasons.append(match.group(1))
            if seasons:
                self.log.info(f" + Discovered {len(seasons)} season(s) via SPA detail page")
            return seasons
        except Exception as e:
            self.log.debug(f"SPA season discovery failed: {e}")
            return []

    def _codec_fallback_chain(self, codec: str) -> list:
        order = ["AV1", "H265", "H264"]
        if codec not in order:
            return [codec]
        return order[order.index(codec):]

    def _sync_vcodec_filter(self, codec: str) -> None:
        enum_map = {"AV1": Video.Codec.AV1, "H265": Video.Codec.HEVC, "H264": Video.Codec.AVC}
        target = enum_map.get(codec)
        if not target:
            return
        params = getattr(getattr(self.ctx, "parent", None), "params", None)
        if not isinstance(params, dict):
            return
        vlist = params.get("vcodec")
        if isinstance(vlist, list) and vlist and target not in vlist:
            vlist.append(target)

    def _bitrate_for_codec(self, codec: str) -> str:
        if self.bitrate_source == ParameterSource.COMMANDLINE:
            return self.orig_bitrate
        if self.manifest_type == "ISM":
            return "CBR"
        if self.vquality == "UHD" and self.range != "SDR":
            return "CBR"
        if codec == "AV1":
            return "CVBR"
        if codec == "H265" and self.range == "SDR":
            return "CVBR+CBR"
        return self.orig_bitrate

    def _bitrate_candidates(self, codec: str) -> list:
        if self.bitrate_source == ParameterSource.COMMANDLINE:
            return [self.orig_bitrate]
        cands = [self._bitrate_for_codec(codec)]
        for b in ("CBR", "CVBR"):
            if b not in cands:
                cands.append(b)
        return cands

    def get_tracks(self, title: Title_T) -> Tracks:
        if self.chapters_only:
            return Tracks([])
        self._check_entitlement(title)
        self._warn_cdm_quality_mismatch()

        is_hybrid = self.range == "HYBRID"
        effective_range = "DV" if is_hybrid else self.range
        video_protocol = "SmoothStreaming" if self.manifest_type == "ISM" else "DASH"

        def _fetch_for_codec(codec: str) -> dict:
            last: dict = {}
            for bmode in self._bitrate_candidates(codec):
                m = self.get_manifest(
                    title, video_codec=codec, bitrate_mode=bmode, quality=self.vquality,
                    hdr=effective_range, ignore_errors=True, protocol=video_protocol,
                    use_playlisted=self.playlisted,
                )
                if (self.range == "DV" or is_hybrid) and not m.get("vodPlaybackUrls"):
                    m = self.get_manifest(
                        title, video_codec=codec, bitrate_mode=bmode, quality=self.vquality,
                        hdr="HDR10", ignore_errors=True, protocol=video_protocol,
                        use_playlisted=self.playlisted,
                    )
                if not self._usable_manifest(m) and self.device_token and not self.playlisted:
                    fb = self.get_manifest(
                        title, video_codec=codec, bitrate_mode=bmode, quality=self.vquality,
                        hdr=effective_range, ignore_errors=True, protocol=video_protocol,
                        use_playlisted=True,
                    )
                    if self._usable_manifest(fb):
                        m = fb
                if self._usable_manifest(m):
                    self.bitrate = bmode
                    return m
                last = m
            return last
        requested_codec = self.requested_vcodec
        self.vcodec = requested_codec
        self.bitrate = self._bitrate_for_codec(requested_codec)
        codec_chain = self._codec_fallback_chain(requested_codec)
        effective_vcodec = requested_codec

        def _run_chain() -> dict:
            nonlocal effective_vcodec
            mani: dict = {}
            for codec in codec_chain:
                if len(codec_chain) > 1:
                    self.log.info(f" + Requesting {codec} video manifest...")
                mani = _fetch_for_codec(codec)
                if self._usable_manifest(mani):
                    if codec != requested_codec:
                        self.log.warning(f" - {requested_codec} unavailable for this title; using {codec}.")
                    effective_vcodec = codec
                    self.vcodec = codec
                    self._sync_vcodec_filter(codec)
                    return mani
                if len(codec_chain) > 1:
                    self.log.warning(f" - {codec} not available for this title.")
            return mani

        manifest = _run_chain()

        if not self._usable_manifest(manifest):
            self.log.error(
                f" - No usable manifest for this title with any codec ({', '.join(codec_chain)}). "
                "Re-run with -d/--debug to see the Amazon error."
            )
            raise SystemExit(1)

        if "rightsException" in manifest.get("returnedTitleRendition", {}).get("selectedEntitlement", {}):
            self.log.error(" - The profile used does not have the rights to this title.")
            return Tracks([])

        self.session_handoff_token = (manifest.get("sessionization") or {}).get("sessionHandoffToken")
        if isinstance(getattr(title, "data", None), dict):
            title.data["_amzn_manifest"] = manifest

        chosen_manifest = self.choose_manifest(manifest, self.cdn)
        if not chosen_manifest:
            self.log.error("No manifests available"); raise SystemExit(1)

        streamingProtocol = (
            manifest.get("vodPlaybackUrls", {}).get("result", {}).get("playbackUrls", {})
            .get("urlMetadata", {}).get("streamingProtocol", "DASH")
        )
        if self.event:
            devicetype = self.device.get("device_type")
            manifest_url = f"{chosen_manifest['url']}?amznDtid={devicetype}&encoding=segmentBase"
        elif streamingProtocol == "DASH":
            manifest_url = self.clean_mpd_url(chosen_manifest["url"], False)
        else:
            manifest_url = chosen_manifest["url"]

        self.log.info(f" + Manifest: {manifest_url}")

        if streamingProtocol == "DASH":
            _mpd_raw = self.session.get(manifest_url).text
            _lang_order_map = _build_ordered_lang_map_from_mpd(_mpd_raw)
            self.log.info(f" + MPD language map: {sum(len(v) for v in _lang_order_map.values())} representations indexed")
        else:
            _lang_order_map = {}

        tracks = Tracks()

        if streamingProtocol == "DASH":
            tracks = Tracks([
                x for x in iter(DASH.from_text(_mpd_raw, manifest_url).to_tracks(language="en"))
            ])
        elif streamingProtocol == "SmoothStreaming":
            _ism_tracks = Tracks()
            for _t in iter(ISM.from_url(url=manifest_url, session=self.session).to_tracks(language="en")):
                _ism_tracks.add(_t, warn_only=True)
            tracks = _ism_tracks
        else:
            self.log.error(f"Unsupported manifest type: {streamingProtocol}"); raise SystemExit(1)

        if _lang_order_map:
            _apply_ordered_lang_map(tracks.audio, _lang_order_map)
        self._tag_amzn_tracks(tracks, manifest)

        need_separate_audio = ((self.aquality or self.vquality) != self.vquality
                               or self.amanifest == "CVBR" and (self.vcodec, self.bitrate) != ("H264", "CVBR")
                               or self.amanifest == "CBR" and (self.vcodec, self.bitrate) != ("H264", "CBR")
                               or self.amanifest == "H265" and self.vcodec != "H265"
                               or self.amanifest != "H265" and self.vcodec == "H265")

        if not need_separate_audio:
            audios = defaultdict(list)
            for audio in tracks.audio:
                audios[audio.language].append(audio)
            for lang in audios:
                if not any((x.bitrate or 0) >= 640000 for x in audios[lang]):
                    need_separate_audio = True
                    break

        if need_separate_audio:
            manifest_type = self.amanifest
            self.log.info(f"Getting audio from {manifest_type} manifest for potential higher bitrate or better codec")

            audio_manifest = self.get_manifest(
                title=title,
                video_codec="H264",
                bitrate_mode="CVBR",
                quality="HD",
                hdr=None,
                ignore_errors=True
            )

            if not audio_manifest:
                self.log.warning(f" - Unable to get {manifest_type} audio manifests, skipping")
            elif not (chosen_audio_manifest := self.choose_manifest(audio_manifest, self.cdn)):
                self.log.warning(f" - No {manifest_type} audio manifests available, skipping")
            else:
                audio_mpd_url = self.clean_mpd_url(chosen_audio_manifest["url"], optimise=False)
                if self.event:
                    devicetype = self.device.get("device_type")
                    audio_mpd_url = f"{chosen_audio_manifest['url']}?amznDtid={devicetype}&encoding=segmentBase"

                self.log.info(" + Downloading Audio Manifest")
                try:
                    audio_protocol = audio_manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
                    if audio_protocol == "DASH":
                        _a_raw = self.session.get(audio_mpd_url).text
                        _a_lang_order = _build_ordered_lang_map_from_mpd(_a_raw)
                        self.log.info(f" + Audio MPD language map: {sum(len(v) for v in _a_lang_order.values())} entries")
                        audio_tracks = DASH.from_text(_a_raw, audio_mpd_url).to_tracks(language="en")
                        if _a_lang_order:
                            _apply_ordered_lang_map(audio_tracks.audio, _a_lang_order)
                    elif audio_protocol == "SmoothStreaming":
                        audio_tracks = ISM.from_url(url=audio_mpd_url, session=self.session).to_tracks(language="en")
                    else:
                        audio_tracks = Tracks([])

                    self._tag_amzn_tracks(audio_tracks, audio_manifest)
                    tracks.add(audio_tracks.audio, warn_only=True)
                except Exception as e:
                    self.log.warning(f" - Failed to parse audio manifest: {e}")

        if (self.config.get("device") or {}).get(self.profile or "default", None) and not self.no_device:
            self.log.info(" + Fetching DV/UHD manifest for Atmos audio (576kbps DD+)")
            temp_device = self.device
            temp_token = self.device_token
            temp_id = self.device_id

            if not self.device_token:
                try:
                    self.register_device()
                except Exception:
                    pass

            try:
                uhd_audio_manifest = self.get_manifest(
                    title=title,
                    video_codec="H265",
                    bitrate_mode="CVBR+CBR",
                    quality="UHD",
                    hdr="DV",
                    ignore_errors=True
                )
            except Exception:
                uhd_audio_manifest = None

            self.device = temp_device
            self.device_token = temp_token
            self.device_id = temp_id

            if uhd_audio_manifest and (chosen_uhd := self.choose_manifest(uhd_audio_manifest, self.cdn)):
                uhd_url = self.clean_mpd_url(chosen_uhd["url"], optimise=False)
                self.log.info(" + Downloading DV/UHD audio manifest")
                try:
                    uhd_prot = uhd_audio_manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
                    if uhd_prot == "DASH":
                        _uhd_raw = self.session.get(uhd_url).text
                        _uhd_lang_map = _build_ordered_lang_map_from_mpd(_uhd_raw)
                        uhd_tracks = DASH.from_text(_uhd_raw, uhd_url).to_tracks(language="en")
                        if _uhd_lang_map:
                            _apply_ordered_lang_map(uhd_tracks.audio, _uhd_lang_map)
                            self.log.info(f" + DV audio lang map: {sum(len(v) for v in _uhd_lang_map.values())} entries")
                    elif uhd_prot == "SmoothStreaming":
                        uhd_tracks = ISM.from_url(url=uhd_url, session=self.session).to_tracks(language="en")
                    else:
                        uhd_tracks = Tracks([])
                    self._tag_amzn_tracks(uhd_tracks, uhd_audio_manifest)
                    tracks.add(uhd_tracks.audio, warn_only=True)
                    _atmos_tracks = [x for x in uhd_tracks.audio if (x.bitrate or 0) >= 448000 and (x.channels or 0) >= 6]
                    if _atmos_tracks:
                        _best_kbps = max((x.bitrate or 0) for x in _atmos_tracks) // 1000
                        self.log.info(f" + Added {len(_atmos_tracks)} Atmos/high-bitrate audio track(s) from DV manifest (Best: {_best_kbps} kb/s)")
                    else:
                        self.log.info(" + DV audio manifest fetched (No Atmos found for this title)")
                except Exception as e:
                    self.log.warning(f" - Failed to parse DV audio manifest: {e}")
            else:
                self.log.warning(" - DV/UHD audio manifest unavailable for this title")

        self._post_process_audio(tracks.audio)

        _all_codecs = list({v.codec for v in tracks.videos if v.codec})
        if self.vcodec == "AV1":
            filtered = [v for v in tracks.videos if v.codec and ("av01" in v.codec.lower() or "av1" in v.codec.lower())]
            if not filtered:
                self.log.error(
                    f" - No AV1 tracks found (found codecs: {_all_codecs}). "
                    "The manifest may not have AV1 or the codec string is unexpected."
                )
                raise SystemExit(1)
            tracks.videos = filtered
        elif self.vcodec == "H265":
            filtered = [v for v in tracks.videos if v.codec and any(x in v.codec.lower() for x in ["hev1", "hvc1", "h265"])]
            if filtered:
                tracks.videos = filtered
        else:
            filtered = [v for v in tracks.videos if v.codec and "avc" in v.codec.lower()]
            if filtered:
                tracks.videos = filtered

        actual_range = (
            manifest.get("vodPlaybackUrls", {}).get("result", {}).get("playbackUrls", {})
            .get("urlMetadata", {}).get("dynamicRange", "None")
        )
        for video in tracks.videos:
            video.hdr10 = actual_range == "Hdr10"
            video.dv = actual_range == "DolbyVision"

        if (self.range == "DV" or is_hybrid) and actual_range != "DolbyVision":
            friendly = {"Hdr10": "HDR10", "None": "SDR"}.get(actual_range, actual_range)
            self.log.warning(f" - Dolby Vision not available for this title/region. Server returned: {friendly}")

        if (self.range == "DV" or is_hybrid) and actual_range == "DolbyVision":
            self.log.info(" + Hybrid mode: fetching HDR10 manifest for base layer...")
            hdr10_manifest = self.get_manifest(
                title=title,
                video_codec=effective_vcodec,
                bitrate_mode=self.bitrate,
                quality=self.vquality,
                hdr="HDR10",
                ignore_errors=True
            )
            if hdr10_manifest and hdr10_manifest.get("vodPlaybackUrls"):
                chosen_hdr10 = self.choose_manifest(hdr10_manifest, self.cdn)
                if chosen_hdr10:
                    hdr10_url = self.clean_mpd_url(chosen_hdr10["url"], False)
                    self.log.info(f" + HDR10 Manifest (base layer): {hdr10_url}")
                    try:
                        hdr10_protocol = hdr10_manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
                        if hdr10_protocol == "DASH":
                            hdr10_tracks = DASH.from_url(url=hdr10_url, session=self.session).to_tracks(language="en")
                        elif hdr10_protocol == "SmoothStreaming":
                            hdr10_tracks = ISM.from_url(url=hdr10_url, session=self.session).to_tracks(language="en")
                        else:
                            hdr10_tracks = Tracks([])
                        for video in hdr10_tracks.videos:
                            video.hdr10 = True
                            video.dv = False
                        hdr10_video_count = len(list(hdr10_tracks.videos))
                        tracks.add(hdr10_tracks.videos, warn_only=True)
                        self.log.info(f" + Added {hdr10_video_count} HDR10 base-layer video track(s) for hybrid mux")
                    except Exception as e:
                        self.log.warning(f" - Failed to fetch HDR10 base layer for hybrid: {e}")
                else:
                    self.log.warning(" - No HDR10 manifest CDN available for hybrid mode")
            else:
                self.log.warning(" - HDR10 manifest unavailable for hybrid and DV-only tracks will be used")

        for sub in manifest.get("timedTextUrls", {}).get("result", {}).get("subtitleUrls", []) + \
                manifest.get("timedTextUrls", {}).get("result", {}).get("forcedNarrativeUrls", []):
            url = sub["url"]
            url_ext = os.path.splitext(url.split("?")[0])[1].lstrip(".").lower()
            codec_map = {
                "ttml": Subtitle.Codec.TimedTextMarkupLang,
                "dfxp": Subtitle.Codec.TimedTextMarkupLang,
                "vtt": Subtitle.Codec.WebVTT,
                "srt": Subtitle.Codec.SubRip,
            }
            detected_codec = codec_map.get(url_ext, Subtitle.Codec.TimedTextMarkupLang)

            tracks.add(Subtitle(
                id_=f"{sub['trackGroupId']}_{sub['languageCode']}_{sub['type']}_{sub['subtype']}",
                url=url,
                codec=detected_codec,
                language=_resolve_subtitle_language(sub["languageCode"], url),
                forced="ForcedNarrative" in sub["type"],
                sdh=sub["type"].lower() == "sdh"
            ), warn_only=True)

        try:
            images = title.data.get("detail", {}).get("images", {})
            if isinstance(title, Movie):
                image_url = images.get("packshot") or images.get("titleshot") or images.get("covershot")
            else:
                image_url = images.get("covershot") or images.get("packshot") or images.get("titleshot")
            if image_url and image_url.strip():
                tracks.add(Attachment.from_url(
                    url=image_url.strip(),
                    name="cover",
                    mime_type="image/jpeg",
                    session=self.session,
                ))
        except Exception as e:
            self.log.warning(f" - Attachment failed: {e}")
        if not self.no_true_region and self.vquality != "UHD" and tracks.videos:
            self.manage_session(tracks.videos[0])

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        manifest = title.data.get("_amzn_manifest") if isinstance(getattr(title, "data", None), dict) else None
        if not manifest:
            manifest = self.get_manifest(
                title,
                video_codec=self.vcodec,
                bitrate_mode=self.bitrate,
                quality=self.vquality,
                hdr=self.range,
                ignore_errors=True
            )
        if not manifest:
            return Chapters()

        chapters = self._chapters_from_transitions(manifest)
        if chapters:
            self.log.info(f" + Found {len(chapters)} chapter marker(s) from transition timecodes")
            return Chapters(chapters)

        return Chapters(self._chapters_from_xray(manifest))

    def _chapters_from_transitions(self, manifest: dict) -> list:
        result = manifest.get("transitionTimecodes", {}).get("result", {})
        events = result.get("events")
        if not events:
            return []

        def fmt(ms):
            s = ms / 1000
            return f"{int(s // 3600):02d}:{int((s % 3600) // 60):02d}:{s % 60:06.3f}"

        chapters = []
        for event in sorted(events, key=lambda e: e.get("startTimeMs", 0)):
            start_ms = event.get("startTimeMs")
            if start_ms is None:
                continue
            event_type = event.get("eventType", "")
            if event_type == "SKIP_INTRO":
                chapters.append(Chapter(name="Intro", timestamp=fmt(start_ms)))
                if event.get("endTimeMs"):
                    chapters.append(Chapter(name="Main Content", timestamp=fmt(event["endTimeMs"])))
            elif event_type == "SKIP_RECAP":
                chapters.append(Chapter(name="Recap", timestamp=fmt(start_ms)))
                if event.get("endTimeMs"):
                    chapters.append(Chapter(name="Episode Start", timestamp=fmt(event["endTimeMs"])))
            elif event_type == "END_CREDITS":
                chapters.append(Chapter(name="Credits", timestamp=fmt(start_ms)))
        return chapters

    def _chapters_from_xray(self, manifest: dict) -> list:
        if "vodXrayMetadata" not in manifest or "error" in manifest["vodXrayMetadata"]:
            return []
        try:
            xray_params = {
                "pageId": "fullScreen",
                "pageType": "xray",
                "serviceToken": json.dumps({
                    "consumptionType": "Streaming",
                    "deviceClass": "normal",
                    "playbackMode": "playback",
                    "vcid": json.loads(manifest["vodXrayMetadata"]["result"]["parameters"]["serviceToken"])["vcid"]
                }),
                "deviceID": self.device_id,
                "deviceTypeID": self.config["device_types"]["browser"],
                "marketplaceID": self.region["marketplace_id"],
                "gascEnabled": str(self.pv).lower(),
                "decorationScheme": "none",
                "version": "inception-v2",
                "uxLocale": "en-US",
                "featureScheme": "XRAY_WEB_2020_V1"
            }
            xray = self.session.get(url=self.endpoints["xray"], params=xray_params).json().get("page")
        except Exception:
            return []

        if not xray:
            return []

        try:
            widgets = xray["sections"]["center"]["widgets"]["widgetList"]
            scenes = next((x for x in widgets if x["tabType"] == "scenesTab"), None)
            if not scenes:
                return []
            scenes = scenes["widgets"]["widgetList"][0]["items"]["itemList"]
        except (KeyError, IndexError):
            return []

        chapters = []
        for scene in scenes:
            chapter_title = scene["textMap"]["PRIMARY"]
            match = re.search(r"(\d+\. |)(.+)", chapter_title)
            if match:
                chapter_title = match.group(2)
            timecode = scene["textMap"]["TERTIARY"].replace("Starts at ", "")
            chapters.append(Chapter(name=chapter_title, timestamp=timecode))
        return chapters

    def _check_entitlement(self, title) -> None:
        try:
            base = self.region.get("base_manifest")
            if not base:
                return
            params = {
                "itemId": title.id,
                "presentationScheme": "android-tv-react",
                "deviceTypeID": self.device.get("device_type"),
                "deviceID": self.device_id,
            }
            headers = {"Accept": "application/json"}
            _bearer = self.actor_token or self.device_token
            if _bearer:
                params["roles"] = "playback-envelope-supported"
                headers["Authorization"] = f"Bearer {_bearer}"
            else:
                params["firmware"] = ""
                params["roles"] = "prime-offer-supported,svod-supported"
                params["clientFeatures"] = "EnableBuyBoxV2"

            res = self.session.get(
                url=f"https://{base}/lrcedge/getDataByJavaTransform/v1/lr/detailsPage/detailsPageATF",
                params=params,
                headers=headers,
                timeout=15,
            )
            if res.status_code != 200:
                self.log.debug(f"Entitlement pre-check unavailable ({res.status_code})")
                return

            resource = (res.json() or {}).get("resource", {}) or {}
            message = (
                resource.get("entitlementMessaging", {})
                .get("ENTITLEMENT_MESSAGE_SLOT_DETAIL", {})
                .get("message")
            )
            if message:
                if any(k in message.lower() for k in ("join", "trial", "subscribe", "rent", "buy", "add-on", "add on")):
                    self.log.warning(f" - Entitlement: {message} (this title may need a purchase/add-on)")
                else:
                    self.log.info(f" + Entitlement: {message}")

            apply_hdr = resource.get("applyHdr")
            apply_uhd = resource.get("applyUhd")
            bits = []
            if apply_uhd is not None:
                bits.append(f"UHD {'available' if apply_uhd else 'not available'}")
            if apply_hdr is not None:
                bits.append(f"HDR {'available' if apply_hdr else 'not available'}")
            if bits:
                self.log.info(f" + Amazon advertises: {', '.join(bits)} for this title")
        except Exception as e:
            self.log.debug(f"Entitlement pre-check failed: {e}")

    def _bearer(self) -> Optional[str]:
        if self.living_room and self.actor_token:
            return self.actor_token
        return self.device_token

    def _get_primary_profile(self) -> Optional[str]:
        try:
            base = self.region.get("base_manifest")
            if not base or not self.device_token:
                return None
            res = self.session.get(
                url=f"https://{base}/lrcedge/getDataByJavaTransform/v1/lr/profiles/profileSelection",
                params={"deviceTypeID": self.device.get("device_type"), "deviceID": self.device_id},
                headers={"Authorization": f"Bearer {self.device_token}", "Accept": "application/json"},
                timeout=15,
            ).json()
            profiles = (res.get("resource") or {}).get("profiles") or []
            default = next((p for p in profiles if p.get("isDefaultProfile")), None) or (profiles[0] if profiles else None)
            return default.get("profileId") if default else None
        except Exception as e:
            self.log.debug(f"Profile lookup failed: {e}")
            return None

    def _ensure_actor_token(self) -> Optional[str]:
        if self.actor_token:
            return self.actor_token
        if not self.device_token or not self.device_refresh_token:
            return None

        _profile = self.profile or "default"
        cache = Cacher("AMZN").get(f"actor_token_{_profile}")
        if cache and cache.data and cache.data.get("token") and cache.data.get("expires_in", 0) > int(time.time()):
            self.actor_token = cache.data["token"]
            self.profile_id = cache.data.get("profile_id")
            self.log.debug(" + Using cached actor token")
            return self.actor_token

        try:
            profile_id = self._get_primary_profile()
            if not profile_id:
                self.log.debug(" - No primary profile found for actor token")
                return None
            res = self.session.post(
                url=self.endpoints["token"],
                headers={"Content-Type": "application/json"},
                json={
                    "actor_id": profile_id,
                    "app_name": "AIV",
                    "requested_token_type": "actor_access_token",
                    "source_token_type": "refresh_token",
                    "source_device_tokens": [{
                        "device_type": self.device.get("device_type"),
                        "account_refresh_token": {"token": self.device_refresh_token},
                    }],
                },
            ).json()
            device_tokens = res.get("device_tokens") or []
            token = (device_tokens[0].get("actor_access_token") or {}).get("token") if device_tokens else None
            if not token:
                self.log.debug(f" - Actor token exchange returned no token (Keys: {list(res.keys())})")
                return None
            self.actor_token = token
            self.profile_id = profile_id
            cache.set(
                {"token": token, "profile_id": profile_id, "expires_in": int(time.time()) + 3000},
                int(time.time()) + 3000,
            )
            self.log.info(" + Acquired LivingRoomPlayer actor token")
            return token
        except Exception as e:
            self.log.debug(f"Actor token acquisition failed: {e}")
            return None

    def _livingroom_envelope(self, title) -> Optional[str]:
        token = self._ensure_actor_token()
        if not token:
            return None
        try:
            base = self.region.get("base_manifest")
            res = self.session.get(
                url=f"https://{base}/lrcedge/getDataByJavaTransform/v1/lr/detailsPage/detailsPageATF",
                params={
                    "itemId": title.id,
                    "presentationScheme": "android-tv-react",
                    "deviceTypeID": self.device.get("device_type"),
                    "deviceID": self.device_id,
                    "roles": "playback-envelope-supported",
                },
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=20,
            ).json()
            resource = (res or {}).get("resource") or {}
            for action in resource.get("actions", []) or []:
                pem = (action.get("metadata") or {}).get("playbackExperienceMetadata") or {}
                if pem.get("playbackEnvelope"):
                    self.playbackInfo = {"titleID": title.id, "playbackExperienceMetadata": pem}
                    return pem["playbackEnvelope"]
            self.log.debug(" - LivingRoom detailsPage returned no playback envelope")
        except Exception as e:
            self.log.debug(f"LivingRoom envelope fetch failed: {e}")
        return None

    def playbackEnvelope_data(self, title):
        try:
            res = self.session.get(
                url=self.endpoints["metadata"],
                params={
                    "metadataToEnrich": json.dumps({"placement": "HOVER", "playback": "true", "preroll": "true", "trailer": "true", "watchlist": "true"}),
                    "titleIDsToEnrich": json.dumps([title.id]),
                    "currentUrl": f"https://{self.region['base']}/"
                },
                headers={
                    "device-memory": "8",
                    "x-amzn-requestid": "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(20)),
                    "x-requested-with": "XMLHttpRequest"
                }
            )

            if res.status_code != 200:
                self.log.error(f"Unable to get playbackEnvelope: {res.text}"); raise SystemExit(1)

            data = res.json()
            enrichment = data.get("enrichments", {}).get(title.id, {})
            playback_actions = [
                a for a in enrichment.get("playbackActions", [])
                if a.get("playbackExperienceMetadata", {}).get("playbackEnvelope")
            ]
            if playback_actions:
                pem = playback_actions[0]["playbackExperienceMetadata"]
                self.playbackInfo = {"titleID": title.id, "playbackExperienceMetadata": pem}
                return pem["playbackEnvelope"]

            preroll = enrichment.get("prerollsEnvelope") or {}
            preroll_env = preroll.get("playbackEnvelope")
            if preroll_env:
                self.playbackInfo = {"titleID": title.id, "playbackExperienceMetadata": preroll}
                return preroll_env
            focus = enrichment.get("entitlementCues", {}).get("focusMessage", {}).get("message", "")
            if focus and any(k in focus.lower() for k in ("trial", "rent", "buy", "subscribe", "join")):
                self.log.error(
                    f" - This account is not entitled to '{title}'. Amazon returned: \"{focus}\". "
                    "Account needs a purchase/rental/subscription, or the cookies belong to "
                    "a different account."
                ); raise SystemExit(1)
            self.log.error(
                f" - No playback envelope returned for '{title}' "
                f"(not entitled, or bad cookies). Entitlement cue: {focus or 'none'}"
            ); raise SystemExit(1)
        except SystemExit:
            raise
        except Exception as e:
            self.log.error(f"Unable to get playbackEnvelope: {e}"); raise SystemExit(1)

    def playbackEnvelope_update(self, playbackInfo):
        if not playbackInfo:
            return playbackInfo
        try:
            pem = playbackInfo.get("playbackExperienceMetadata", {}) or {}
            expiry = pem.get("expiryTime", 0)
            if expiry and (int(expiry) / 1000) > time.time():
                return playbackInfo

            correlation_id = pem.get("correlationId")
            title_id = playbackInfo.get("titleID")
            if not correlation_id or not title_id:
                return playbackInfo

            self.log.debug(" + Refreshing expired playback envelope")
            res = self.session.post(
                url=self.endpoints["refreshplayback"],
                params={
                    "deviceID": self.device_id,
                    "deviceTypeID": self.device["device_type"],
                    "gascEnabled": str(self.pv).lower(),
                    "marketplaceID": self.region["marketplace_id"],
                    "uxLocale": "en_EN",
                    "firmware": "1",
                    "version": "1",
                    "nerid": self.generate_nerid(),
                },
                data=json.dumps({
                    "deviceId": self.device_id,
                    "deviceTypeId": self.device["device_type"],
                    "identifiers": {title_id: correlation_id},
                    "geoToken": "null",
                    "identityContext": "null",
                }),
            )
            if res.status_code != 200:
                return playbackInfo
            response_data = res.json().get("response", {})
            if not isinstance(response_data, dict):
                return playbackInfo
            new_pe = response_data.get(title_id, {}).get("playbackExperience")
            if not new_pe:
                return playbackInfo
            new_expiry = new_pe.get("expiryTime")
            if new_expiry:
                new_pe["expiryTime"] = int(new_expiry * 1000)
            refreshed = {"titleID": title_id, "playbackExperienceMetadata": new_pe}
            self.playbackInfo = refreshed
            if new_pe.get("playbackEnvelope"):
                self.playbackEnvelope = new_pe["playbackEnvelope"]
            return refreshed
        except Exception as e:
            self.log.debug(f"Playback envelope refresh failed (non-fatal): {e}")
            return playbackInfo

    def _technologies(self, protocol: str) -> list:
        if protocol == "SmoothStreaming":
            return ["SmoothStreaming"]
        if self.manifest_type_source == ParameterSource.COMMANDLINE and self.manifest_type == "DASH":
            return ["DASH"]
        return ["DASH", "SmoothStreaming"]

    def _build_manifest_payload(self, title, video_codec, bitrate_mode, quality, hdr, protocol,
                                use_playlisted):
        bitrate_adaptations = ["CVBR", "CBR"] if bitrate_mode in ("CVBR+CBR", "CVBR,CBR") else [bitrate_mode]
        range_fmt = self.VIDEO_RANGE_MAP.get(hdr, "None")
        drm_type = "PlayReady" if self.playready else "Widevine"

        if use_playlisted and self.device_token:
            def build_tech():
                return {
                    "bitrateAdaptations": bitrate_adaptations,
                    "codecs": [video_codec],
                    "drmType": drm_type,
                    "drmKeyScheme": "SingleKey" if self.playready else "DualKey",
                    "drmStrength": "L40",
                    "dynamicRangeFormats": [range_fmt],
                    "edgeDeliveryAuthorizationSchemes": ["PVExchangeV1", "Transparent"],
                    "fragmentRepresentations": ["ByteOffsetRange", "SeparateFile"],
                    "frameRates": ["Standard", "High"],
                    "segmentInfoType": "Base",
                    "stitchType": "MultiPeriod",
                    "timedTextRepresentations": ["BurnedIn", "NotInManifestNorStream", "SeparateStreamInManifest"],
                    "trickplayRepresentations": ["NotInManifestNorStream"],
                    "variableAspectRatio": "supported",
                    "vastTimelineType": "Absolute",
                    "manifestThinningToSupportedResolution": "Forbidden",
                }

            supported_techs = self._technologies(protocol)
            techs = {t: build_tech() for t in supported_techs}

            return {
                "globalParameters": {
                    "deviceCapabilityFamily": "LivingRoomPlayer",
                    "playbackEnvelope": self.playbackEnvelope,
                    "capabilityDiscriminators": {"discriminators": {"software": {}, "version": 1}},
                },
                "timedTextUrlsRequest": {"supportedTimedTextFormats": ["TTMLv2", "DFXP"]},
                "transitionTimecodesRequest": {},
                "vodPlaylistedPlaybackUrlsRequest": {
                    "device": {
                        "displayBasedVending": "supported",
                        "displayHeight": 2304,
                        "displayWidth": 4096,
                        "hdcpLevel": "2.3",
                        "maxVideoResolution": "2160p",
                        "category": "Tv",
                        "platform": "Android",
                        "streamingTechnologies": techs,
                        "supportedStreamingTechnologies": supported_techs,
                    },
                    "playbackSettingsRequest": {
                        "firmware": "UNKNOWN",
                        "playerType": self.player,
                        "responseFormatVersion": "1.0.0",
                        "titleId": title.id,
                    },
                },
                "vodXrayMetadataRequest": {
                    "xrayDeviceClass": "normal",
                    "xrayPlaybackMode": "playback",
                    "xrayToken": "XRAY_REIGN_3PLR_2025_V2",
                },
            }

        if not self.device_token:
            if protocol == "SmoothStreaming":
                self.log.warning(" - ISM/SmoothStreaming requires a registered device; using DASH for web playback.")
            global_params = {
                "deviceCapabilityFamily": "WebPlayer",
                "playbackEnvelope": self.playbackEnvelope,
                "capabilityDiscriminators": {
                    "operatingSystem": {"name": "Windows", "version": "10.0"},
                    "middleware": {"name": "EdgeNext", "version": "136.0.0.0"},
                    "nativeApplication": {"name": "EdgeNext", "version": "136.0.0.0"},
                    "hfrControlMode": "Legacy",
                    "displayResolution": {"height": 2304, "width": 4096}
                }
            }
            audit_request = {}
            vod_request = {
                "device": {
                    "hdcpLevel": "2.2" if quality == "UHD" else "1.4",
                    "maxVideoResolution": ("1080p" if quality == "HD" else "480p" if quality == "SD" else "2160p"),
                    "supportedStreamingTechnologies": ["DASH"],
                    "streamingTechnologies": {
                        "DASH": {
                            "bitrateAdaptations": bitrate_adaptations,
                            "codecs": [video_codec],
                            "drmKeyScheme": "SingleKey" if self.playready else "DualKey",
                            "drmType": drm_type,
                            "dynamicRangeFormats": range_fmt,
                            "fragmentRepresentations": ["ByteOffsetRange", "SeparateFile"],
                            "frameRates": ["Standard"],
                            "stitchType": "MultiPeriod",
                            "segmentInfoType": "Base",
                            "timedTextRepresentations": ["NotInManifestNorStream", "SeparateStreamInManifest"],
                            "trickplayRepresentations": ["NotInManifestNorStream"],
                            "variableAspectRatio": "supported"
                        }
                    },
                    "displayWidth": 4096,
                    "displayHeight": 2304
                },
                "ads": {"sitePageUrl": "", "gdpr": {"enabled": "false", "consentMap": {}}},
                "playbackCustomizations": {},
                "playbackSettingsRequest": {
                    "firmware": "UNKNOWN",
                    "playerType": self.player,
                    "responseFormatVersion": "1.0.0",
                    "titleId": title.id
                }
            }
        else:
            global_params = {
                "deviceCapabilityFamily": "AndroidPlayer",
                "playbackEnvelope": self.playbackEnvelope,
                "capabilityDiscriminators": {"discriminators": {"software": {}, "version": 1}}
            }
            audit_request = {"device": {"category": "Tv", "platform": "Android"}}

            def _android_tech():
                return {
                    "bitrateAdaptations": bitrate_adaptations,
                    "codecs": [video_codec],
                    "drmType": drm_type,
                    "drmKeyScheme": "SingleKey" if self.playready else "DualKey",
                    "drmStrength": "L40",
                    "dynamicRangeFormats": [range_fmt],
                    "edgeDeliveryAuthorizationSchemes": ["PVExchangeV1", "Transparent"],
                    "fragmentRepresentations": ["ByteOffsetRange", "SeparateFile"],
                    "frameRates": ["Standard", "High"],
                    "segmentInfoType": "Base",
                    "stitchType": "MultiPeriod",
                    "timedTextRepresentations": ["BurnedIn", "NotInManifestNorStream", "SeparateStreamInManifest"],
                    "trickplayRepresentations": ["NotInManifestNorStream"],
                    "variableAspectRatio": "supported",
                    "vastTimelineType": "Absolute",
                    "manifestThinningToSupportedResolution": "Forbidden"
                }

            technologies = self._technologies(protocol)
            tech_block = {t: _android_tech() for t in technologies}
            vod_request = {
                "ads": {},
                "device": {
                    "displayBasedVending": "supported",
                    "displayHeight": 2304,
                    "displayWidth": 4096,
                    "streamingTechnologies": tech_block,
                    "acceptedCreativeApis": [],
                    "category": "Tv",
                    "hdcpLevel": "2.2",
                    "maxVideoResolution": "2160p",
                    "platform": "Android",
                    "supportedStreamingTechnologies": technologies
                },
                "playbackCustomizations": {},
                "playbackSettingsRequest": {
                    "firmware": "UNKNOWN",
                    "playerType": self.player,
                    "responseFormatVersion": "1.0.0",
                    "titleId": title.id
                }
            }

        return {
            "globalParameters": global_params,
            "auditPingsRequest": audit_request,
            "playbackDataRequest": {},
            "timedTextUrlsRequest": {"supportedTimedTextFormats": ["TTMLv2", "DFXP"]},
            "trickplayUrlsRequest": {},
            "transitionTimecodesRequest": {},
            "vodPlaybackUrlsRequest": vod_request,
            "vodXrayMetadataRequest": {
                "xrayDeviceClass": "normal",
                "xrayPlaybackMode": "playback",
                "xrayToken": "XRAY_WEB_2023_V2"
            }
        }

    @staticmethod
    def _normalize_playlisted_manifest(manifest: dict) -> dict:
        playlisted = manifest.get("vodPlaylistedPlaybackUrls", {}) or {}
        converted = dict(manifest)

        if "error" in playlisted:
            converted["vodPlaybackUrls"] = {"error": playlisted["error"]}
            return converted

        playback_urls = playlisted.get("result", {}).get("playbackUrls", {}) or {}
        playlist = playback_urls.get("intraTitlePlaylist", []) or []
        main = next((x for x in playlist if x.get("type") == "Main"), playlist[0] if playlist else None)

        if not main:
            converted["vodPlaybackUrls"] = {"result": {"playbackUrls": {"urlSets": [], "urlMetadata": {}}}}
            return converted

        url_sets = [
            {"cdn": u.get("cdn"), "url": u.get("url"), "consumptionId": u.get("consumptionId")}
            for u in main.get("urls", []) if u.get("url")
        ]
        converted["vodPlaybackUrls"] = {
            "result": {
                "playbackUrls": {
                    "urlSets": url_sets,
                    "urlMetadata": main.get("manifestMetadata", {}),
                }
            }
        }
        return converted

    def get_manifest(self, title, video_codec, bitrate_mode, quality, hdr, ignore_errors=False,
                     protocol="DASH", use_playlisted=False, retries=3):
        use_playlisted = use_playlisted or self.living_room
        if use_playlisted and not self.device_token:
            self.log.warning(" - Playlisted (LivingRoomPlayer) path needs a registered device. Trying classic run.")
            use_playlisted = False

        for attempt in range(retries):
            if attempt == 0 or not self.playbackInfo:
                lr_env = self._livingroom_envelope(title) if self.living_room else None
                if self.living_room and not lr_env:
                    self.log.warning(" - LivingRoom envelope unavailable. Trying classic run.")
                    self.living_room = False
                    use_playlisted = self.playlisted
                self.playbackEnvelope = lr_env or self.playbackEnvelope_data(title)
            else:
                self.log.debug(f" + Manifest retry {attempt + 1}/{retries}; refreshing playback envelope")
                try:
                    self.playbackInfo["playbackExperienceMetadata"]["expiryTime"] = 0
                except Exception:
                    pass
                self.playbackInfo = self.playbackEnvelope_update(self.playbackInfo)
                self.playbackEnvelope = (
                    (self.playbackInfo or {}).get("playbackExperienceMetadata", {}).get("playbackEnvelope")
                    or self.playbackEnvelope
                )

            data_dict = self._build_manifest_payload(
                title, video_codec, bitrate_mode, quality, hdr, protocol, use_playlisted,
            )

            res = self.session.post(
                url=self.endpoints["playback"],
                params={
                    "deviceID": self.device_id,
                    "deviceTypeID": self.device["device_type"],
                    "gascEnabled": str(self.pv).lower(),
                    "marketplaceID": self.region["marketplace_id"],
                    "uxLocale": "en_EN",
                    "firmware": 1,
                    "titleId": title.id,
                    "nerid": self.generate_nerid(),
                },
                data=json.dumps(data_dict),
                headers={
                    "Authorization": f"Bearer {self._bearer()}" if self._bearer() else None,
                },
            )

            try:
                manifest = res.json()
            except json.JSONDecodeError:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                if ignore_errors:
                    return {}
                self.log.error(f" - Amazon reported an error when obtaining the Playback Manifest\n{res.text}"); raise SystemExit(1)
            if "vodPlaylistedPlaybackUrls" in manifest:
                manifest = self._normalize_playlisted_manifest(manifest)

            vod = manifest.get("vodPlaybackUrls", {})

            if "error" in vod:
                message = vod["error"].get("message", "unknown error")
                if ignore_errors:
                    self.log.warning(f" - {video_codec} manifest error: {message}")
                    return {}
                self.log.error(f" - Amazon reported an error when obtaining the Playback Manifest: {message}"); raise SystemExit(1)
            for resource in ("PlaybackUrls", "AudioVideoUrls"):
                err = manifest.get("errorsByResource", {}).get(resource)
                if err and err.get("errorCode") not in (None, "PRS.NoRights.NotOwned"):
                    detail = f"{err.get('message')} [{err.get('errorCode')}]"
                    if ignore_errors:
                        self.log.warning(f" - {video_codec} {resource} error: {detail}")
                        return {}
                    self.log.error(f" - Amazon had an error with the {resource}: {detail}"); raise SystemExit(1)

            return manifest

        return {}

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return self.config["certificate"]

    def get_widevine_license(self, challenge: bytes, title: Title_T, track, **_) -> str:
        return self._get_license(challenge, title, track, widevine=True)

    def get_playready_license(self, challenge: bytes, title: Title_T, track, **_) -> str:
        return self._get_license(challenge, title, track, widevine=False)

    def _get_license(self, challenge: bytes, title: Title_T, track, widevine: bool):
        if not self.device_token and not self.no_device:
            try:
                self.register_device()
            except Exception:
                pass

        challenge_bytes = challenge if isinstance(challenge, bytes) else challenge.encode("utf-8")
        encoded_challenge = base64.b64encode(challenge_bytes).decode("utf-8")

        amzn = track.data.get("amzn", {}) if isinstance(getattr(track, "data", None), dict) else {}
        envelope = amzn.get("envelope") or self.playbackEnvelope
        handoff = amzn.get("handoff") or self.session_handoff_token
        is_ism = isinstance(getattr(track, "data", None), dict) and "ism" in track.data

        endpoint = self.endpoints["license_wv"] if widevine else self.endpoints["license_pr"]

        if self.device_token:
            data_lic = {
                "playbackEnvelope": envelope,
                "licenseChallenge": encoded_challenge,
                "deviceCapabilityFamily": "LivingRoomPlayer",
                "packagingFormat": "SMOOTH_STREAMING" if is_ism else "MPEG_DASH",
            }
            if widevine:
                data_lic["includeHdcpTestKey"] = True
            if getattr(track, "kid", None):
                try:
                    data_lic["keyId"] = str(uuid.UUID(str(track.kid))).upper()
                except (ValueError, AttributeError):
                    pass
            params = {
                "deviceID": self.device_id,
                "deviceTypeID": self.device.get("device_type", self.config["device_types"]["browser"]),
                "firmware": "1",
                "marketplaceID": self.region["marketplace_id"],
                "titleId": title.id,
                "uxLocale": "en_US",
            }
        else:
            if not handoff:
                self.log.error(" - No device token and no sessionHandoffToken; cannot license."); raise SystemExit(1)
            data_lic = {
                "playbackEnvelope": envelope,
                "licenseChallenge": encoded_challenge,
                "deviceCapabilityFamily": "WebPlayer",
                "sessionHandoffToken": handoff,
            }
            if widevine:
                data_lic["includeHdcpTestKey"] = True
            params = {
                "deviceID": self.device_id,
                "deviceTypeID": self.device["device_type"],
                "gascEnabled": str(self.pv).lower(),
                "marketplaceID": self.region["marketplace_id"],
                "uxLocale": "en_EN",
                "firmware": 1,
                "titleId": title.id,
                "nerid": self.generate_nerid(),
            }

        try:
            resp = self.session.post(
                url=endpoint,
                params=params,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._bearer()}" if self._bearer() else None,
                },
                json=data_lic,
            )
            resp.raise_for_status()
            res = resp.json()
        except requests.exceptions.HTTPError as e:
            msg = "Failed to license"
            status = e.response.status_code if e.response is not None else None
            if e.response is not None:
                try:
                    msg += f": {e.response.json()}"
                except Exception:
                    msg += f": {e.response.text[:200]}"
            self.log.error(f" - {msg}")
            if status in (403, 500):
                self.log.error(f" - {self._license_denial_hint(track)}")
            raise SystemExit(1)
        except Exception as e:
            self.log.error(f" - Failed to license: {e}"); raise SystemExit(1)

        if "errorsByResource" in res:
            error = res["errorsByResource"]
            code = error.get("errorCode") or error.get("type") or "Unknown"
            if code == "PRS.NoRights.AnonymizerIP":
                self.log.error(" - Amazon detected a Proxy/VPN and refused to return a license."); raise SystemExit(1)
            self.log.error(f" - Amazon reported an error during the License request: [{code}]")
            self.log.error(f" - {self._license_denial_hint(track)}")
            raise SystemExit(1)
        if "error" in res:
            self.log.error(f" - License Error: {res['error'].get('message', 'Unknown')}"); raise SystemExit(1)

        primary_key = "widevineLicense" if widevine else "playReadyLicense"
        lic = None
        if isinstance(res.get(primary_key), dict):
            lic = res[primary_key].get("license")
        if not lic:
            for key, val in res.items():
                if "license" in key.lower() and isinstance(val, dict) and val.get("license"):
                    lic = val["license"]; break
                if "license" in key.lower() and isinstance(val, str) and val:
                    lic = val; break
        if not lic:
            self.log.error(
                f" - License response did not contain a '{primary_key}'. "
                f"Response keys: {list(res.keys())}. Raw (truncated): {json.dumps(res)[:600]}"
            )
            self.log.error(f" - {self._license_denial_hint(track)}")
            raise SystemExit(1)
        return base64.b64decode(lic) if isinstance(lic, str) else lic

    def _requested_uhd(self) -> bool:
        q = self.quality[0] if isinstance(self.quality, list) and self.quality else self.quality
        return self.vquality == "UHD" or (isinstance(q, int) and q > 1080)

    def _license_denial_hint(self, track=None) -> str:
        level = None
        try:
            level = getattr(self.cdm, "security_level", None)
        except Exception:
            pass
        cdm_bit = ""
        if level == 3:
            cdm_bit = " Your CDM is Widevine L3 (Amazon licenses only SD to L3)."
        elif level in (2000, 3000):
            cdm_bit = f" Your CDM is PlayReady SL{level}."
        want = []
        if self._requested_uhd() or (isinstance(getattr(track, 'height', None), int) and track.height > 1080):
            want.append("UHD")
        if self.range and self.range != "SDR":
            want.append(self.range)
        want_bit = f" (you requested {'/'.join(want)})" if want else ""
        return (
            f"License failed. CDM robustness limit. {want_bit}.{cdm_bit} "
            "UHD/HDR/DV needs Widevine L1 or PlayReady SL3000. Re-run at a lower quality "
            "(e.g. -q 1080) or SDR to get a licensable stream."
        )

    def _warn_cdm_quality_mismatch(self) -> None:
        try:
            level = getattr(self.cdm, "security_level", None)
            if level == 3 and (self._requested_uhd() or (self.range and self.range != "SDR")):
                self.log.warning(
                    " - Widevine L3 CDM with a UHD/HDR request. Amazon only licenses SD to L3, so the "
                    "license will most likely be denied. For UHD/HDR use Widevine L1 / PlayReady SL3000, or -q 1080 / SDR."
                )
        except Exception:
            pass

    def _tag_amzn_tracks(self, track_iterable, manifest: dict) -> None:
        token = (manifest.get("sessionization") or {}).get("sessionHandoffToken")
        envelope = self.playbackEnvelope
        for t in track_iterable:
            try:
                if not isinstance(t.data, dict):
                    continue
                amzn = t.data.setdefault("amzn", {})
                if token:
                    amzn["handoff"] = token
                if envelope:
                    amzn["envelope"] = envelope
            except Exception:
                pass

    def _post_process_audio(self, audio_tracks) -> None:
        for audio in audio_tracks:
            try:
                data = getattr(audio, "data", None)
                if not isinstance(data, dict) or "dash" not in data:
                    continue
                adaptation_set = data["dash"].get("adaptation_set")
                representation = data["dash"].get("representation")
                if adaptation_set is None:
                    continue

                subtype = (adaptation_set.get("audioTrackSubtype") or "").lower()
                if "descriptive" in subtype:
                    audio.descriptive = True
                if "boosteddialog" in subtype:
                    audio.bitrate = 1
                if not audio.joc:
                    for elem in (adaptation_set, representation):
                        if elem is None:
                            continue
                        try:
                            props = elem.findall("SupplementalProperty") + elem.findall("EssentialProperty")
                        except Exception:
                            props = []
                        for prop in props:
                            scheme = prop.get("schemeIdUri", "")
                            value = (prop.get("value", "") or "").upper()
                            if scheme == "tag:dolby.com,2018:dash:EC3_ExtensionType:2018" and value == "JOC":
                                audio.joc = 16
                                break
                        if audio.joc:
                            break
            except Exception:
                continue

    def manage_session(self, track) -> None:
        try:
            handoff = None
            if isinstance(getattr(track, "data", None), dict):
                handoff = (track.data.get("amzn") or {}).get("handoff")
            handoff = handoff or self.session_handoff_token
            if not handoff or not isinstance(handoff, str):
                self.log.debug("Skipping keep-alive session because no session handoff token is available.")
                return

            refreshed = self.playbackEnvelope_update(self.playbackInfo) or self.playbackInfo or {}
            envelope = (refreshed.get("playbackExperienceMetadata", {}) or {}).get("playbackEnvelope") \
                or self.playbackEnvelope
            if not envelope:
                self.log.debug("Skipping. No playback envelope for keep-alive session.")
                return

            def session_params():
                return {
                    "deviceID": self.device_id,
                    "deviceTypeID": self.device["device_type"],
                    "gascEnabled": str(self.pv).lower(),
                    "marketplaceID": self.region["marketplace_id"],
                    "uxLocale": "en_EN",
                    "firmware": "1",
                    "version": "1",
                    "nerid": self.generate_nerid(),
                }

            headers = {
                "Content-Type": "application/json",
                "accept": "application/json",
                "x-request-priority": "CRITICAL",
                "x-retry-count": "0",
            }

            progress = round(random.uniform(0, 10), 6)
            step = 3
            start_time = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"

            res = self.session.post(
                url=self.endpoints["opensession"],
                params=session_params(),
                headers=headers,
                json={
                    "sessionHandoff": handoff,
                    "playbackEnvelope": envelope,
                    "streamInfo": {
                        "eventType": "START",
                        "streamUpdateTime": progress,
                        "vodProgressInfo": {
                            "currentProgressTime": f"PT{progress:.6f}S",
                            "timeFormat": "ISO8601DURATION",
                        },
                    },
                    "userWatchSessionId": str(uuid.uuid4()),
                },
            )
            if res.status_code != 200:
                self.log.debug(f"Keep-alive: unable to open session ({res.status_code})")
                return
            session_token = res.json().get("sessionToken")
            if not session_token:
                return

            time.sleep(step)
            update_time = (datetime.fromisoformat(start_time[:-1]) + timedelta(seconds=step)) \
                .isoformat(timespec="milliseconds") + "Z"

            res = self.session.post(
                url=self.endpoints["updatesession"],
                params=session_params(),
                headers=headers,
                json={
                    "sessionToken": session_token,
                    "streamInfo": {
                        "eventType": "PAUSE",
                        "streamUpdateTime": update_time,
                        "vodProgressInfo": {
                            "currentProgressTime": f"PT{progress + step:.6f}S",
                            "timeFormat": "ISO8601DURATION",
                        },
                    },
                },
            )
            if res.status_code != 200:
                self.log.debug(f"Keep-alive: unable to update session ({res.status_code})")
                return
            session_token = res.json().get("sessionToken", session_token)

            res = self.session.post(
                url=self.endpoints["closesession"],
                params=session_params(),
                headers=headers,
                json={
                    "sessionToken": session_token,
                    "streamInfo": {
                        "eventType": "STOP",
                        "streamUpdateTime": update_time,
                        "vodProgressInfo": {
                            "currentProgressTime": f"PT{progress + step:.6f}S",
                            "timeFormat": "ISO8601DURATION",
                        },
                    },
                },
            )
            if res.status_code == 200:
                self.log.info(" + True-region playback session completed")
            else:
                self.log.debug(f"Keep-alive: unable to close session ({res.status_code})")
        except Exception as e:
            self.log.debug(f"Keep-alive session failed: {e}")

    @staticmethod
    def _usable_manifest(manifest: dict) -> bool:
        return bool(
            (manifest or {}).get("vodPlaybackUrls", {}).get("result", {})
            .get("playbackUrls", {}).get("urlSets")
        )

    def choose_manifest(self, manifest: dict, cdn=None):
        if not manifest or "vodPlaybackUrls" not in manifest:
            return {}

        url_sets = (
            manifest["vodPlaybackUrls"].get("result", {}).get("playbackUrls", {}).get("urlSets", [])
        )
        if not url_sets:
            return {}

        if cdn:
            cdn = cdn.lower()
            return next((x for x in url_sets if (x.get("cdn") or "").lower() == cdn), {})
        akamai = [x for x in url_sets if "akamai" in (x.get("cdn") or "").lower()]
        if akamai:
            return secrets.choice(akamai)
        return secrets.choice(url_sets)

    @staticmethod
    def generate_nerid(length=24):
        chars = string.ascii_letters + string.digits
        return "".join(secrets.choice(chars) for _ in range(length))

    @staticmethod
    def clean_mpd_url(mpd_url, optimise=False):
        if optimise:
            return mpd_url.replace("~", "") + "?encoding=segmentBase"
        if match := re.match(r"(https?://.*/)d.?/.*~/(.*)", mpd_url):
            mpd_url = "".join(match.groups())
        else:
            try:
                mpd_url = "".join(
                    re.split(r"(?i)(/)", mpd_url)[:5] + re.split(r"(?i)(/)", mpd_url)[9:]
                )
            except IndexError:
                pass
        return mpd_url

    def get_region(self) -> dict:
        domain_region = self.get_domain_region()
        if not domain_region:
            return {}

        region = self.config["regions"].get(domain_region)
        if not region:
            self.log.error(f" - There's no region configuration data for the region: {domain_region}"); raise SystemExit(1)

        region["code"] = domain_region

        if self.pv:
            res = self.session.get("https://www.primevideo.com").text
            match = re.search(r'ue_furl *= *([\'"])fls-(na|eu|fe)\.amazon\.[a-z.]+\1', res)
            if match:
                pv_region = match.group(2).lower()
            else:
                self.log.error(" - Failed to get PrimeVideo region"); raise SystemExit(1)
            pv_region = {"na": "atv-ps"}.get(pv_region, f"atv-ps-{pv_region}")
            region["base_manifest"] = f"{pv_region}.primevideo.com"
            region["base"] = "www.primevideo.com"

        return region

    def get_domain_region(self):
        tlds = [tldextract.extract(x.domain) for x in self.session.cookies if x.domain_specified]
        tld = next((x.suffix for x in tlds if x.domain.lower() in ("amazon", "primevideo")), None)
        if tld:
            tld = tld.split(".")[-1]
        region = {"com": "us", "uk": "gb"}.get(tld, tld)

        if region == "us":
            lc_cookie = next(
                (x.value for x in self.session.cookies
                 if x.name in ("lc-main-av", "lc-main") and x.domain_specified),
                None
            )
            if lc_cookie:
                parts = lc_cookie.replace("-", "_").split("_")
                if len(parts) >= 2:
                    country = parts[-1].lower()
                    if country not in ("us", ""):
                        mapped = {"uk": "gb"}.get(country, country)
                        if mapped in self.config.get("regions", {}):
                            region = mapped

        return region

    def prepare_endpoint(self, name: str, uri: str, region: dict) -> str:
        if name in ("playback", "license_wv", "license_pr", "xray",
                    "refreshplayback", "opensession", "updatesession", "closesession",
                    "configuration"):
            return f"https://{region['base_manifest']}{uri}"
        if name in ("ontv", "devicelink", "details", "getDetailWidgets", "metadata"):
            if self.pv:
                host = "www.primevideo.com"
            else:
                host = f"{region['base']}/gp/video" if name == "metadata" else region["base"]
            return f"https://{host}{uri}"
        if name in ("codepair", "register", "token"):
            base_api = region.get("base_api") or self.config["regions"]["us"]["base_api"]
            return f"https://{base_api}{uri}"
        raise ValueError(f"Unknown endpoint: {name}")

    def prepare_endpoints(self, endpoints: dict, region: dict) -> dict:
        return {k: self.prepare_endpoint(k, v, region) for k, v in endpoints.items()}

    def register_device(self) -> None:
        _profile = self.profile or "default"
        self.device = dict((self.config.get("device") or {}).get(_profile, {}))

        identity_cache = Cacher("AMZN")
        identity_key = f"device_identity_{_profile}"
        cached_identity = identity_cache.get(identity_key)

        if cached_identity and cached_identity.data:
            identity = cached_identity.data
            self.log.debug(" + Using cached device identity")
        else:
            unique_serial = secrets.token_hex(8)
            base_name = self.device.get("device_name", "%FIRST_NAME%'s Shield TV")
            clean_name = re.sub(r"%DUPE_STRATEGY[^%]*%", "", base_name).rstrip()
            suffix = secrets.token_hex(2).upper()
            unique_name = f"{clean_name}-{suffix}"
            identity = {"device_serial": unique_serial, "device_name": unique_name}
            cached_identity = identity_cache.get(identity_key)
            cached_identity.set(identity, int(time.time()) + 60 * 60 * 24 * 3650)
            self.log.info(f" + Generated unique device identity: serial={unique_serial}, name={unique_name!r}")

        self.device["device_serial"] = identity["device_serial"]
        self.device["device_name"] = identity["device_name"]

        device_hash = hashlib.md5(json.dumps(self.device, sort_keys=True).encode()).hexdigest()[0:6]
        device_cache_path = f"device_tokens_{_profile}_{device_hash}"

        _reg = self.DeviceRegistration(
            device=self.device,
            endpoints=self.endpoints,
            log=self.log,
            cache_path=device_cache_path,
            session=self.session
        )
        self.device_token = _reg.bearer
        self.device_refresh_token = _reg.refresh_token

        self.device_id = self.device.get("device_serial")
        if not self.device_id:
            self.log.error(f" - A device serial is required in the config, try: {os.urandom(8).hex()}"); raise SystemExit(1)

    class DeviceRegistration:
        def __init__(self, device: dict, endpoints: dict, cache_path: str, session: requests.Session, log):
            self.session = session
            self.device = device
            self.endpoints = endpoints
            self.cache_path = cache_path
            self.log = log
            self.cache = Cacher("AMZN")

            self.device = {k: str(v) if not isinstance(v, str) else v for k, v in self.device.items()}
            self.bearer = None
            self.refresh_token = None

            cached_data = self.cache.get(self.cache_path)

            if cached_data and cached_data.data:
                if cached_data.data.get("expires_in", 0) > int(time.time()):
                    self.log.info(" + Using cached device bearer")
                    self.bearer = cached_data.data["access_token"]
                    self.refresh_token = cached_data.data.get("refresh_token")
                else:
                    self.log.info("Cached device bearer expired, refreshing...")
                    refresh_token = cached_data.data.get("refresh_token")
                    refreshed_tokens = self.refresh(self.device, refresh_token) if refresh_token else None
                    if refreshed_tokens and refreshed_tokens.get("access_token"):
                        refreshed_tokens["refresh_token"] = refreshed_tokens.get("refresh_token") or refresh_token
                        expires_seconds = int(refreshed_tokens.get("expires_in") or 3600)
                        refreshed_tokens["expires_in"] = int(time.time()) + expires_seconds
                        cached_data.set(refreshed_tokens, refreshed_tokens["expires_in"])
                        self.bearer = refreshed_tokens["access_token"]
                        self.refresh_token = refreshed_tokens.get("refresh_token")
                    else:
                        self.log.info(" + Re-registering device bearer")
                        self.bearer = self.register(self.device)
            else:
                self.log.info(" + Registering new device bearer")
                self.bearer = self.register(self.device)

        def register(self, device: dict) -> str:
            code_pair = self.get_code_pair(device)
            public_code = code_pair["public_code"]

            self.log.info(f" + Visit https://www.primevideo.com/mytv and enter code: {public_code}")
            self.log.info("    Waiting for authorisation (May take up to 5 minutes)")

            interval = 10
            deadline = int(time.time()) + 300

            while int(time.time()) < deadline:
                res = self.session.post(
                    url=self.endpoints["register"],
                    headers={"Content-Type": "application/json", "Accept-Language": "en-US"},
                    json={
                        "auth_data": {"code_pair": code_pair},
                        "registration_data": device,
                        "requested_token_type": ["bearer"],
                        "requested_extensions": ["device_info", "customer_info"]
                    },
                    cookies=None
                )
                data = res.json()

                if res.status_code == 200 and "success" in data.get("response", {}):
                    break

                error_code = data.get("response", {}).get("error", {}).get("code", "")
                if error_code == "Unauthorized":
                    time.sleep(interval)
                    continue
                else:
                    self.log.error(f"Unable to register: {res.text}"); raise SystemExit(1)
            else:
                self.log.error("Device registration timed out. Code not approved in time."); raise SystemExit(1)

            bearer = data["response"]["success"]["tokens"]["bearer"]
            expires_val = bearer.get("expires_in", 3600)
            if isinstance(expires_val, dict):
                expires_val = expires_val.get("value", 3600)
            bearer_data = {
                "access_token": bearer["access_token"],
                "refresh_token": bearer.get("refresh_token", ""),
                "expires_in": int(time.time()) + int(expires_val),
            }
            keyed_cache = self.cache.get(self.cache_path)
            keyed_cache.set(bearer_data, int(time.time()) + int(expires_val))

            self.refresh_token = bearer_data["refresh_token"]
            self.log.info(" + Device registered and token cached successfully")
            return bearer_data["access_token"]

        def refresh(self, device: dict, refresh_token: str):
            try:
                res = self.session.post(
                    url=self.endpoints["token"],
                    json={
                        "app_name": device["app_name"],
                        "app_version": device["app_version"],
                        "source_token_type": "refresh_token",
                        "source_token": refresh_token,
                        "requested_token_type": "access_token"
                    }
                ).json()
            except Exception as e:
                self.log.warning(f"Device token refresh request failed ({e}); re-registering.")
                return None

            if "error" in res:
                self.log.warning(
                    f"Could not refresh device token ({res.get('error_description') or res.get('error')}); re-registering."
                )
                return None

            return res

        def get_csrf_token(self) -> str:
            res = self.session.get(self.endpoints["ontv"])
            if 'name="appAction" value="SIGNIN"' in res.text or "SIGNIN_PWD_COLLECT" in res.text:
                self.log.error("Cookies are signed out, cannot get ontv CSRF token.")
                raise SystemExit(1)
            for match in re.finditer(r'<script type="text/template">(.+?)</script>', res.text, re.DOTALL):
                try:
                    prop = json.loads(match.group(1))
                    token = prop.get("props", {}).get("codeEntry", {}).get("token")
                    if token:
                        return token
                    token = prop.get("codeEntry", {}).get("token")
                    if token:
                        return token
                except Exception:
                    pass
            ce_idx = res.text.find('"codeEntry"')
            if ce_idx != -1:
                snippet = res.text[ce_idx:ce_idx + 2000]
                m2 = re.search(r'"token"\s*:\s*"([^"]+)"', snippet)
                if m2:
                    return m2.group(1)
            self.log.error("Unable to get ontv CSRF token")
            raise SystemExit(1)

        def get_code_pair(self, device: dict) -> dict:
            res = self.session.post(
                url=self.endpoints["codepair"],
                headers={"Content-Type": "application/json", "Accept-Language": "en-US"},
                json={"code_data": device}
            ).json()
            if "error" in res:
                self.log.error(f"Unable to get code pair: {res['error']}"); raise SystemExit(1)
            return res

    def parse_title(self, ctx, title):
        title = title or ctx.parent.params.get("title")
        if not title:
            self.log.error(" - No title ID specified")
        if not getattr(self, "TITLE_RE"):
            self.title = title
            return {}
        for regex in as_list(self.TITLE_RE):
            m = re.search(regex, title)
            if m:
                self.title = m.group("id")
                return m.groupdict()
        self.log.warning(f" - Unable to parse title ID {title!r}, using as-is")
        self.title = title
