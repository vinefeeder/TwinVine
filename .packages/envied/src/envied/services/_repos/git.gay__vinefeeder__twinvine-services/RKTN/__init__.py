import base64
from copy import copy
import datetime
import hashlib
import hmac
import json
import re
import os
import sys
from typing import Optional
from aiohttp import CookieJar
from pymediainfo import MediaInfo
from langcodes import Language
import click
import urllib.parse
from requests import HTTPError
from envied.core.config import config
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.service import Service
from envied.core.titles import Title_T
from envied.core.titles.episode import Episode, Series
from envied.core.titles.movie import Movie, Movies
from envied.core.tracks.audio import Audio
from envied.core.tracks.chapters import Chapters
from envied.core.tracks.subtitle import Subtitle
from envied.core.tracks.tracks import Tracks
from envied.core.tracks.video import Video
from pyplayready.cdm import Cdm as PlayReadyCdm

class RKTN(Service):
    """
    Service code for Rakuten's Rakuten TV streaming service (https://rakuten.tv).

    \b
    Authorization: Credentials
    Security: FHD-UHD@L1, SD-FHD@L3; with trick

    \b
    Maximum of 3 audio tracks, otherwise will fail because Rakuten blocks more than 3 requests.
    Subtitles requests expires fast, so together with video and audio it will fail.
    If you want subs, use -S or -na -nv -nc, and download the rest separately.

    \b
    Command for Titles with no SDR (if not set range to HDR10 it will fail):
    uv run envied.dl -r HDR10 [OPTIONS] RKTN -m https://www.rakuten.tv/...

    \b
    TODO: - TV Shows are not yet supported as there's 0 TV Shows to purchase, rent, or watch in my region

    \b
    NOTES: - Only movies are supported as my region's Rakuten has no TV shows available to purchase at all
    """

    ALIASES = ["RakutenTV", "rakuten", "rakutentv"]
    TITLE_RE = r"^(?:https?://(?:www\.)?rakuten\.tv/([a-z]+/|)movies(?:/[a-z]{2})?/)(?P<id>[a-z0-9-]+)"
    LANG_MAP = {
        "es": "es-ES",
        "pt": "pt-PT",
    }
    @staticmethod
    @click.command(name="RakutenTV", short_help="https://rakuten.tv")
    @click.argument("title", type=str, required=False)
    @click.option(
        "-dev",
        "--device",
        default=None,
        type=click.Choice(
            [
                "web",  # Device: Web Browser - Maximum Quality: 720p - DRM: Widevine
                "android",  # Device: Android Phone - Maximum Quality: 720p - DRM: Widevine
                "atvui40",  # Device: AndroidTV - Maximum Quality: 2160p - DRM: Widevine
                "lgui40",  # Device: LG SMART TV - Maximum Quality: 2160p - DRM: Playready
                "smui40",  # Device: Samsung SMART TV - Maximum Quality: 2160p - DRM: Playready
            ],
            case_sensitive=True,
        ),
        help="The device you want to make requests with.",
    )
    @click.option(
        "-m", "--movie", is_flag=True, default=False, help="Title is a movie."
    )
    @click.option(
        "-dal", "--desired-audio-language", type=str, default="SPA,ENG", help="Select desired audio language tracks for this title. Default SPA,ENG. Separate multiple languages with a comma."
    )
    @click.pass_context
    def cli(ctx, **kwargs):
        return RKTN(ctx, **kwargs)

    def __init__(self, ctx, title, device, movie, desired_audio_language):
        super().__init__(ctx)
        #self.parse_title(ctx, title)
        self.title = title
        self.cdm = ctx.obj.cdm
        self.playready = isinstance(self.cdm, PlayReadyCdm)
        self.desired_audio_language = desired_audio_language
        self.range = ctx.parent.params.get("range_")[0].name or "SDR"
        self.vcodec = ctx.parent.params.get("vcodec") or Video.Codec.AVC # Defaults to H264
        self.resolution = "UHD" if (self.vcodec.extension.lower() == "h265" or self.range in ['HYBRID', 'HDR10', 'HDR10P', 'DV']) else "FHD"
        self.device = "lgui40" if self.playready else "android"
        self.movie = movie or "movies" in title
        self.audio_languages = []
        
        # set a custom device if provided
        if device is not None:
            self.device = device

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")
        
        self.session.headers.update(
            {
                "Origin": "https://rakuten.tv/",
                "User-Agent": "Mozilla/5.0 (Linux; Android 11; SHIELD Android TV Build/RQ1A.210105.003; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/99.0.4844.88 Mobile Safari/537.36",
            }
        )
        
    def get_titles(self):
        self.pair_device()
        
        if self.movie:
            endpoint = self.config["endpoints"]["title"]
        else:
            endpoint = self.config["endpoints"]["show"]
            
        params = urllib.parse.urlencode(
            {
                "classification_id": self.classification_id,
                "device_identifier": self.config["clients"][self.device][
                    "device_identifier"
                ],
                "device_serial": self.config["clients"][self.device]["device_serial"],
                "locale": self.locale,
                "market_code": self.market_code,
                "session_uuid": self.session_uuid,
                "timestamp": f"{int(datetime.datetime.now().timestamp())}005",
                "support_closed_captions": "true",
            }
        )
        title_url = endpoint.format(
            title_id=self.title
        ) + params

        
        title = self.session.get(url=title_url).json()
        
        if "errors" in title:
            error = title["errors"][0]
            if error["code"] == "error.not_found":
                self.log.error(f"Title [{self.title}] was not found on this account.")
            else:
                self.log.error(
                    f"Unable to get title info: {error['message']} [{error['code']}]"
                )
            sys.exit(1)
            
        title = self.get_info(title["data"])

        if self.movie:
            
            return Movies(
                [
                    Movie(
                        id_=self.title,
                        service=self.__class__,
                        name=title["title"],
                        year=title["year"],
                        language="en",
                        data=title,
                        description=title["plot"],
                    )
                ]
            )
        else:
            episodes_list = []
            #title_ep = self.get_info(title["data"]['episodes'])
            for season in title["tv_show"]["seasons"]:
                data_season = endpoint.format(
                    title_id=season["id"]
                ) + params
                
                data = self.session.get(url=data_season).json() 
                
                if "errors" in data:
                    error = data["errors"][0]
                    if error["code"] == "error.not_found":
                        self.log.error(f"Season [{season['id']}] was not found on this account.")
                    else:
                        self.log.error(
                            f"Unable to get title info: {error['message']} [{error['code']}]"
                        )
                    continue

                for episode in data["data"]["episodes"]:
                    episodes_list.append(
                        Episode(
                            id_=episode["id"],
                            service=self.__class__,
                            title=episode["tv_show_title"],
                            season=episode["season_number"],
                            number=episode["number"],
                            name=episode["title"] or episode['display_name'],
                            description=episode["short_plot"],
                            year=episode["year"],
                            language="en",
                            data=episode,
                        )
                    )
                    
            return Series(episodes_list)


    
    def get_tracks(self, title: Title_T) -> Tracks:
        # Obtener tracks para todos los idiomas de audio disponibles
        all_tracks = None
        
        for audio_lang in self.audio_languages:
            self.log.info(f"Getting tracks for audio language: {audio_lang}")
            
            # Obtener stream info para este idioma específico
            stream_info = self.get_avod(audio_lang, title) if self.kind == "avod" else self.get_me(audio_lang, title)

            if "errors" in stream_info:
                error = stream_info["errors"][0]
                if "error.streaming.no_active_right" in stream_info["errors"][0]["code"]:
                    self.log.error(
                        " x You don't have the rights for this content\n   You need to rent or buy it first"
                    )
                else:
                    self.log.error(
                        f" - Failed to get track info: {error['message']} [{error['code']}]"
                    )
                sys.exit(1)
            
            stream_info = stream_info["data"]["stream_infos"][0]
            
            if all_tracks is None:
                # Primera iteración: crear el objeto tracks principal
                self.license_url = stream_info["license_url"]
                
                all_tracks = DASH.from_url(url=stream_info["url"], session=self.session).to_tracks(language=title.language)
                
                # Procesar subtítulos (solo una vez)
                subtitle_tracks = []
                for subtitle in stream_info.get("all_subtitles", []):
                    subtitle_tracks += [
                        Subtitle(
                            id_=hashlib.md5(subtitle["url"].encode()).hexdigest()[0:6],
                            url=subtitle["url"],
                            codec=Subtitle.Codec.from_mime(subtitle["format"]),
                            forced=subtitle["forced"],
                            language=subtitle["locale"],
                        )
                    ]
                
                all_tracks.add(subtitle_tracks)
            else:
                # Iteraciones adicionales: obtener tracks de audio adicionales
                temp_tracks = DASH.from_url(url=stream_info["url"], session=self.session).to_tracks(language=title.language)
                
                # Agregar solo los tracks de audio nuevos
                for audio_track in temp_tracks.audio:
                    # Verificar que no sea duplicado basado en el idioma y codec
                    is_duplicate = False
                    for existing_audio in all_tracks.audio:
                        if (existing_audio.language == audio_track.language and 
                            existing_audio.codec == audio_track.codec):
                            is_duplicate = True
                            break
                    
                    if not is_duplicate:
                        all_tracks.audio.append(audio_track)

        # Procesar HDR para videos
        for video in all_tracks.videos:
            if "HDR10" in video.url:
                video.range = Video.Range.HDR10

        # Aplicar el método append_tracks mejorado
        self.append_tracks(all_tracks)

        return all_tracks

    def get_chapters(self, title: Title_T) -> Chapters:

        return Chapters([])   

    def get_me(self, audio_language=None, title: Title_T = None):
        # Si no se especifica idioma, usar el primero disponible
        if audio_language is None:
            audio_language = self.audio_languages[0]
            
        stream_info_url = self.config["endpoints"]["manifest"].format(
            kind="me"
        ) + urllib.parse.urlencode(
            {
                "audio_language": audio_language,  # Usar el idioma especificado
                "audio_quality": "5.1",  # Will get better audio in different request to make sure it wont error
                "classification_id": self.classification_id,
                "content_id": title.id,
                "content_type": "movies" if self.movie else "episodes",
                "device_identifier": self.config["clients"][self.device][
                    "device_identifier"
                ],
                "device_serial": "not_implemented",
                "device_stream_audio_quality": "5.1",
                "device_stream_hdr_type": self.hdr_type,
                "device_stream_video_quality": self.resolution,
                "device_uid": "affa434b-8b7c-4ff3-a15e-df1fe500e71e",
                "device_year": self.config["clients"][self.device]["device_year"],
                "disable_dash_legacy_packages": "false",
                "gdpr_consent": self.config["gdpr_consent"],
                "gdpr_consent_opt_out": 0,
                "hdr_type": self.hdr_type,
                "ifa_subscriber_id": self.ifa_subscriber_id,
                "locale": self.locale,
                "market_code": self.market_code,
                "player": self.config["clients"][self.device]["player"],
                "player_height": 1080,
                "player_width": 1920,
                "publisher_provided_id": "046f58b1-d89b-4fa4-979b-a9bcd6d78a76",
                "session_uuid": self.session_uuid,
                "strict_video_quality": "false",
                "subtitle_formats": ["vtt"],
                "subtitle_language": "MIS",
                "timestamp": f"{int(datetime.datetime.now().timestamp())}122",
                "video_type": "stream",
            }
        )
        stream_info_url += "&signature=" + self.generate_signature(stream_info_url)
        return self.session.post(
            url=stream_info_url,
        ).json()

    def get_avod(self, audio_language=None, title: Title_T = None):
        # Si no se especifica idioma, usar el primero disponible
        if audio_language is None:
            audio_language = self.audio_languages[0]
            
        stream_info_url = self.config["endpoints"]["manifest"].format(
            kind="avod"
        ) + urllib.parse.urlencode(
            {
                "device_stream_video_quality": self.resolution,
                "device_identifier": self.config["clients"][self.device][
                    "device_identifier"
                ],
                "market_code": self.market_code,
                "session_uuid": self.session_uuid,
                "timestamp": f"{int(datetime.datetime.now().timestamp())}122",
            }
        )
        stream_info_url += "&signature=" + self.generate_signature(stream_info_url)
        return self.session.post(
            url=stream_info_url,
            data={
                "hdr_type": self.hdr_type,
                "audio_quality": "5.1",  # Will get better audio in different request to make sure it wont error
                "app_version": self.config["clients"][self.device]["app_version"],
                "content_id": title.id,
                "video_quality": self.resolution,
                "audio_language": audio_language,  # Usar el idioma especificado
                "video_type": "stream",
                "device_serial": self.config["clients"][self.device]["device_serial"],
                "content_type": "movies" if self.movie else "episodes",
                "classification_id": self.classification_id,
                "subtitle_language": "MIS",
                "player": self.config["clients"][self.device]["player"],
            },
        ).json()

    def generate_signature(self, url):
        up = urllib.parse.urlparse(url)
        digester = hmac.new(
            self.access_token.encode(),
            f"POST{up.path}{up.query}".encode(),
            hashlib.sha1,
        )
        return (
            base64.b64encode(digester.digest())
            .decode("utf-8")
            .replace("+", "-")
            .replace("/", "_")
        )
        


    def append_tracks(self, tracks):
        """
        Busca y agrega tracks adicionales de video y audio que no están en el manifest.
        """
        if not tracks.videos:
            self.log.warning("No video tracks found, skipping append_tracks")
            return
        
        # Buscar tracks de video adicionales
        self._append_video_tracks(tracks)
        
        # Buscar tracks de audio adicionales
        self._append_audio_tracks(tracks)


    def _append_video_tracks(self, tracks):
        """Busca y agrega tracks de video adicionales para H.264."""
        if not tracks.videos:
            return
        
        codec = tracks.videos[0].codec
        
        # Solo buscar tracks adicionales para H.264
        if codec != Video.Codec.AVC:
            self.log.debug(f"Skipping video track search (codec: {codec.name}, only works for AVC/H.264)")
            return
        
        # Extraer el patrón del codec de la URL
        url_pattern = tracks.videos[-1].url
        codec_match = re.search(r'(avc1|h264)-(\d+)', url_pattern, re.IGNORECASE)
        
        if not codec_match:
            self.log.debug("Could not find codec pattern in URL for video track search")
            return
        
        codec_prefix = codec_match.group(1)  # "avc1" o "h264"
        self.log.info(f"Searching for additional H.264 video tracks (pattern: {codec_prefix})...")
        

        # Usar el directorio temp de.envied
        temp_file = os.path.join(str(config.directories.temp), "video_test.mp4")
    
        
        tracks_found = 0
        
        for n in range(100):
            # Generar URL del siguiente track
            current_number = len(tracks.videos) + 1
            ismv = re.sub(
                rf"{codec_prefix}-\d+",
                rf"{codec_prefix}-{current_number}",
                tracks.videos[-1].url,
            )
            
            # Verificar si existe
            try:
                response = self.session.head(ismv, timeout=5)
                if response.status_code != 200:
                    self.log.debug(f"Video track search ended at index {current_number}")
                    break
            except Exception as e:
                self.log.debug(f"Video track search failed: {e}")
                break
            
            # Crear copia del último video track
            video = copy(tracks.videos[-1])
            video.url = ismv
            video.id_ = hashlib.md5(ismv.encode()).hexdigest()[:16]
            
            # Descargar chunk para obtener info con MediaInfo
            try:
                with open(temp_file, "wb") as chunkfile:
                    data = self.session.get(
                        url=ismv, 
                        headers={"Range": "bytes=0-50000"},
                        timeout=10
                    )
                    chunkfile.write(data.content)
                
                # Parsear con MediaInfo
                info = MediaInfo.parse(temp_file)
                
                if not info.video_tracks:
                    self.log.debug(f"No video info found for track {current_number}")
                    continue
                
                video_info = info.video_tracks[0]
                video.height = video_info.height
                video.width = video_info.width
                video.bitrate = video_info.maximum_bit_rate or video_info.bit_rate
                
                # Agregar el track
                tracks.videos.append(video)
                tracks_found += 1
                self.log.info(
                    f"  + Added video track #{current_number}: "
                    f"{video.width}x{video.height} @ {video.bitrate} bps"
                )
                
            except Exception as e:
                self.log.warning(f"Failed to process video track {current_number}: {e}")
                break
            finally:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
        
        if tracks_found > 0:
            self.log.info(f"Total additional video tracks found: {tracks_found}")


    def _append_audio_tracks(self, tracks):
        """Busca y agrega tracks de audio adicionales para todos los idiomas seleccionados."""
        if not tracks.audio:
            self.log.warning("No audio tracks found to use as base")
            return
        
        if not hasattr(self, 'audio_languages') or not self.audio_languages:
            self.log.debug("No audio languages configured")
            return
        
        self.log.info(f"Searching for additional audio tracks in languages: {self.audio_languages}")
        
        # Codecs a probar (en orden de preferencia)
        codecs_to_try = ["ec-3", "ac-3", "dts", "mp4a"]
        
        # Usar el directorio temp de.envied
        temp_file = os.path.join(str(config.directories.temp), "audio_test.mp4")
    
        
        base_audio = tracks.audio[0]
        base_url = base_audio.url
        
        tracks_found = 0
        
        for language in self.audio_languages:
            for codec_name in codecs_to_try:
                # Generar URL del track
                # Patrón: audio-{LANG}-{CODEC}-{NUMBER}
                isma = re.sub(
                    r"audio-[a-zA-Z]{2,3}-[a-z0-9\-]+-\d+",
                    f"audio-{language.lower()}-{codec_name}-1",
                    base_url,
                )
                
                # Verificar si existe
                try:
                    response = self.session.head(isma, timeout=5)
                    if response.status_code != 200:
                        continue
                except Exception:
                    continue
                
                # Verificar si ya existe (evitar duplicados)
                if any(audio.url == isma for audio in tracks.audio):
                    self.log.debug(f"Audio track already exists: {language}-{codec_name}")
                    continue
                
                # Crear nuevo track de audio
                audio = copy(base_audio)
                audio.url = isma
                audio.id_ = hashlib.md5(isma.encode()).hexdigest()[:16]
                
                # Mapear idioma
                mapped_lang = self.LANG_MAP.get(language, language)
                audio.language = Language.get(mapped_lang)
                
                # Determinar si es idioma original
                if tracks.videos:
                    audio.is_original_lang = (
                        audio.language.language == tracks.videos[0].language.language
                    )
                
                # Obtener información del track con MediaInfo
                try:
                    with open(temp_file, "wb") as bytetest:
                        data = self.session.get(
                            url=isma, 
                            headers={"Range": "bytes=0-50000"},
                            timeout=10
                        )
                        bytetest.write(data.content)
                    
                    info = MediaInfo.parse(temp_file)
                    
                    if not info.audio_tracks:
                        self.log.debug(f"No audio info found for {language}-{codec_name}")
                        continue
                    
                    audio_info = info.audio_tracks[0]
                    audio.bitrate = audio_info.bit_rate
                    
                    # Detectar canales basado en codec
                    if codec_name in ["ec-3", "ac-3", "dts"]:
                        audio.channels = audio_info.channel_s or "5.1"
                    else:  # mp4a (AAC)
                        audio.channels = audio_info.channel_s or "2.0"
                    
                    # Actualizar codec
                    # Para envied. necesitas mantener el formato correcto
                    audio.codec = Audio.Codec.from_codecs(codec_name)
                    
                    # Agregar el track
                    tracks.audio.append(audio)
                    tracks_found += 1
                    
                    self.log.info(
                        f"  + Added audio track: {audio.language.display_name()} "
                        f"[{codec_name.upper()}] - {audio.channels}ch @ {audio.bitrate} bps"
                    )
                    
                except Exception as e:
                    self.log.debug(f"Failed to process audio {language}-{codec_name}: {e}")
                finally:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
        
        if tracks_found > 0:
            self.log.info(f"Total additional audio tracks found: {tracks_found}")

    def get_widevine_service_certificate(self, **kwargs):
        return self.config["certificate"]

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        res = self.session.post(
            url=self.license_url,
            data=challenge,
        )

        if "errors" in res.text:
            res = res.json()
            if res["errors"][0]["message"] == "HttpException: Forbidden":
                self.log.error(
                    " x This CDM is not eligible to decrypt this\n"
                    "   content or has been blacklisted by RakutenTV"
                )
            elif res["errors"][0]["message"] == "HttpException: An error happened":
                self.log.error(
                    " x This CDM seems to be revoked and\n"
                    "   therefore it can't decrypt this content",
                )
            
            sys.exit(1)
            
        return res.content
    
    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        res = self.session.post(
            url=self.license_url,
            data=challenge,
        )

        if "errors" in res.text:
            res = res.json()
            if res["errors"][0]["message"] == "HttpException: Forbidden":
                self.log.error(
                    " x This CDM is not eligible to decrypt this\n"
                    "   content or has been blacklisted by RakutenTV"
                )
            elif res["errors"][0]["message"] == "HttpException: An error happened":
                self.log.error(
                    " x This CDM seems to be revoked and\n"
                    "   therefore it can't decrypt this content",
                )
            
            sys.exit(1)
            
        return res.content

        
    def pair_device(self):
        # TODO: Make this return the tokens, move print out of the func
        # log.info_("Logging into RakutenTV as an Android device")
        if not self.credential:
            self.log.error(" - No credentials provided, unable to log in.")
            sys.exit(1)
        try:
            res = self.session.post(
                url=self.config["endpoints"]["auth"],
                params={
                    "device_identifier": self.config["clients"][self.device][
                        "device_identifier"
                    ]
                },
                data={
                    "app_version": self.config["clients"][self.device]["app_version"],
                    "device_metadata[uid]": self.config["clients"][self.device][
                        "device_serial"
                    ],
                    "device_metadata[os]": self.config["clients"][self.device][
                        "device_os"
                    ],
                    "device_metadata[model]": self.config["clients"][self.device][
                        "device_model"
                    ],
                    "device_metadata[year]": self.config["clients"][self.device][
                        "device_year"
                    ],
                    "device_serial": self.config["clients"][self.device][
                        "device_serial"
                    ],
                    "device_metadata[trusted_uid]": False,
                    "device_metadata[brand]": self.config["clients"][self.device][
                        "device_brand"
                    ],
                    "classification_id": 69,
                    "user[password]": self.credential.password,
                    "device_metadata[app_version]": self.config["clients"][self.device][
                        "app_version"
                    ],
                    "user[username]": self.credential.username,
                    "device_metadata[serial_number]": self.config["clients"][
                        self.device
                    ]["device_serial"],
                },
            ).json()
        except HTTPError as e:
            if e.response.status_code == 403:
                self.log.error(
                    " - Rakuten returned a 403 (FORBIDDEN) error. "
                    "This could be caused by your IP being detected as a proxy, or regional issues. Cannot continue."
                )
        if "errors" in res:
            error = res["errors"][0]
            if "exception.forbidden_vpn" in error["code"]:
                self.log.error(" x RakutenTV is detecting this VPN or Proxy")
            else:
                self.log.error(f" - Login failed: {error['message']} [{error['code']}]")
        self.access_token = res["data"]["user"]["access_token"]
        self.ifa_subscriber_id = res["data"]["user"]["avod_profile"][
            "ifa_subscriber_id"
        ]
        self.session_uuid = res["data"]["user"]["session_uuid"]
        self.classification_id = res["data"]["user"]["profile"]["classification"]["id"]
        self.locale = res["data"]["market"]["locale"]
        self.market_code = res["data"]["market"]["code"]
        
    def get_info(self, title):
        self.kind = title["labels"]["purchase_types"][0]["kind"]

        # self.available_resolutions = [x for x in title["labels"]["video_qualities"]]
        # if any(x["abbr"] == "UHD" for x in title["labels"]["video_qualities"]):
        # 		self.resolution = "UHD"
        # elif any(x["abbr"] == "FHD" for x in title["labels"]["video_qualities"]):
        # 		self.resolution = "FHD"
        # elif any(x["abbr"] == "HD" for x in title["labels"]["video_qualities"]):
        # 		self.resolution = "HD"
        # else:
        # 		self.resolution = "SD"

        self.available_hdr_types = [x for x in title["labels"]["hdr_types"]]
        if any(x["abbr"] == "HDR10_PLUS" for x in self.available_hdr_types) and any(
            x["abbr"] == "HDR10_PLUS"
            for x in title["view_options"]["support"]["hdr_types"]
        ):
            self.hdr_type = "HDR10_PLUS"
        elif any(x["abbr"] == "DOLBY_VISION" for x in self.available_hdr_types) and any(
            x["abbr"] == "DOLBY_VISION"
            for x in title["view_options"]["support"]["hdr_types"]
        ):
            self.hdr_type = "DOLBY_VISION"
        elif any(x["abbr"] == "HDR10" for x in self.available_hdr_types) and any(
            x["abbr"] == "HDR10" for x in title["view_options"]["support"]["hdr_types"]
        ):
            self.hdr_type = "HDR10"

        else:
            self.hdr_type = "NONE"

        # Obtener view_options desde title o episodes
        view_options = title.get("episodes", [{}])[0].get("view_options") or title.get("view_options")

        # FIJO: Obtener TODOS los idiomas de audio disponibles
        if len(view_options["private"]["offline_streams"]) == 1:
            # Caso 1: Un solo stream con múltiples idiomas
            self.audio_languages = [
                x["abbr"]
                for x in view_options["private"]["streams"][0]["audio_languages"]
            ]
        else:
            # Caso 2: Múltiples streams, obtener todos los idiomas únicos
            all_audio_languages = []
            for stream in view_options["private"]["streams"]:
                for audio_lang in stream["audio_languages"]:
                    if audio_lang["abbr"] not in all_audio_languages:
                        all_audio_languages.append(audio_lang["abbr"])
            self.audio_languages = all_audio_languages

        # # TODO: Look up only for languages chosen by the user
        # print(f"\nAvailable audio languages: {', '.join(self.audio_languages)}")
        # selected = input("Type your desired languages, maximum of 3, UPPER CASE (ex: ENG,SPA,FRA): ")

        selected_langs = [lang.strip() for lang in self.desired_audio_language.split(",") if lang.strip() in self.audio_languages]
        if not selected_langs:
            self.log.error("No selected language. Exiting.")
        self.audio_languages = selected_langs

        # Log para debug
        self.log.info(f"Selected audio languages: {self.audio_languages}")

        return title