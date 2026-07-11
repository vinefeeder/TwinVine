import base64
import hashlib
import json
import os
import re
import secrets
import string
import time
import random
from collections import defaultdict
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Optional, Literal, Union
from urllib.parse import quote, urlencode

import click
import jsonpickle
import requests
from click.core import ParameterSource
from langcodes import Language
from tldextract import tldextract

from envied.core.cacher import Cacher
from envied.core.credential import Credential
from envied.core.manifests import DASH, ISM
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Subtitle, Tracks, Track, Video
from envied.core.utilities import is_close_match
from envied.core.utils.collections import as_list




def _build_ordered_lang_map_from_mpd(mpd_text: str) -> dict:
    import xml.etree.ElementTree as ET
    import re as _re
    ns_strip = _re.compile(r'\{[^}]*\}')
    rid_lang_re = _re.compile(r'^audio_([a-zA-Z]{2,3}-[a-zA-Z0-9]{2,5})')
    result: dict = {}
    try:
        root = ET.fromstring(mpd_text)
        for elem in root.iter():
            if ns_strip.sub('', elem.tag) != 'AdaptationSet':
                continue
            content_type = elem.get('contentType', '') or elem.get('mimeType', '')
            if 'audio' not in content_type.lower():
                if not any('audio' in (r.get('mimeType', '')).lower() for r in elem):
                    continue
            base_lang = elem.get('lang') or elem.get('language') or ''
            if not base_lang:
                continue
            for r in elem:
                if ns_strip.sub('', r.tag) != 'Representation':
                    continue
                rid = r.get('id') or ''
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
        if not any('-' in p for p in ordered):
            continue
        idx = counters.get(base, 0)
        if idx < len(ordered):
            track.language = _Lang.get(ordered[idx])
        counters[base] = idx + 1

