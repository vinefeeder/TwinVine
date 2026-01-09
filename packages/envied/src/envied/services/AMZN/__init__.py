import base64
import hashlib
import json
from logging import Logger
import os
from pathlib import Path
import re
import sys
from collections import defaultdict
from http.cookiejar import CookieJar
import time
from typing import Any, Optional, Literal, Union
from urllib.parse import quote, urlencode, urlparse, urlunparse
from uuid import uuid4

import requests

import click
import jsonpickle
from langcodes import Language
from click.core import ParameterSource

from envied.core.cacher import Cacher
from envied.core.credential import Credential
from envied.core.manifests import DASH, ISM
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Subtitle, Tracks, Track, Video
from envied.core.tracks.audio import Audio
from envied.core.utilities import is_close_match
from envied.core.utils.collections import as_list


class AMZN(Service):

    """
    Service code for Amazon VOD (https://amazon.com) and Amazon Prime Video (https://primevideo.com).

    \b

    Authorization: Cookies

    Security: 

        UHD@L1/SL3000
        FHD@L3(ChromeCDM)/SL2000
        SD@L3
        Certain SL2000 can do UHD

    \b

    Maintains their own license server like Netflix, be cautious.

    Region is chosen automatically based on domain extension found in cookies.
    Prime Video specific code will be run if the ASIN is detected to be a prime video variant.
    Use 'Amazon Video ASIN Display' for Tampermonkey addon for ASIN
    https://greasyfork.org/en/scripts/496577-amazon-video-asin-display

    vt dl --list -z uk -q 1080 Amazon B09SLGYLK8 
    """
    # GEOFENCE = ("",)
    ALIASES = ("Amazon", "prime", 'amazon')
    TITLE_RE = r"^(?:https?://(?:www\.)?(?P<domain>amazon\.(?P<region>com|co\.uk|de|co\.jp)|primevideo\.com)(?:/.+)?/)?(?P<id>[A-Z0-9]{10,}|amzn1\.dv\.gti\.[a-f0-9-]+)"  # noqa: E501

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
    VIDEO_CODEC_MAP = {
        "H264": ["avc1"],
        "H265": ["hvc1", "dvh1"]
    }
    @staticmethod
    @click.command(name="AMZN", short_help="https://amazon.com, https://primevideo.com", help=__doc__)
    @click.argument("title", type=str, required=False)
    @click.option("-b", "--bitrate", default="CBR",
                    type=click.Choice(["CVBR", "CBR", "CVBR+CBR"], case_sensitive=False),
                    help="Video Bitrate Mode to download in. CVBR=Constrained Variable Bitrate, CBR=Constant Bitrate.")
    @click.option("-c", "--cdn", default=None, type=str,
                    help="CDN to download from, defaults to the CDN with the highest weight set by Amazon.")
    # UHD, HD, SD. UHD only returns HEVC, ever, even for <=HD only content
    @click.option("-vq", "--vquality", default="HD",
                    type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                    help="Manifest quality to request.")
    @click.option("-s", "--single", is_flag=True, default=False,
                    help="Force single episode/season instead of getting series ASIN.")
    @click.option("-am", "--amanifest", default="H265",
                    type=click.Choice(["CVBR", "CBR", "H265"], case_sensitive=False),
                    help="Manifest to use for audio. Defaults to H265 if the video manifest is missing 640k audio.")
    @click.option("-aq", "--aquality", default="SD",
                    type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                    help="Manifest quality to request for audio. Defaults to the same as --quality.")
    # @click.option("-ism", "--ism", is_flag=True, default=False,
    #             help="Set manifest override to SmoothStreaming. Defaults to DASH w/o this flag.") ## DPRECATED
    @click.option("-aa", "--atmos", is_flag=True, default=False,
                help="Prefer Atmos audio if available, otherwise defaults to 640k audio.")    
    @click.option("-drm", "--drm-system", type=click.Choice(["widevine", "playready"], case_sensitive=False),
                  default="playready",
                  help="which drm system to use")
    
    @click.pass_context
    def cli(ctx, **kwargs):
        return AMZN(ctx, **kwargs)

    def __init__(self, ctx, title, bitrate: str, cdn: str, vquality: str, single: bool, amanifest: str, aquality: str,  drm_system: Literal["widevine", "playready"], atmos: bool) -> None:
        m = self.parse_title(ctx, title)
        self.domain = m.get("domain")
        self.domain_region = m.get("region")
        self.drm_system = drm_system
        self.bitrate = bitrate
        self.bitrate_source = ctx.get_parameter_source("bitrate")
        self.vquality = vquality
        self.vquality_source = ctx.get_parameter_source("vquality")        
        self.cdn = cdn
        self.single = single
        self.amanifest = amanifest
        self.aquality = aquality
        self.atmos = atmos 
        super().__init__(ctx)

        assert ctx.parent is not None

        
        self.chapters_only = ctx.parent.params.get("chapters_only")
        self.quality = ctx.parent.params.get("quality")

        self.cdm = ctx.obj.cdm
        self.profile = ctx.obj.profile
        self.region: dict[str, str] = {}
        self.endpoints: dict[str, str] = {}
        self.device: dict[str, str] = {}

        self.pv = self.domain == "primevideo.com"
        self.device_token = None
        self.device_id = None
        self.customer_id = None
        self.client_id = "f22dbddb-ef2c-48c5-8876-bed0d47594fd"  # browser client id

        vcodec = ctx.parent.params.get("vcodec")
        range = ctx.parent.params.get("range_")

        self.range = range[0].name if range else "SDR"
        self.vcodec = "H265" if vcodec and vcodec == Video.Codec.HEVC else "H264"

        if self.vquality_source != ParameterSource.COMMANDLINE:
            if  any(q <= 576 for q in self.quality) and "SDR" == self.range:
                self.log.info(" + Setting manifest quality to SD")
                self.vquality = "SD"

            if any(q > 1080 for q in self.quality):
                self.log.info(" + Setting manifest quality to UHD and vcodec to H265 to be able to get 2160p video track")
                self.vquality = "UHD"
                self.vcodec = "H265"

        self.vquality = self.vquality or "HD"

        if self.bitrate_source != ParameterSource.COMMANDLINE:
            if self.vcodec == "H265" and self.range == "SDR" and self.bitrate != "CVBR+CBR":
                self.bitrate = "CVBR+CBR"
                self.log.info(" + Changed bitrate mode to CVBR+CBR to be able to get H.265 SDR video track")

            if self.vquality == "UHD" and self.range != "SDR" and self.bitrate != "CBR":
                self.bitrate = "CBR"
                self.log.info(f" + Changed bitrate mode to CBR to be able to get highest quality UHD {self.range} video track")

        self.orig_bitrate = self.bitrate


        self.manifestTypeTry = "DASH"
        self.log.info("Getting tracks from MPD manifest")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")

        self.session.cookies.update(cookies)
        self.configure()
        
    # Abstracted functions

    def get_titles(self) -> Titles_T:
        res = self.session.get(
            url=self.endpoints["details"],
            params={"titleID": self.title, "isElcano": "1", "sections": "Atf"},
            headers={"Accept": "application/json"},
        ).json()["widgets"]

        entity = res["header"]["detail"].get("entityType")
        if not entity:
            self.log.error(" - Failed to get entity type")
            sys.exit(1)

        if entity == "Movie":
            metadata = res["header"]["detail"]
            return Movies(
                [
                    Movie(
                        id_=metadata.get("catalogId"),
                        year=metadata.get("releaseYear"),
                        name=metadata.get("title"),
                        service=self.__class__,
                        data=metadata,
                    )
                ]
            )
        elif entity == "TV Show":
            seasons = [x.get("titleID") for x in res["seasonSelector"]]

            episodes = []
            for season in seasons:
                res = self.session.get(
                    url=self.endpoints["detail"],
                    params={"titleID": season, "isElcano": "1", "sections": "Btf"},
                    headers={"Accept": "application/json"},
                ).json()["widgets"]

                # cards = [x["detail"] for x in as_list(res["titleContent"][0]["cards"])]
                cards = [
                    {**x["detail"], "sequenceNumber": x["self"]["sequenceNumber"]}
                    for x in res["episodeList"]["episodes"]
                ]

                product_details = res["productDetails"]["detail"]

                episodes.extend(
                    Episode(
                        id_=title.get("titleId") or title["catalogId"],
                        title=product_details.get("parentTitle") or product_details["title"],
                        year=title.get("releaseYear") or product_details.get("releaseYear"),
                        season=product_details.get("seasonNumber"),
                        number=title.get("sequenceNumber"),
                        name=title.get("title"),
                        service=self.__class__,
                        data=title,
                    )
                    for title in cards
                    if title["entityType"] == "TV Show"
                )

            return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        tracks = Tracks()
        if self.chapters_only:
            return []

        #manifest, chosen_manifest, tracks = self.get_best_quality(title)

        manifest = self.get_manifest(
            title,
            video_codec=self.vcodec,
            bitrate_mode=self.bitrate,
            quality=self.vquality,
            hdr=self.range,
            ignore_errors=False
            
        )
        
        # Move rightsException termination here so that script can attempt continuing
        if "rightsException" in manifest["returnedTitleRendition"]["selectedEntitlement"]:
            self.log.error(" - The profile used does not have the rights to this title.")
            return

        self.customer_id = manifest["returnedTitleRendition"]["selectedEntitlement"]["grantedByCustomerId"]

        default_url_set = manifest["playbackUrls"]["urlSets"][manifest["playbackUrls"]["defaultUrlSetId"]]
        encoding_version = default_url_set["urls"]["manifest"]["encodingVersion"]
        self.log.info(f" + Detected encodingVersion={encoding_version}")

        #print(manifest)
        chosen_manifest = self.choose_manifest(manifest, self.cdn)

        if not chosen_manifest:
            raise self.log.exit(f"No manifests available")

        manifest_url = self.clean_mpd_url(chosen_manifest["avUrlInfoList"][0]["url"], False)
        self.log.debug(manifest_url)
        # if self.event:
        #     devicetype = self.device["device_type"]
        #     manifest_url = chosen_manifest["avUrlInfoList"][0]["url"]
        #     manifest_url = f"{manifest_url}?amznDtid={devicetype}&encoding=segmentBase"
        self.log.info(" + Downloading Manifest")

        if chosen_manifest["streamingTechnology"] == "DASH":
            tracks = Tracks([
                x for x in iter(DASH.from_url(url=manifest_url, session=self.session).to_tracks(language="es"))
            ])
        elif chosen_manifest["streamingTechnology"] == "SmoothStreaming":
            tracks = Tracks([
                x for x in iter(ISM.from_url(url=manifest_url, session=self.session).to_tracks(language="es"))
            ])
        else:
            raise self.log.exit(f"Unsupported manifest type: {chosen_manifest['streamingTechnology']}")

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

        if need_separate_audio: # and not self.atmos:
            tracks.audio.clear()
            manifest_type = self.amanifest or "H265"
            self.log.info(f"Getting audio from {manifest_type} manifest for potential higher bitrate or better codec")
            audio_manifest = self.get_manifest(
                title=title,
                video_codec="H265" if manifest_type == "H265" else "H264",
                bitrate_mode="CVBR" if manifest_type != "CBR" else "CBR",
                quality=self.aquality or self.vquality,
                hdr=None,
                ignore_errors=True
            )
            if not audio_manifest:
                self.log.warning(f" - Unable to get {manifest_type} audio manifests, skipping")
            elif not (chosen_audio_manifest := self.choose_manifest(audio_manifest, self.cdn)):
                self.log.warning(f" - No {manifest_type} audio manifests available, skipping")
            else:
                audio_mpd_url = self.clean_mpd_url(chosen_audio_manifest["avUrlInfoList"][0]["url"], optimise=False)
                self.log.debug(audio_mpd_url)
                # if self.event:
                #     devicetype = self.device["device_type"]
                #     audio_mpd_url = chosen_audio_manifest["avUrlInfoList"][0]["url"]
                #     audio_mpd_url = f"{audio_mpd_url}?amznDtid={devicetype}&encoding=segmentBase"
                self.log.info(" + Downloading HEVC manifest")

                try:
                    audio_mpd = Tracks([
                        x for x in iter(DASH.from_url(url=audio_mpd_url, session=self.session).to_tracks(language="en"))
                    ])
                except KeyError:
                    self.log.warning(f" - Title has no {self.amanifest} stream, cannot get higher quality audio")
                else:
                    tracks.audio = audio_mpd.audio  # expecting possible dupes, ignore

        need_uhd_audio = self.atmos

        if not self.amanifest and ((self.aquality == "UHD" and self.vquality != "UHD") or not self.aquality):
            audios = defaultdict(list)
            for audio in tracks.audio:
                audios[audio.language].append(audio)
            for lang in audios:
                if not any((x.bitrate or 0) >= 640000 for x in audios[lang]):
                    need_uhd_audio = True
                    break

        if need_uhd_audio and (self.config.get("device") or {}).get(self.profile, None):
            self.log.info("Getting audio from UHD manifest for potential higher bitrate or better codec")
            temp_device = self.device
            temp_device_token = self.device_token
            temp_device_id = self.device_id
            uhd_audio_manifest = None

            try:
                if self.cdm.device.type in ["CHROME", "PLAYREADY"] and self.quality < 2160:
                    self.log.info(f" + Switching to device to get UHD manifest")
                    self.register_device()

                uhd_audio_manifest = self.get_manifest(
                    title=title,
                    video_codec="H265",
                    bitrate_mode="CVBR+CBR",
                    quality="UHD",
                    hdr="DV",  # Needed for 576kbps Atmos sometimes
                    ignore_errors=True
                )
            except:
                pass

            self.device = temp_device
            self.device_token = temp_device_token
            self.device_id = temp_device_id

            if not uhd_audio_manifest:
                self.log.warning(f" - Unable to get UHD manifests, skipping")
            elif not (chosen_uhd_audio_manifest := self.choose_manifest(uhd_audio_manifest, self.cdn)):
                self.log.warning(f" - No UHD manifests available, skipping")
            else:
                tracks.audio.clear()
                uhd_audio_mpd_url = self.clean_mpd_url(chosen_uhd_audio_manifest["avUrlInfoList"][0]["url"], optimise=False)
                self.log.debug(uhd_audio_mpd_url)
                # if self.event:
                #     devicetype = self.device["device_type"]
                #     uhd_audio_mpd_url = chosen_uhd_audio_manifest["avUrlInfoList"][0]["url"]
                #     uhd_audio_mpd_url = f"{uhd_audio_mpd_url}?amznDtid={devicetype}&encoding=segmentBase"
                self.log.info(" + Downloading UHD  manifest")

                try:
                    uhd_audio_mpd = Tracks([
                        x for x in iter(DASH.from_url(url=uhd_audio_mpd_url, session=self.session).to_tracks(language="en"))
                    ])
                except KeyError:
                    self.log.warning(f" - Title has no UHD stream, cannot get higher quality audio")
                else:
                    # replace the audio tracks with DV manifest version if atmos is present
                    if any(x for x in uhd_audio_mpd.audio if x.atmos):
                        tracks.audio = uhd_audio_mpd.audio

        for video in tracks.videos:
            video.hdr10 = chosen_manifest["hdrFormat"] == "Hdr10"
            video.dv = chosen_manifest["hdrFormat"] == "DolbyVision"

        for audio in tracks.audio:
            audio.descriptive = audio.data["dash"]["adaptation_set"].get("audioTrackSubtype") == "descriptive"
            # Amazon @lang is just the lang code, no dialect, @audioTrackId has it.
            audio_track_id = audio.data["dash"]["adaptation_set"].get("audioTrackId")
            if audio_track_id:
                audio.language = Language.get(audio_track_id.split("_")[0])  # e.g. es-419_ec3_blabla
            # Remove any audio tracks with dialog boost!
            if audio.data["dash"]["adaptation_set"] is not None and "boosteddialog" in audio.data["dash"]["adaptation_set"].get("audioTrackSubtype", ""):
                audio.bitrate = 1

        for sub in manifest.get("subtitleUrls", []) + manifest.get("forcedNarratives", []):
            try:
                tracks.add(Subtitle(
                    id_=sub.get(
                        "timedTextTrackId",
                        f"{sub['languageCode']}_{sub['type']}_{sub['subtype']}_{sub.get('index', 'default')}"
                    ),
                    url=os.path.splitext(sub["url"])[0] + ".srt",  # DFXP -> SRT forcefully seems to work fine
                    # metadata
                    codec=Subtitle.Codec.from_codecs("srt"),  # sub["format"].lower(),
                    language=sub["languageCode"],
                    #is_original_lang=title.original_lang and is_close_match(sub["languageCode"], [title.original_lang]),
				    forced="forced" in sub["displayName"],
                    sdh=sub["type"].lower() == "sdh"  # TODO: what other sub types? cc? forced?
                ), warn_only=True)  # expecting possible dupes, ignore
            except KeyError:
                # Log the KeyError Exception but continue (as only the subtitles will be missing)
                self.log.error("Unexpected subtitle track id data format, subtitles will be missing", exc_info=True)
            
        for track in tracks:
            track.needs_proxy = False

        tracks.audio = self._dedupe(tracks.audio)

        return tracks

    @staticmethod
    def _dedupe(items: list) -> list:
        if not items:
            return items
        if isinstance(items[0].url, list):
            return items
        
        # Create a more specific key for deduplication that includes resolution/bitrate
        seen = {}
        for item in items:
            # For video tracks, use codec + resolution + bitrate as key
            if hasattr(item, 'width') and hasattr(item, 'height'):
                key = f"{item.codec}_{item.width}x{item.height}_{item.bitrate}"
            # For audio tracks, use codec + language + bitrate + channels as key  
            elif hasattr(item, 'channels'):
                key = f"{item.codec}_{item.language}_{item.bitrate}_{item.channels}"
            # Fallback to URL for other track types
            else:
                key = item.url
            
            # Keep the item if we haven't seen this exact combination
            if key not in seen:
                seen[key] = item
        
        return list(seen.values())
    
    def get_chapters(self, title: Title_T) -> Chapters:
        """Get chapters from Amazon's XRay Scenes API."""
        manifest = self.get_manifest(
            title,
            video_codec=self.vcodec,
            bitrate_mode=self.bitrate,
            quality="UHD",
            hdr=self.range
        )

        if "xrayMetadata" in manifest:
            xray_params = manifest["xrayMetadata"]["parameters"]
        elif self.chapters_only:
            xray_params = {
                "pageId": "fullScreen",
                "pageType": "xray",
                "serviceToken": json.dumps({
                    "consumptionType": "Streaming",
                    "deviceClass": "normal",
                    "playbackMode": "playback",
                    "vcid": manifest["returnedTitleRendition"]["contentId"],
                })
            }
        else:
            return []

        xray_params.update({
            "deviceID": self.device_id,
            "deviceTypeID": self.config["device_types"]["browser"],  # must be browser device type
            "marketplaceID": self.region["marketplace_id"],
            "gascEnabled": str(self.pv).lower(),
            "decorationScheme": "none",
            "version": "inception-v2",
            "uxLocale": "en-US",
            "featureScheme": "XRAY_WEB_2020_V1"
        })

        xray = self.session.get(
            url=self.endpoints["xray"],
            params=xray_params
        ).json().get("page")

        if not xray:
            return []

        widgets = xray["sections"]["center"]["widgets"]["widgetList"]

        scenes = next((x for x in widgets if x["tabType"] == "scenesTab"), None)
        if not scenes:
            return []
        scenes = scenes["widgets"]["widgetList"][0]["items"]["itemList"]

        chapters = []

        for scene in scenes:
            chapter_title = scene["textMap"]["PRIMARY"]
            match = re.search(r"(\d+\. |)(.+)", chapter_title)
            if match:
                chapter_title = match.group(2)
            chapters.append(Chapter(
                name=chapter_title,
                timestamp=scene["textMap"]["TERTIARY"].replace("Starts at ", "")
            ))

        return chapters

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return self.config["certificate"]

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track) -> None:
        response = self.session.post(
            url=self.endpoints["licence"],
            params={
                "asin": title.id,
                "consumptionType": "Streaming",
                "desiredResources": "Widevine2License",
                "deviceTypeID": self.device["device_type"],
                "deviceID": self.device_id,
                "firmware": 1,
                "gascEnabled": str(self.pv).lower(),
                "marketplaceID": self.region["marketplace_id"],
                "resourceUsage": "ImmediateConsumption",
                "videoMaterialType": "Feature",
                "operatingSystemName": "Linux" if any(q <= 576 for q in self.quality) else "Windows",
                "operatingSystemVersion": "unknown" if any(q <= 576 for q in self.quality) else "10.0",
                "customerID": self.customer_id,
                "deviceDrmOverride": "CENC",
                "deviceStreamingTechnologyOverride": "DASH",
                "deviceVideoQualityOverride": "HD",
                "deviceHdrFormatsOverride": "None",
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {self.device_token}",
            },
            data={
                "widevine2Challenge": base64.b64encode(challenge).decode(),
                "includeHdcpTestKeyInLicense": "false",
            },
        ).json()
        if "errorsByResource" in response:
            error_code = response["errorsByResource"]["Widevine2License"]
            if "errorCode" in error_code:
                error_code = error_code["errorCode"]
            elif "type" in error_code:
                error_code = error_code["type"]

            if error_code in ["PRS.NoRights.AnonymizerIP", "PRS.NoRights.NotOwned"]:
                self.log.error("Proxy detected, Unable to License")
            elif error_code == "PRS.Dependency.DRM.Widevine.UnsupportedCdmVersion":
                self.log.error("Cdm version not supported")
            else:
                self.log.error(f"  x Error from Amazon's License Server: [{error_code}]")
            sys.exit(1)

        return response["widevine2License"]["license"]

    def get_playready_license(self, challenge: Union[bytes, str], title: Title_T, **_):
        lic_list = []
        lic_challenge = base64.b64encode(challenge).decode("utf-8") if isinstance(challenge, bytes) else base64.b64encode(challenge.encode("utf-8")).decode("utf-8")
        self.log.debug(f"Challenge - {lic_challenge}")
        params = {
            "asin": title.id,
            "consumptionType": "Streaming", # Streaming or Download both work
            "desiredResources": "PlayReadyLicense",
            "deviceTypeID": self.device["device_type"],
            "deviceID": self.device_id,
            "firmware": 1,
            "gascEnabled": str(self.pv).lower(),
            "marketplaceID": self.region["marketplace_id"],
            "resourceUsage": "ImmediateConsumption",
            "videoMaterialType": "Feature",
            "operatingSystemName": "Windows",
            "operatingSystemVersion": "10.0",
            "customerID": self.customer_id,
            "deviceDrmOverride": "CENC", #CENC or Playready both work
            "deviceStreamingTechnologyOverride": "DASH", # or SmoothStreaming
            "deviceVideoQualityOverride": self.vquality,
            "deviceHdrFormatsOverride": self.VIDEO_RANGE_MAP.get(self.range, "None"),
        }	
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {self.device_token}"
        }
        data = {
            "playReadyChallenge": lic_challenge,  # expects base64
            "includeHdcpTestKeyInLicense": "true"
        }
        lic = self.session.post(
            url=self.endpoints["licence"],
            params=params,
            headers=headers,
            data=data
        ).json()
        lic_list.append(lic)		
        # params["deviceStreamingTechnologyOverride"] = "SmoothStreaming"
        params["deviceDrmOverride"] = "Playready"
        lic = self.session.post(
            url=self.endpoints["licence"],
            params=params,
            headers=headers,
            data=data
        ).json()
        lic_list.append(lic)

        for lic in lic_list:
            if "errorsByResource" in lic:
                error_code = lic["errorsByResource"]["PlayReadyLicense"]
                self.log.debug(error_code)
                if "errorCode" in error_code:
                    error_code = error_code["errorCode"]
                elif "type" in error_code:
                    error_code = error_code["type"]
                if error_code == "PRS.NoRights.AnonymizerIP":
                    self.log.error(" - Amazon detected a Proxy/VPN and refused to return a license!")
                    continue
                message = lic["errorsByResource"]["PlayReadyLicense"]["message"]
                self.log.error(f" - Amazon reported an error during the License request: {message} [{error_code}]")
                continue
            elif "error" in lic:
                error_code = lic["error"]
                if "errorCode" in error_code:
                    error_code = error_code["errorCode"]
                elif "type" in error_code:
                    error_code = error_code["type"]
                if error_code == "PRS.NoRights.AnonymizerIP":
                    self.log.error(" - Amazon detected a Proxy/VPN and refused to return a license!")
                    continue
                message = lic["error"]["message"]
                self.log.error(f" - Amazon reported an error during the License request: {message} [{error_code}]")
                continue
            else:
                xmrlic = base64.b64decode(lic["playReadyLicense"]["encodedLicenseResponse"].encode("utf-8")).decode("utf-8")
                self.log.debug(xmrlic)
                return xmrlic # Return Xml licence
            
    # Service specific functions

    def configure(self):
        if len(self.title) > 10 and not (self.domain or "").startswith("amazon."):
            self.pv = True

        self.log.info("Getting account region")
        self.region = self.get_region()
        if not self.region:
            self.log.error(" - Failed to get Amazon account region")
            sys.exit(1)
        # self.GEOFENCE.append(self.region["code"])
        self.log.info(f" + Region: {self.region['code'].upper()}")

        # endpoints must be prepared AFTER region data is retrieved
        self.endpoints = self.prepare_endpoints(self.config["endpoints"], self.region)

        self.session.headers.update({"Origin": f"https://{self.region['base']}"})

        self.device = (self.config.get("device") or {}).get(self.profile, "default")

        if (int(self.quality[0]) > 1080 or self.range != "SDR" or self.atmos):
            self.log.info(f"Using device to get UHD manifests")
            self.register_device()
            
        elif not self.device or self.vquality != "UHD" or self.drm_system == "widevine":
            # falling back to browser-based device ID
            if not self.device:
                self.log.warning(
                    "No Device information was provided for %s, using browser device...",
                    self.profile
                )
            self.device_id = hashlib.sha224(
                ("CustomerID" + self.session.headers["User-Agent"]).encode("utf-8")
            ).hexdigest()
            self.device = {"device_type": self.config["device_types"]["browser"]}
        else:
            self.register_device()

    def register_device(self) -> None:
        self.device = (self.config.get("device") or {}).get(self.profile, "default")
        device_cache_path = f"device_tokens_{self.profile}_{hashlib.md5(json.dumps(self.device).encode()).hexdigest()[0:6]}"
        self.device_token = self.DeviceRegistration(
            device=self.device,
            endpoints=self.endpoints,
            log=self.log,
            cache_path=device_cache_path,
            session=self.session
        ).bearer
        self.device_id = self.device.get("device_serial")
        if not self.device_id:
            raise self.log.error(f" - A device serial is required in the config, perhaps use: {os.urandom(8).hex()}")
        
    def get_region(self):
        domain_region = self.get_domain_region()
        if not domain_region:
            return {}

        region = self.config["regions"].get(domain_region)
        if not region:
            raise self.log.error(f" - There's no region configuration data for the region: {domain_region}")

        region["code"] = domain_region

        if self.pv:
            res = self.session.get("https://www.primevideo.com").text
            match = re.search(r'ue_furl *= *([\'"])fls-(na|eu|fe)\.amazon\.[a-z.]+\1', res)
            if match:
                pv_region = match.group(2).lower()
            else:
                raise self.log.error(" - Failed to get PrimeVideo region")
            pv_region = {"na": "atv-ps"}.get(pv_region, f"atv-ps-{pv_region}")
            region["base_manifest"] = f"{pv_region}.primevideo.com"
            region["base"] = "www.primevideo.com"

        return region

    def get_domain_region(self):
        """Get the region of the cookies from the domain."""
        tld = (self.domain_region or "").split(".")[-1]
        if not tld:
            domains = [x.domain for x in self.session.cookies if x.domain_specified]
            tld = next((x.split(".")[-1] for x in domains if x.startswith((".amazon.", ".primevideo."))), None)
        return {"com": "us", "uk": "gb"}.get(tld, tld)
    
    def prepare_endpoint(self, name: str, uri: str, region: dict) -> str:
        if name in ("browse", "playback", "licence", "xray"):
            return f"https://{(region['base_manifest'])}{uri}"
        if name in ("ontv", "ontvold", "mytv", "devicelink", "details", "getDetailWidgets"):
            if self.pv:
                host = "www.primevideo.com"
            else:
                host = region["base"]
            return f"https://{host}{uri}"
        if name in ("codepair", "register", "token"):
            return f"https://{self.config['regions']['us']['base_api']}{uri}"
        raise ValueError(f"Unknown endpoint: {name}")

    def prepare_endpoints(self, endpoints: dict, region: dict) -> dict:
        return {k: self.prepare_endpoint(k, v, region) for k, v in endpoints.items()}

    def choose_manifest(self, manifest: dict, cdn=None):
        """Get manifest URL for the title based on CDN weight (or specified CDN)."""
        if cdn:
            cdn = cdn.lower()
            manifest = next((x for x in manifest["audioVideoUrls"]["avCdnUrlSets"] if x["cdn"].lower() == cdn), {})
            if not manifest:
                raise self.log.exit(f" - There isn't any DASH manifests available on the CDN \"{cdn}\" for this title")
        else:
            manifest = next((x for x in sorted([x for x in manifest["audioVideoUrls"]["avCdnUrlSets"]], key=lambda x: int(x["cdnWeightsRank"]))), {})

        return manifest

    def get_manifest(
        self, title, video_codec: str, bitrate_mode: str, quality: str, hdr=None,
            ignore_errors: bool = False
    ) -> dict:
        res = self.session.get(
            url=self.endpoints["playback"],
            params={
                "asin": title.id,
                "consumptionType": "Streaming",
                "desiredResources": ",".join([
                    "PlaybackUrls",
                    "AudioVideoUrls",
                    "CatalogMetadata",
                    "ForcedNarratives",
                    "SubtitlePresets",
                    "SubtitleUrls",
                    "TransitionTimecodes",
                    "TrickplayUrls",
                    "CuepointPlaylist",
                    "XRayMetadata",
                    "PlaybackSettings",
                ]),
                "deviceID": self.device_id,
                "deviceTypeID": self.device["device_type"],
                "firmware": 1,
                "gascEnabled": str(self.pv).lower(),
                "marketplaceID": self.region["marketplace_id"],
                "resourceUsage": "CacheResources",
                "videoMaterialType": "Feature",
                "playerType": "html5",
                "clientId": self.client_id,
                **({
                    "operatingSystemName": "Linux" if quality == "SD" else "Windows",
                    "operatingSystemVersion": "unknown" if quality == "SD" else "10.0",
                } if not self.device_token else {}),
                "deviceDrmOverride": "CENC",
                "deviceStreamingTechnologyOverride": "DASH",
                "deviceProtocolOverride": "Https",
                "deviceVideoCodecOverride": video_codec,
                "deviceBitrateAdaptationsOverride": bitrate_mode.replace("+", ","),
                "deviceVideoQualityOverride": quality,
                "deviceHdrFormatsOverride": self.VIDEO_RANGE_MAP.get(hdr, "None"),
                "supportedDRMKeyScheme": "DUAL_KEY",  # ?
                "liveManifestType": "live,accumulating",  # ?
                "titleDecorationScheme": "primary-content",
                "subtitleFormat": "TTMLv2",
                "languageFeature": "MLFv2",  # ?
                "uxLocale": "en_US",
                "xrayDeviceClass": "normal",
                "xrayPlaybackMode": "playback",
                "xrayToken": "XRAY_WEB_2020_V1",
                "playbackSettingsFormatVersion": "1.0.0",
                "playerAttributes": json.dumps({"frameRate": "HFR"}),
                # possibly old/unused/does nothing:
                "audioTrackId": "all",
            },
            headers={
                "Authorization": f"Bearer {self.device_token}" if self.device_token else None,
            },
        )
        try:
            manifest = res.json()
        except json.JSONDecodeError:
            if ignore_errors:
                return {}

            raise self.log.exit(" - Amazon didn't return JSON data when obtaining the Playback Manifest.")

        if "error" in manifest:
            if ignore_errors:
                return {}
            raise self.log.exit(" - Amazon reported an error when obtaining the Playback Manifest.")

        # Commented out as we move the rights exception check elsewhere
        # if "rightsException" in manifest["returnedTitleRendition"]["selectedEntitlement"]:
        #     if ignore_errors:
        #         return {}
        #     raise self.log.exit(" - The profile used does not have the rights to this title.")

        # Below checks ignore NoRights errors

        if (
          manifest.get("errorsByResource", {}).get("PlaybackUrls") and
          manifest["errorsByResource"]["PlaybackUrls"].get("errorCode") != "PRS.NoRights.NotOwned"
        ):
            if ignore_errors:
                return {}
            error = manifest["errorsByResource"]["PlaybackUrls"]
            raise self.log.exit(f" - Amazon had an error with the Playback Urls: {error['message']} [{error['errorCode']}]")

        if (
          manifest.get("errorsByResource", {}).get("AudioVideoUrls") and
          manifest["errorsByResource"]["AudioVideoUrls"].get("errorCode") != "PRS.NoRights.NotOwned"
        ):
            if ignore_errors:
                return {}
            error = manifest["errorsByResource"]["AudioVideoUrls"]
            raise self.log.exit(f" - Amazon had an error with the A/V Urls: {error['message']} [{error['errorCode']}]")

        return manifest

    @staticmethod
    def get_original_language(manifest):
        """Get a title's original language from manifest data."""
        try:
            return next(
                x["language"].replace("_", "-")
                for x in manifest["catalogMetadata"]["playback"]["audioTracks"]
                if x["isOriginalLanguage"]
            )
        except (KeyError, StopIteration):
            pass

        if "defaultAudioTrackId" in manifest.get("playbackUrls", {}):
            try:
                return manifest["playbackUrls"]["defaultAudioTrackId"].split("_")[0]
            except IndexError:
                pass

        try:
            return sorted(
                manifest["audioVideoUrls"]["audioTrackMetadata"],
                key=lambda x: x["index"]
            )[0]["languageCode"]
        except (KeyError, IndexError):
            pass

        return None

    @staticmethod
    def clean_mpd_url(mpd_url, optimise=True):
        print(f"MPD URL: {mpd_url}, optimise: {optimise}")
        """Clean up an Amazon MPD manifest url."""
        if 'akamaihd.net' in mpd_url:
            match = re.search(r'[^/]*\$[^/]*/', mpd_url)
            if match:
                dollar_sign_part = match.group(0)
                mpd_url = mpd_url.replace(dollar_sign_part, '', 1)
                return mpd_url
        
        if optimise:
            return mpd_url.replace("~", "") + "?encoding=segmentBase"
        else:
            if match :=   re.match(r"(https?://.*/)d.?/.*~/(.*)", mpd_url):
                print(f"returned: {''.join(match.groups())}")
                return "".join(match.groups())
            elif match := re.match(r"(https?://.*/)d.?/.*\$.*?/(.*)", mpd_url):
                print(f"returned: {''.join(match.groups())}")
                return "".join(match.groups())
            elif match := re.match(r"(https?://.*/).*\$.*?/(.*)", mpd_url):
                print(f"returned: {''.join(match.groups())}")
                return "".join(match.groups())
            raise ValueError("Unable to parse MPD URL")

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

	# Service specific classes

    class DeviceRegistration:
        def __init__(self, device: dict, endpoints: dict, cache_path: str, session: requests.Session, log: Logger):
            self.session = session
            self.device = device
            self.endpoints = endpoints
            self.cache_path = cache_path
            self.log = log
            self.cache = Cacher('AMZN') 
            # self.device = {k: str(v) if not isinstance(v, str) else v for k, v in self.device.items()}

            self.bearer = None

            self._cache = self.cache.get(self.cache_path)
            if self._cache:
                if self._cache.data.get("expires_in", 0) > int(time.time()):
                    self.log.info(" + Using cached device bearer")
                    self.bearer = self._cache["access_token"]
                else:
                    self.log.info("Refreshing cached device bearer...")
                    refreshed_tokens = self.refresh(self.device, self._cache.data["refresh_token"], self._cache.data["access_token"])
                    refreshed_tokens["refresh_token"] = self._cache.data["refresh_token"]
                    # expires_in seems to be in minutes, create a unix timestamp and add the minutes in seconds
                    refreshed_tokens["expires_in"] = int(time.time()) + int(refreshed_tokens["expires_in"])
                    self._cache.data = refreshed_tokens
                    self.bearer = refreshed_tokens["access_token"]
            else:
                self.log.info(" + Registering new device bearer")
                self.bearer = self.register(self.device)

        def register(self, device: dict) -> dict:
            """
            Register device to the account
            :param device: Device data to register
            :return: Device bearer tokens
            """
            # OnTV csrf
            csrf_token, referer = self.get_csrf_token()

            # Code pair
            code_pair = self.get_code_pair(device)

            # Device link
            response = self.session.post(
                url=self.endpoints["devicelink"],
                headers={
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9,es-US;q=0.8,es;q=0.7",  # needed?
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": referer
                },
                params=urlencode({
                    # any reason it urlencodes here? requests can take a param dict...
                    "ref_": "atv_set_rd_reg",
                    "publicCode": code_pair["public_code"],  # public code pair
                    "token": csrf_token  # csrf token
                })
            )
            if response.status_code != 200:
                raise self.log.error(f"Unexpected response with the codeBasedLinking request: {response.text} [{response.status_code}]")

            # Register
            response = self.session.post(
                url=self.endpoints["register"],
                headers={
                    "Content-Type": "application/json",
                    "Accept-Language": "en-US",
                },
                json={
                    "auth_data": {
                        "code_pair": code_pair
                    },
                    "registration_data": device,
                    "requested_token_type": ["bearer"],
                    "requested_extensions": ["device_info", "customer_info"]
                },
                cookies=None  # for some reason, may fail if cookies are present. Odd.
            )
            if response.status_code != 200:
                raise self.log.error(f"Unable to register: {response.text} [{response.status_code}]")
            bearer = response.json()["response"]["success"]["tokens"]["bearer"]
            bearer["expires_in"] = int(time.time()) + int(bearer["expires_in"])

            # Cache bearer
            self._cache.set(bearer)
            # os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            # with open(self.cache_path, "w", encoding="utf-8") as fd:
            #     fd.write(jsonpickle.encode(bearer))

            return bearer["access_token"]

        def refresh(self, device: dict, refresh_token: str, access_token: str) -> dict:
            # using the refresh token get the cookies needed for making calls to *.amazon.com
            response = requests.post(
                url=self.endpoints["token"], 
                headers={
                    'User-Agent': 'AmazonWebView/Amazon Alexa/2.2.223830.0/iOS/11.4.1/iPhone', # https://gitlab.com/keatontaylor/alexapy/-/commit/540b6333d973177bbc98e6ef39b00134f80ef0bb
                    'Accept-Language': 'en-US',
                    'Accept-Charset': 'utf-8',
                    'Connection': 'keep-alive',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': '*/*'
                }, 
                cookies={
                    'at-main': access_token,
                }, 
                data={
                    **device,
                    'domain': '.' + self.endpoints["token"].split("/")[-3],
                    'source_token': str(refresh_token),  
                    'requested_token_type': 'auth_cookies',
                    'source_token_type': 'refresh_token',
                }
            )
            response_json = response.json()
            cookies = {}
            self.log.debug(response_json)
            if response.status_code == 200:
                # Extract the cookies from the response
                raw_cookies = response_json['response']['tokens']['cookies']['.amazon.com']
                for cookie in raw_cookies:
                    cookies[cookie['Name']] = cookie['Value']
            else:
                error = response_json['response']["error"]
                self.cache_path.unlink(missing_ok=True)
                raise self.log.error(f"Error when refreshing cookies: {error['message']} [{error['code']}]")

            response = requests.post(
                url=self.endpoints["token"], 
                headers={
                    'Content-Type': 'application/json; charset=utf-8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Accept': 'application/json; charset=utf-8',
                    'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                    'Accept-Language': 'en-US,en-US;q=1.0',
                    'x-amzn-identity-auth-domain': self.endpoints["token"].split("/")[-3], 
                    'x-amzn-requestid': str(uuid4()).replace('-', '')
                }, 
                json={
                    **device,
                    'requested_token_type': 'access_token',
                    'source_token_type': 'refresh_token',
                    'source_token': str(refresh_token),
                }, # https://github.com/Sandmann79/xbmc/blob/dab17d913ee877d96115e6f799623bca158f3f24/plugin.video.amazon-test/resources/lib/login.py#L593 
                cookies=cookies
            )
            response_json = response.json()
            
            if response.status_code != 200 or "error" in response_json:
                self.cache_path.unlink(missing_ok=True)  # Remove the cached device as its tokens have expired
                raise self.log.error(f"Failed to refresh device token -> {response_json['error_description']} [{response_json['error']}]")
            self.log.debug(response_json)
            if response_json["token_type"] != "bearer":
                raise self.log.error("Unexpected returned refreshed token type")

            return response_json

        def get_csrf_token(self) -> str:
            """
            On the amazon website, you need a token that is in the html page,
            this token is used to register the device
            :return: OnTV Page's CSRF Token
            """
            try:
                res = self.session.get(self.endpoints["ontv"])
                response = res.text
                if 'input type="hidden" name="appAction" value="SIGNIN"' in response:
                    raise self.log.error(
                        "Cookies are signed out, cannot get ontv CSRF token. "
                        f"Expecting profile to have cookies for: {self.endpoints['ontv']}"
                    )
                for match in re.finditer(r"<script type=\"text/template\">(.+)</script>", response):
                    prop = json.loads(match.group(1))
                    prop = prop.get("props", {}).get("codeEntry", {}).get("token")
                    if prop:
                        return prop, self.endpoints["ontv"]
                raise self.log.error(f"Unable to get ontv CSRF token - Navigate to {self.endpoints['mytv']}, login and save cookies from code pair page to default.txt")
            except:
                res = self.session.get(self.endpoints["ontvold"])
                response = res.text
                if 'input type="hidden" name="appAction" value="SIGNIN"' in response:
                    raise self.log.error(
                        "Cookies are signed out, cannot get ontv CSRF token. "
                        f"Expecting profile to have cookies for: {self.endpoints['ontvold']}"
                    )
                for match in re.finditer(r"<script type=\"text/template\">(.+)</script>", response):
                    prop = json.loads(match.group(1))
                    prop = prop.get("props", {}).get("codeEntry", {}).get("token")
                    if prop:
                        return prop, self.endpoints["ontvold"]
                raise self.log.error(f"Unable to get ontv CSRF token - Navigate to {self.endpoints['mytv']}, login and save cookies from code pair page to default.txt")

        def get_code_pair(self, device: dict) -> dict:
            """
            Getting code pairs based on the device that you are using
            :return: public and private code pairs
            """
            res = self.session.post(
                url=self.endpoints["codepair"],
                headers={
                    "Content-Type": "application/json",
                    "Accept-Language": "en-US",
                },
                json={"code_data": device}
            ).json()
            if "error" in res:
                raise self.log.error(f"Unable to get code pair: {res['error_description']} [{res['error']}]")
            return res