class AMZN(Service):
    """
    Service code for Amazon VOD (https://amazon.com) and Amazon Prime Video (https://primevideo.com).

    \b
    Authorization: Cookies
    Security: UHD@L1 FHD@L3(ChromeCDM) SD@L3, Maintains their own license server like Netflix, be cautious.

    \b
    Region is chosen automatically based on domain extension found in cookies.
    Prime Video specific code will be run if the ASIN is detected to be a prime video variant.
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
                  help="CDN to download from, defaults to the CDN with the highest weight set by Amazon.")
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
    @click.option("-aa", "--atmos", is_flag=True, default=False,
                  help="Prefer Atmos audio if available.")
    @click.option("-drm", "--drm-system", type=click.Choice(["widevine", "playready"], case_sensitive=False),
                  default="playready",
                  help="which drm system to use")
    @click.pass_context
    def cli(ctx, **kwargs):
        return AMZN(ctx, **kwargs)

    def __init__(self, ctx, title, bitrate: str, player: str, cdn: str, vquality: str, single: bool,
                 amanifest: str, aquality: str, drm_system: str, atmos: bool):
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
        self.atmos = atmos
        self.drm_system = drm_system

        assert ctx.parent is not None

        # envied.params
        self.chapters_only = ctx.parent.params.get("chapters_only")
        self.quality = ctx.parent.params.get("quality") or 1080
        
        vcodec = ctx.parent.params.get("vcodec")
        range_ = ctx.parent.params.get("range_")
        
        self.range = range_[0].name if range_ else "SDR"
        # Mapping envied.video codec enum to string
        self.vcodec = "H265" if vcodec and "HEVC" in str(vcodec) else "H264"

        self.cdm = ctx.obj.cdm
        self.profile = ctx.obj.profile
        self.playready = self.drm_system == "playready"

        self.region: dict[str, str] = {}
        self.endpoints: dict[str, str] = {}
        self.device: dict[str, str] = {}

        self.pv = False
        self.event = False
        self.device_token = None
        self.device_id = None
        self.customer_id = None
        self.client_id = "f22dbddb-ef2c-48c5-8876-bed0d47594fd"  # browser client id
        self.playbackEnvelope = None

        # Logic from Vinetrimmer regarding quality overrides
        if self.vquality_source != ParameterSource.COMMANDLINE:
            # Check if quality is list (envied. or int
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
            if self.vcodec == "H265" and self.range == "SDR" and self.bitrate != "CVBR+CBR":
                self.bitrate = "CVBR+CBR"
                self.log.info(" + Changed bitrate mode to CVBR+CBR to be able to get H.265 SDR video track")

            if self.vquality == "UHD" and self.range != "SDR" and self.bitrate != "CBR":
                self.bitrate = "CBR"
                self.log.info(f" + Changed bitrate mode to CBR to be able to get highest quality UHD {self.range} video track")

        self.orig_bitrate = self.bitrate

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")
        
        self.session.cookies.update(cookies)
        self.configure()

    def configure(self) -> None:
        if len(self.title) > 10:
            self.pv = True
        self.pv = True  # always use primevideo endpoints

        self.log.info("Getting Account Region")
        self.region = self.get_region()
        if not self.region:
            self.log.error(" - Failed to get Amazon Account region"); raise SystemExit(1)
        
        self.log.info(f" + Region: {self.region['code']}")

        # endpoints must be prepared AFTER region data is retrieved
        self.endpoints = self.prepare_endpoints(self.config["endpoints"], self.region)

        self.session.headers.update({
            "Origin": f"https://{self.region['base']}"
        })

        _profile = self.profile or "default"
        self.device = (self.config.get("device") or {}).get(_profile, {})
        
        # Logic to decide if we need a specific device registration
        need_device = False
        if (isinstance(self.quality, list) and self.quality[0] > 1080) or self.vquality == "UHD" or self.range != "SDR":
            need_device = True
        
        if self.device:
            if need_device and self.vcodec == "H265":
                self.log.info(f"Using device to get UHD manifests")
            else:
                self.log.info(f"Using configured device for profile: {_profile}")
            self.register_device()
        else:
            # Falling back to browser-based device ID
            self.log.warning(
                "No Device information was provided for %s, using browser device...",
                self.profile
            )
            self.device_id = hashlib.sha224(
                ("CustomerID" + self.session.headers["User-Agent"]).encode("utf-8")
            ).hexdigest()
            self.device = {"device_type": self.config["device_types"]["browser"]}

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
        else:
            # TV Show logic with pagination support from Vinetrimmer
            episodes_list = []
            seasons = [x.get("titleID") for x in data["seasonSelector"]]

            # If single flag is set, logic to filter seasons happens in main loop, 
            # but for envied.structure we usually return the whole series or let user filter.
            # Implementing the Vinetrimmer pagination logic:
            
            for season in seasons:
                # If single mode and logic requires skipping, we should handle it here
                # But strict Vinetrimmer logic handled title switching. 
                # For envied. we iterate all found seasons.
                
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
                # exit(product_details_season)
                
                # Process initial batch
                for episode in episode_data_list:
                    details = episode["detail"]
                    episodes_list.append(Episode(
                        id_=details["catalogId"],
                        title=product_details["title"],
                        name=details["title"],
                        season=product_details_season["seasonNumber"],
                        number=episode["self"]["sequenceNumber"],
                        service=self.__class__,
                        data=episode
                    ))

                # Handle Pagination
                pagination_data = res.get('episodeList', {}).get('actions', {}).get('pagination', [])
                token = next((quote(item.get('token')) for item in pagination_data if item.get('tokenType') == 'NextPage'), None)
                
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
                    
                    episodeListWidget = res_page['widgets'].get('episodeList', {})
                    for item in episodeListWidget.get('episodes', []):
                        ep_num = int(item.get('self', {}).get('sequenceNumber', 0))
                        episodes_list.append(Episode(
                            id_=item["detail"]["catalogId"],
                            name=item["detail"]["title"],
                            season=product_details_season["seasonNumber"],
                            number=ep_num,
                            service=self.__class__,
                            data=item
                        ))

                    pagination_data = res_page['widgets'].get('episodeList', {}).get('actions', {}).get('pagination', [])
                    token = next((quote(item.get('token')) for item in pagination_data if item.get('tokenType') == 'NextPage'), None)

            return Series(episodes_list)

    def get_tracks(self, title: Title_T) -> Tracks:
        if self.chapters_only:
            return Tracks([])

        # Main Video Manifest
        # When DV is requested, declare HEVC_DOLBY_VISION codec so Amazon returns DV streams
        # If the server rejects the DV request, fall back to HDR10 automatically
        effective_vcodec = "HEVC_DOLBY_VISION" if self.range == "DV" and self.vcodec == "H265" else self.vcodec
        effective_range = self.range
        manifest = self.get_manifest(
            title,
            video_codec=effective_vcodec,
            bitrate_mode=self.bitrate,
            quality=self.vquality,
            hdr=effective_range,
            ignore_errors=self.range == "DV"
        )
        if self.range == "DV" and not manifest.get("vodPlaybackUrls"):
            self.log.warning(" - Dolby Vision request rejected by server, retrying with HDR10...")
            effective_vcodec = self.vcodec
            effective_range = "HDR10"
            manifest = self.get_manifest(
                title,
                video_codec=effective_vcodec,
                bitrate_mode=self.bitrate,
                quality=self.vquality,
                hdr=effective_range,
                ignore_errors=False
            )

        if "rightsException" in manifest.get("returnedTitleRendition", {}).get("selectedEntitlement", {}):
            self.log.error(" - The profile used does not have the rights to this title.")
            return Tracks([])

        chosen_manifest = self.choose_manifest(manifest, self.cdn)
        if not chosen_manifest:
            self.log.error(f"No manifests available"); raise SystemExit(1)

        manifest_url = self.clean_mpd_url(chosen_manifest["url"], False)
        if self.event:
            devicetype = self.device.get("device_type")
            manifest_url = chosen_manifest["url"]
            manifest_url = f"{manifest_url}?amznDtid={devicetype}&encoding=segmentBase"
        
        self.log.info(" + Downloading Manifest")

        _mpd_raw = self.session.get(manifest_url).text
        _lang_order_map = _build_ordered_lang_map_from_mpd(_mpd_raw)
        self.log.info(f" + MPD language map: {sum(len(v) for v in _lang_order_map.values())} representations indexed")

        streamingProtocol = manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
        
        # Base Tracks object
        tracks = Tracks()

        if streamingProtocol == "DASH":
            tracks = Tracks([
                x for x in iter(DASH.from_url(url=manifest_url, session=self.session).to_tracks(language="en"))
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

        # Logic for separate audio (Higher Quality / Different Codec)
        need_separate_audio = ((self.aquality or self.vquality) != self.vquality
                               or self.amanifest == "CVBR" and (self.vcodec, self.bitrate) != ("H264", "CVBR")
                               or self.amanifest == "CBR" and (self.vcodec, self.bitrate) != ("H264", "CBR")
                               or self.amanifest == "H265" and self.vcodec != "H265"
                               or self.amanifest != "H265" and self.vcodec == "H265")

        if not need_separate_audio:
            # Check for low bitrate audio
            audios = defaultdict(list)
            for audio in tracks.audio:
                audios[audio.language].append(audio)
            for lang in audios:
                if not any((x.bitrate or 0) >= 640000 for x in audios[lang]):
                    need_separate_audio = True
                    break

        if need_separate_audio and not self.atmos:
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
                    audio_mpd_url = chosen_audio_manifest["url"]
                    audio_mpd_url = f"{audio_mpd_url}?amznDtid={devicetype}&encoding=segmentBase"
                
                self.log.info(" + Downloading Audio Manifest")
                try:
                    audio_protocol = audio_manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
                    if audio_protocol == "DASH":
                        _a_raw = self.session.get(audio_mpd_url).text
                        _a_lang_order = _build_ordered_lang_map_from_mpd(_a_raw)
                        self.log.info(f" + Audio MPD language map: {sum(len(v) for v in _a_lang_order.values())} entries")
                        audio_tracks = DASH.from_url(url=audio_mpd_url, session=self.session).to_tracks(language="en")
                        if _a_lang_order:
                            _apply_ordered_lang_map(audio_tracks.audio, _a_lang_order)
                    elif audio_protocol == "SmoothStreaming":
                        audio_tracks = ISM.from_url(url=audio_mpd_url, session=self.session).to_tracks(language="en")
                    else:
                        audio_tracks = Tracks([])
                    
                    tracks.add(audio_tracks.audio, warn_only=True)
                except Exception as e:
                     self.log.warning(f" - Failed to parse audio manifest: {e}")

        # Logic for UHD/Atmos Audio
        need_uhd_audio = self.atmos
        if not self.amanifest and ((self.aquality == "UHD" and self.vquality != "UHD") or not self.aquality):
             # Simple check if current tracks lack high bitrate
             if all((x.bitrate or 0) < 640000 for x in tracks.audio):
                 need_uhd_audio = True

        if need_uhd_audio and (self.config.get("device") or {}).get(self.profile, None):
            self.log.info("Getting audio from UHD manifest for potential higher bitrate or better codec")
            temp_device = self.device
            temp_token = self.device_token
            temp_id = self.device_id
            
            # Switch to device if on browser/playready
            if self.playready or self.cdm.device.type == "CHROME":
                 self.register_device() # Switch to registered device context

            try:
                uhd_audio_manifest = self.get_manifest(
                    title=title,
                    video_codec="H265",
                    bitrate_mode="CVBR+CBR",
                    quality="UHD",
                    hdr="DV", # Needed for 576kbps Atmos
                    ignore_errors=True
                )
            except:
                uhd_audio_manifest = None
            
            # Restore context
            self.device = temp_device
            self.device_token = temp_token
            self.device_id = temp_id

            if uhd_audio_manifest and (chosen_uhd := self.choose_manifest(uhd_audio_manifest, self.cdn)):
                uhd_url = self.clean_mpd_url(chosen_uhd["url"], optimise=False)
                self.log.info(" + Downloading UHD Manifest")
                try:
                    uhd_prot = uhd_audio_manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
                    if uhd_prot == "DASH":
                         uhd_tracks = DASH.from_url(url=uhd_url, session=self.session).to_tracks(language="en")
                    elif uhd_prot == "SmoothStreaming":
                         uhd_tracks = ISM.from_url(url=uhd_url, session=self.session).to_tracks(language="en")
                    else:
                         uhd_tracks = Tracks([])

                    # If atmos found, replace
                    if any(x for x in uhd_tracks.audio if "atmos" in (x.codec or "").lower() or x.channels >= 6):
                         tracks.add(uhd_tracks.audio, warn_only=True)
                except Exception:
                    pass

        # Post-process video tracks (HDR info)
        actual_range = manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["dynamicRange"]
        for video in tracks.videos:
             video.hdr10 = actual_range == "Hdr10"
             video.dv = actual_range == "DolbyVision"

        if self.range == "DV" and actual_range != "DolbyVision":
            friendly = {"Hdr10": "HDR10", "None": "SDR"}.get(actual_range, actual_range)
            self.log.warning(f" - Dolby Vision not available for this title/region. Server returned: {friendly}")

        # Subtitles
        for sub in manifest.get("timedTextUrls", {}).get("result", {}).get("subtitleUrls", []) + manifest.get("timedTextUrls", {}).get("result", {}).get("forcedNarrativeUrls", []):
            url = sub["url"]

            url_path = url.split("?")[0]
            url_ext = os.path.splitext(url_path)[1].lstrip(".").lower()
            codec_map = {
                "ttml": Subtitle.Codec.TimedTextMarkupLang,
                "dfxp": Subtitle.Codec.TimedTextMarkupLang,
                "vtt": Subtitle.Codec.WebVTT,
                "srt": Subtitle.Codec.SubRip,
            }
            detected_codec = codec_map.get(url_ext, Subtitle.Codec.TimedTextMarkupLang)

            sub_obj = Subtitle(
                id_=f"{sub['trackGroupId']}_{sub['languageCode']}_{sub['type']}_{sub['subtype']}",
                url=url,
                codec=detected_codec,
                language=sub["languageCode"],
                forced="ForcedNarrative" in sub["type"],
                sdh=sub["type"].lower() == "sdh"
            )

            tracks.add(sub_obj, warn_only=True)

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        manifest = self.get_manifest(
            title,
            video_codec=self.vcodec,
            bitrate_mode=self.bitrate,
            quality=self.vquality,
            hdr=self.range
        )

        if "vodXrayMetadata" in manifest:
            if "error" in manifest["vodXrayMetadata"]:
                return []
            
            xray_params = {
                "pageId": "fullScreen",
                "pageType": "xray",
                "serviceToken": json.dumps({
                    "consumptionType": "Streaming",
                    "deviceClass": "normal",
                    "playbackMode": "playback",
                    "vcid": json.loads(manifest["vodXrayMetadata"]["result"]["parameters"]["serviceToken"])["vcid"]
                })
            }
        else:
            return []

        xray_params.update({
            "deviceID": self.device_id,
            "deviceTypeID": self.config["device_types"]["browser"],
            "marketplaceID": self.region["marketplace_id"],
            "gascEnabled": str(self.pv).lower(),
            "decorationScheme": "none",
            "version": "inception-v2",
            "uxLocale": "en-US",
            "featureScheme": "XRAY_WEB_2020_V1"
        })

        try:
            xray = self.session.get(
                url=self.endpoints["xray"],
                params=xray_params
            ).json().get("page")
        except:
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

    def playbackEnvelope_data(self, title):
        try:
            res = self.session.get(
                url=self.endpoints["metadata"],
                params={
                    'metadataToEnrich': json.dumps({"placement": "HOVER", "playback": "true", "preroll": "true", "trailer": "true", "watchlist": "true"}),
                    'titleIDsToEnrich': json.dumps([title.id])
                },
                headers={'x-requested-with': 'XMLHttpRequest'}
            )
            
            if res.status_code == 200:
                try:
                    data = res.json()
                    if (
                        title.id in data["enrichments"]
                        and "focusMessage" in data["enrichments"][title.id].get("entitlementCues", {})
                        and data["enrichments"][title.id]["entitlementCues"]["focusMessage"].get("message") == "Watch with a free Prime trial"
                    ):
                        self.log.error("Invalid Cookies"); raise SystemExit(1)

                    try:
                        playbackEnvelope = data["enrichments"][title.id]["playbackActions"][0]["playbackExperienceMetadata"]["playbackEnvelope"]
                    except:
                        playbackEnvelope = data['enrichments'][title.id]['prerollsEnvelope']['playbackEnvelope']
                    return playbackEnvelope
                except Exception as e:
                    self.log.error(f"Unable to get playbackEnvelope: {e}"); raise SystemExit(1)
            else:
                self.log.error(f"Unable to get playbackEnvelope: {res.text}"); raise SystemExit(1)
        except Exception:
            self.log.error("Unable to get playbackEnvelope"); raise SystemExit(1)

    def get_manifest(self, title, video_codec, bitrate_mode, quality, hdr, ignore_errors=False):
        self.playbackEnvelope = self.playbackEnvelope_data(title)

        # Construct Payload (Vinetrimmer Style)
        data_dict = {
            "globalParameters": {
                "deviceCapabilityFamily": "WebPlayer" if not self.device_token else "AndroidPlayer",
                "playbackEnvelope": self.playbackEnvelope,
                "capabilityDiscriminators": {
                    "operatingSystem": {"name": "Windows", "version": "10.0"},
                    "middleware": {"name": "EdgeNext", "version": "136.0.0.0"},
                    "nativeApplication": {"name": "EdgeNext", "version": "136.0.0.0"},
                    "hfrControlMode": "Legacy",
                    "displayResolution": {"height": 2304, "width": 4096}
                } if not self.device_token else {
                    "discriminators": {"software": {}, "version": 1}
                }
            },
            "auditPingsRequest": {
                **({"device": {"category": "Tv", "platform": "Android"}} if self.device_token else {})
            },
            "playbackDataRequest": {},
            "timedTextUrlsRequest": {
                "supportedTimedTextFormats": ["TTMLv2", "DFXP"]
            },
            "trickplayUrlsRequest": {},
            "transitionTimecodesRequest": {},
            "vodPlaybackUrlsRequest": {
                "device": {
                    "hdcpLevel": "2.2" if quality == "UHD" else "1.4",
                    "maxVideoResolution": ("1080p" if quality == "HD" else "480p" if quality == "SD" else "2160p"),
                    "supportedStreamingTechnologies": ["DASH"],
                    "streamingTechnologies": {
                        "DASH": {
                            "bitrateAdaptations": ["CVBR", "CBR"] if bitrate_mode in ("CVBR+CBR", "CVBR,CBR") else [bitrate_mode],
                            "codecs": [video_codec],
                            "drmKeyScheme": "SingleKey" if self.playready else "DualKey",
                            "drmType": "PlayReady" if self.playready else "Widevine",
                            "dynamicRangeFormats": self.VIDEO_RANGE_MAP.get(hdr, "None"),
                            "fragmentRepresentations": ["ByteOffsetRange"],
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
                "ads": {
                    "sitePageUrl": "",
                    "gdpr": {"enabled": "false", "consentMap": {}}
                },
                "playbackCustomizations": {},
                "playbackSettingsRequest": {
                    "firmware": "UNKNOWN",
                    "playerType": self.player,
                    "responseFormatVersion": "1.0.0",
                    "titleId": title.id
                }
            } if not self.device_token else {
                # Android/Device Payload
                "ads": {},
                "device": {
                    "displayBasedVending": "supported",
                    "displayHeight": 2304,
                    "displayWidth": 4096,
                    "streamingTechnologies": {
                        "DASH": {
                            "fragmentRepresentations": ["ByteOffsetRange"],
                            "manifestThinningToSupportedResolution": "Forbidden",
                            "segmentInfoType": "List",
                            "stitchType": "MultiPeriod",
                            "timedTextRepresentations": ["BurnedIn", "NotInManifestNorStream", "SeparateStreamInManifest"],
                            "trickplayRepresentations": ["NotInManifestNorStream"],
                            "variableAspectRatio": "supported",
                            "vastTimelineType": "Absolute",
                            "bitrateAdaptations": ["CVBR", "CBR"] if bitrate_mode in ("CVBR+CBR", "CVBR,CBR") else [bitrate_mode],
                            "codecs": [video_codec],
                            "drmKeyScheme": "SingleKey",
                            "drmStrength": "L40",
                            "drmType": "PlayReady" if self.playready else "Widevine",
                            "dynamicRangeFormats": [self.VIDEO_RANGE_MAP.get(hdr, "None")],
                            "frameRates": ["Standard"]
                        },
                        "SmoothStreaming": {
                            "fragmentRepresentations": ["ByteOffsetRange"],
                            "manifestThinningToSupportedResolution": "Forbidden",
                            "segmentInfoType": "List",
                            "stitchType": "MultiPeriod",
                            "timedTextRepresentations": ["BurnedIn", "NotInManifestNorStream", "SeparateStreamInManifest"],
                            "trickplayRepresentations": ["NotInManifestNorStream"],
                            "variableAspectRatio": "supported",
                            "vastTimelineType": "Absolute",
                            "bitrateAdaptations": ["CVBR", "CBR"] if bitrate_mode in ("CVBR+CBR", "CVBR,CBR") else [bitrate_mode],
                            "codecs": [video_codec],
                            "drmKeyScheme": "SingleKey",
                            "drmStrength": "L40",
                            "drmType": "PlayReady",
                            "dynamicRangeFormats": [self.VIDEO_RANGE_MAP.get(hdr, "None")],
                            "frameRates": ["Standard"]
                        }
                    },
                    "acceptedCreativeApis": [],
                    "category": "Tv",
                    "hdcpLevel": "2.2",
                    "maxVideoResolution": "2160p",
                    "platform": "Android",
                    "supportedStreamingTechnologies": ["DASH", "SmoothStreaming"]
                },
                "playbackCustomizations": {},
                "playbackSettingsRequest": {
                    "firmware": "UNKNOWN",
                    "playerType": self.player,
                    "responseFormatVersion": "1.0.0",
                    "titleId": title.id
                }
            },
            "vodXrayMetadataRequest": {
                "xrayDeviceClass": "normal",
                "xrayPlaybackMode": "playback",
                "xrayToken": "XRAY_WEB_2023_V2"
            }
        }

        json_data = json.dumps(data_dict)

        res = self.session.post(
            url=self.endpoints["playback"],
            params={
                'deviceID': self.device_id,
                'deviceTypeID': self.device["device_type"],
                'gascEnabled': str(self.pv).lower(),
                'marketplaceID': self.region["marketplace_id"],
                'uxLocale': 'en_EN',
                'firmware': 1,
                'titleId': title.id,
                'nerid': self.generate_nerid(),
            },
            data=json_data,
            headers={
                "Authorization": f"Bearer {self.device_token}" if self.device_token else None,
            },
        )

        try:
            manifest = res.json()
        except json.JSONDecodeError:
            if ignore_errors: return {}
            self.log.error(f" - Amazon reported an error when obtaining the Playback Manifest\n{res.text}"); raise SystemExit(1)

        if "error" in manifest.get("vodPlaybackUrls", {}):
            if ignore_errors: return {}
            message = manifest["vodPlaybackUrls"]["error"]["message"]
            self.log.error(f" - Amazon reported an error when obtaining the Playback Manifest: {message}"); raise SystemExit(1)

        return manifest

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return self.config["certificate"]

    def get_widevine_license(self, challenge: bytes, title: Title_T, track: Tracks, **_) -> str:
        return self._get_license(challenge, title, track, widevine=True)

    def get_playready_license(self, challenge: bytes, title: Title_T, track: Tracks, **_) -> str:
        return self._get_license(challenge, title, track, widevine=False)

    def _get_license(self, challenge: bytes, title: Title_T, track: Tracks, widevine: bool):
        # Determine SessionHandoffToken from tracks extra data or similar
        # In Vinetrimmer this is passed in track.extra, but in envied.tracks are standard objects.
        # We need to rely on the fact that envied.usually doesn't need the sessionHandoffToken for the license request 
        # UNLESS Amazon specifically enforces it for the payload.
        # Vinetrimmer payload:
        
        # NOTE: envied.Track objects might not carry the sessionHandoffToken unless we subclassed DASH parser.
        # For migration purposes, we will attempt without it or fetch from manifest if strictly needed.
        # Vinetrimmer logic extracts it from manifest during track parsing. 
        # Ideally we would pass it via the track object.
        
        session_handoff_token = None
        # Try to find it in track extras if stored (envied.doesn't natively store custom extras easily)
        
        challenge_bytes = challenge if isinstance(challenge, bytes) else challenge.encode("utf-8")
        encoded_challenge = base64.b64encode(challenge_bytes).decode("utf-8")

        data_lic = {
            "includeHdcpTestKeyInLicense": "true",
            "licenseChallenge": encoded_challenge,
            "playbackEnvelope": self.playbackEnvelope,
        }

        endpoint = self.endpoints["licence_pr"] if not widevine else self.endpoints["licence"]
        
        res = self.session.post(
            url=endpoint,
            params={
                'deviceID': self.device_id,
                'deviceTypeID': self.device["device_type"],
                'gascEnabled': str(self.pv).lower(),
                'marketplaceID': self.region["marketplace_id"],
                'uxLocale': 'en_EN',
                'firmware': 1,
                'titleId': title.id,
                'nerid': self.generate_nerid(),
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.device_token}"
            },
            json=data_lic
        ).json()

        req_preview = {k: (v[:80] if isinstance(v, str) else v) for k, v in data_lic.items()}
        if "errorsByResource" in res:
            error = res["errorsByResource"]
            if "errorCode" in error:
                code = error["errorCode"]
            elif "type" in error:
                 code = error["type"]
            else:
                 code = "Unknown"
            
            if code == "PRS.NoRights.AnonymizerIP":
                self.log.error(" - Amazon detected a Proxy/VPN and refused to return a license!"); raise SystemExit(1)
            
            self.log.error(f" - Amazon reported an error during the License request: [{code}]"); raise SystemExit(1)
        
        if "error" in res:
             self.log.error(f" - License Error: {res['error']['message']}"); raise SystemExit(1)

        if widevine:
            return res["widevineLicense"]["license"]
        else:
            return res["playReadyLicense"]["license"]

    # --- Helpers ---

    def choose_manifest(self, manifest: dict, cdn=None):
        if not manifest or "vodPlaybackUrls" not in manifest:
            return {}
        
        url_sets = manifest["vodPlaybackUrls"]["result"]["playbackUrls"].get("urlSets", [])
        if not url_sets: return {}

        if cdn:
            cdn = cdn.lower()
            return next((x for x in url_sets if x["cdn"].lower() == cdn), {})
        
        return random.choice(url_sets)

    @staticmethod
    def generate_nerid(length=24):
        chars = string.ascii_letters + string.digits
        return ''.join(secrets.choice(chars) for _ in range(length))

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
        if name in ("browse", "playback", "licence", "licence_pr", "xray"):
            return f"https://{(region['base_manifest'])}{uri}"
        if name in ("ontv", "devicelink", "details", "getDetailWidgets", "metadata"):
            if self.pv:
                host = "www.primevideo.com"
            else:
                if name in ("metadata"):
                    host = f"{region['base']}/gp/video"
                else:
                    host = region["base"]
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

        # Resolve unique device identity from cache, or generate and persist it.
        # This avoids collisions when multiple users share the same config values,
        # which can cause Amazon to deregister devices that appear duplicated.
        identity_cache = Cacher("AMZN")
        identity_key = f"device_identity_{_profile}"
        cached_identity = identity_cache.get(identity_key)

        if cached_identity and cached_identity.data:
            identity = cached_identity.data
            self.log.debug(" + Using cached device identity")
        else:
            # Generate a unique serial (16 hex chars, same format as real Android devices)
            unique_serial = secrets.token_hex(8)
            # Build a plausible device name: keep the base from config but make it unique
            base_name = self.device.get("device_name", "%FIRST_NAME%'s Shield TV")
            # Strip any existing DUPE_STRATEGY placeholder so we can add our suffix cleanly
            clean_name = re.sub(r"%DUPE_STRATEGY[^%]*%", "", base_name).rstrip()
            suffix = secrets.token_hex(2).upper()  # e.g. "A3F1" — short, looks like a serial suffix
            unique_name = f"{clean_name}-{suffix}"
            identity = {"device_serial": unique_serial, "device_name": unique_name}
            # Persist indefinitely (10 years TTL) — identity should never rotate on its own
            cached_identity = identity_cache.get(identity_key)
            cached_identity.set(identity, int(time.time()) + 60 * 60 * 24 * 3650)
            self.log.info(f" + Generated unique device identity: serial={unique_serial}, name={unique_name!r}")

        self.device["device_serial"] = identity["device_serial"]
        self.device["device_name"] = identity["device_name"]

        device_hash = hashlib.md5(json.dumps(self.device, sort_keys=True).encode()).hexdigest()[0:6]
        device_cache_path = f"device_tokens_{_profile}_{device_hash}"

        self.device_token = self.DeviceRegistration(
            device=self.device,
            endpoints=self.endpoints,
            log=self.log,
            cache_path=device_cache_path,
            session=self.session
        ).bearer

        self.device_id = self.device.get("device_serial")
        if not self.device_id:
            self.log.error(f" - A device serial is required in the config, perhaps use: {os.urandom(8).hex()}"); raise SystemExit(1)

    class DeviceRegistration:
        def __init__(self, device: dict, endpoints: dict, cache_path: str, session: requests.Session, log):
            self.session = session
            self.device = device
            self.endpoints = endpoints
            self.cache_path = cache_path
            self.log = log
            self.cache = Cacher('AMZN')
            
            self.device = {k: str(v) if not isinstance(v, str) else v for k, v in self.device.items()}
            self.bearer = None

            # Retrieve from Cacher
            cached_data = self.cache.get(self.cache_path)
            
            if cached_data:
                # Check expiration
                if cached_data.data.get("expires_in", 0) > int(time.time()):
                    self.log.info(" + Using cached device bearer")
                    self.bearer = cached_data.data["access_token"]
                else:
                    self.log.info("Cached device bearer expired, refreshing...")
                    # Note: Vinetrimmer uses a specific refresh logic calling self.refresh
                    # We need to extract refresh token from cache
                    refresh_token = cached_data.data.get("refresh_token")
                    if refresh_token:
                        refreshed_tokens = self.refresh(self.device, refresh_token)
                        refreshed_tokens["refresh_token"] = refresh_token
                        # Fix: fallback to 3600s if expires_in is missing or zero
                        expires_seconds = int(refreshed_tokens.get("expires_in") or 3600)
                        refreshed_tokens["expires_in"] = int(time.time()) + expires_seconds

                        # Fix: persist refreshed token to cache (was only updated in memory before)
                        cached_data.data = refreshed_tokens
                        cached_data.set(refreshed_tokens, refreshed_tokens["expires_in"])
                        self.bearer = refreshed_tokens["access_token"]
                    else:
                        self.log.info(" + Registering new device bearer (No refresh token)")
                        self.bearer = self.register(self.device)
            else:
                self.log.info(" + Registering new device bearer")
                self.bearer = self.register(self.device)

        def register(self, device: dict) -> str:
            code_pair = self.get_code_pair(device)
            public_code = code_pair["public_code"]

            self.log.info(f" + Visit https://www.primevideo.com/mytv and enter the code: {public_code}")
            self.log.info(f"   Waiting for authorisation (up to 5 minutes)...")

            interval = 10   # seconds between polls
            deadline = int(time.time()) + 300  # 5 minute timeout

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
                self.log.error("Device registration timed out — code was not approved in time."); raise SystemExit(1)

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

            self.log.info(" + Device registered and token cached successfully")
            return bearer_data["access_token"]

        def refresh(self, device: dict, refresh_token: str) -> dict:
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
            
            if "error" in res:
                # Invalidate cache if error
                # self.cache.delete(self.cache_path) # If method existed
                self.log.error(f"Failed to refresh device token: {res.get('error_description')}"); raise SystemExit(1)
            
            return res

        def get_csrf_token(self) -> str:
            res = self.session.get(self.endpoints["ontv"])
            if 'name="appAction" value="SIGNIN"' in res.text or 'SIGNIN_PWD_COLLECT' in res.text:
                self.log.error("Cookies are signed out, cannot get ontv CSRF token.")
                raise SystemExit(1)
            for match in re.finditer(r'<script type="text/template">(.+?)</script>', res.text, re.DOTALL):
                try:
                    prop = json.loads(match.group(1))
                    token = prop.get("props", {}).get("codeEntry", {}).get("token")
                    if token: return token
                    token = prop.get("codeEntry", {}).get("token")
                    if token: return token
                except Exception:
                    pass
            ce_idx = res.text.find('"codeEntry"')
            if ce_idx != -1:
                snippet = res.text[ce_idx:ce_idx+2000]
                m2 = re.search(r'"token"\s*:\s*"([^"]+)"', snippet)
                if m2: return m2.group(1)
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
    # Misc
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
