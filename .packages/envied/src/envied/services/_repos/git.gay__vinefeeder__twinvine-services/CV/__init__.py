import base64
from hashlib import md5
from http.cookiejar import CookieJar
import json
import re
import sys
from typing import Optional, Union
import click
from langcodes import Language
import requests
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.service import Service
from envied.core.titles import Title_T
from envied.core.titles.episode import Episode, Series
from envied.core.titles.movie import Movie, Movies
from envied.core.tracks.subtitle import Subtitle
from envied.core.tracks.tracks import Tracks
from envied.core.tracks.video import Video
from envied.core.utilities import is_close_match
from envied.core.utils.collections import as_list


class CV(Service):
    """
    Service code for ClaroVideo streaming service (https://www.clarovideo.com).

    \b
    Authorization: Credentials
    Security: FHD@L3
    """

    ALIASES = ("CV", "ClaroVideo", "CLVD")
    #TITLE_RE = [r"https?://(?:www\.)?clarovideo.com/(?P<region>[\w-]+)/vcard/(?:[\w-]+/)?(?P<id>\d+)"]
    TITLE_RE = r"https?://(?:www\.)?clarovideo\.com/(?P<region>[\w-]+)/vcard/(?:.*/)?(?P<id>\d+)/?$"
    LANGUAGE_MAP = {
        "AR": "es-AR", "BO": "es-BO", "BR": "pt-BR", "CA": "en-CA", "CL": "es-CL",
        "CO": "es-CO", "CR": "es-CR", "CU": "es-CU", "DO": "es-DO", "EC": "es-EC",
        "GT": "es-GT", "HN": "es-HN", "MX": "es-MX", "NI": "es-NI", "PA": "es-PA",
        "PE": "es-PE", "PR": "es-PR", "PY": "es-PY", "SV": "es-SV", "US": "en-US",
        "UY": "es-UY", "VE": "es-VE", "AT": "de-AT", "BE": "nl-BE", "BG": "bg-BG",
        "CH": "de-CH", "CZ": "cs-CZ", "DE": "de-DE", "DK": "da-DK", "EE": "et-EE",
        "ES": "es-ES", "FI": "fi-FI", "FR": "fr-FR", "GB": "en-GB", "UK": "en-GB",
        "GR": "el-GR", "HR": "hr-HR", "HU": "hu-HU", "IE": "en-IE", "IS": "is-IS",
        "IT": "it-IT", "LT": "lt-LT", "LU": "lb-LU", "LV": "lv-LV", "MT": "mt-MT",
        "NL": "nl-NL", "NO": "nb-NO", "PL": "pl-PL", "PT": "pt-PT", "RO": "ro-RO",
        "RU": "ru-RU", "SE": "sv-SE", "SI": "sl-SI", "SK": "sk-SK", "UA": "uk-UA",
        "AE": "ar-AE", "CN": "zh-CN", "HK": "zh-HK", "ID": "id-ID", "IL": "he-IL",
        "IN": "hi-IN", "IQ": "ar-IQ", "IR": "fa-IR", "JP": "ja-JP", "KH": "km-KH",
        "KR": "ko-KR", "KW": "ar-KW", "MY": "ms-MY", "PH": "fil-PH", "PK": "ur-PK",
        "QA": "ar-QA", "SA": "ar-SA", "SG": "en-SG", "SY": "ar-SY", "TH": "th-TH",
        "TR": "tr-TR", "TW": "zh-TW", "VN": "vi-VN", "DZ": "ar-DZ", "EG": "ar-EG",
        "ET": "am-ET", "GH": "en-GH", "KE": "sw-KE", "LY": "ar-LY", "MA": "ar-MA",
        "MU": "en-MU", "NG": "en-NG", "TN": "ar-TN", "ZA": "en-ZA", "AU": "en-AU",
        "FJ": "en-FJ", "NZ": "en-NZ", "PG": "en-PG"
    }


    @staticmethod
    @click.command(name="CV", short_help="https://www.clarovideo.com")
    @click.argument("title", type=str, required=False)
    @click.option("--master", type=str, required=False, default="ORIGINAL", help="Get the selected master")
    @click.pass_context
    def cli(ctx, **kwargs):
        return CV(ctx, **kwargs)

    def __init__(self, ctx, title: str, master: str):
        super().__init__(ctx)
        m = self.parse_title(ctx, title)
        #self.movie = movie or m.get("type") == "filme"
        self.region = m["region"]
        self.master = master

        self.log.warning(f"Selected Master: '{self.master}'")
        
## Service specific methods
    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for login.")
        
        # configure account service 
        self.configure()

    def get_titles(self):
        self.config["params"]["group_id"] = self.title

        try:
            res = self.session.get(
                url=self.config["endpoints"]["data"],
                params=self.config["params"]
            )
            res.raise_for_status()
            data_full = res.json()
            metadata = data_full["response"]["group"]["common"]
        except Exception as e:
            self.log.error(f" + Failed to retrieve title metadata: {e}")
            raise

        # Referencias directas para evitar accesos repetitivos
        media = metadata["extendedcommon"]["media"]
        self.encode = "dashwv_ma"
        self.movie = "episode" not in media

        title_name = media["originaltitle"]
        release_year = media["publishyear"]

        # Limpieza en la obtención del lenguaje
        country_code = str(media.get("countryoforigin", {}).get("code", "")).upper()
        original_lang = self.LANGUAGE_MAP.get(country_code, "en")
        self.log.info(f"Original Language: {original_lang}")

        if self.movie:
            return Movies([
                Movie(
                    id_=metadata["id"],
                    service=self.__class__,
                    name=title_name,
                    year=release_year,
                    language=original_lang,
                    data=metadata
                )
            ])
        
        
        # TV Shows - Novels and Series
        titles = []

        try:
            self.config["params"]["group_id"] = self.title
            response = self.session.get(
                url=self.config["endpoints"]["serie"],
                params=self.config["params"],
            )
            response.raise_for_status() 
            data = response.json()

        except Exception as e:
            self.log.error(f" + Failed to retrieve title metadata: {e}")
            raise e

        else:
            try:
                seasons = data["response"]["seasons"]
                for season in seasons:
                    for episode in season["episodes"]:
                        titles.append(
                            Episode(
                                id_=episode["id"],
                                service=self.__class__,
                                title=title_name,
                                season=episode['season_number'],
                                number=episode['episode_number'],
                                name=episode['title_episode'],
                                year=release_year,
                                language=original_lang,
                                data=episode,
                            )
                        )
            except KeyError as e:
                self.log.error(f" + API response structure changed: Missing key {e}")
                raise e

            return Series(titles)

    def get_tracks(self, title: Title_T) -> Tracks:
        #Define individual parameters for payway and data endpoints
        payway_params = self.config["payway_params"].copy()
        self.config["params"]["group_id"] = title.id
        payway_params["group_id"] = title.id
        
        # Request payway token
        response = self.session.get(
            url=self.config["endpoints"]["payway"],
            params=payway_params,
        ).json()
        
        if not response.get("response", {}).get("playButton", {}).get("payway_token"):
            self.log.warning("The user does not have access to this content")
            sys.exit(1)
            
        payway_token = response["response"]["playButton"]["payway_token"]
        
        # Request title data
        response = self.session.get(url=self.config["endpoints"]["data"], params=self.config["params"]).json()
        title_data = response["response"]["group"]["common"]

        title_audios = [
            x
            for x in title_data["extendedcommon"]["media"]["language"]["options"]["option"]
            if not x["option_name"] == "subbed"
        ]
        
        original_master = next((x for x in title_audios if x["audio"] == "ORIGINAL"), None)
        if not original_master:
            self.log.warning("Original master not found.")
            original_master = title_audios[0]

        original_encode = "dashwv_ma" if "dashwv_ma" in original_master["encodes"] else "dashwv"
        payway_params["stream_type"] = original_encode
        payway_params["user_hash"] = self.user_info["session_userhash"]

        response = self.session.post(
            url=self.config["endpoints"]["media"],
            params=payway_params,
            data={"user_token": self.user_info["user_token"], "payway_token": payway_token},
        ).json()

        if not response.get("response"):
            raise ValueError(response)
        
        original_manifest = response["response"]
        _ = self.session.get(original_manifest["tracking"]["urls"]["stop"], params={"timecode": 0}).json()

        missing_audio = [
            x for x in title_audios if x["audio"] not in original_manifest["media"].get("audio", {}).get("options", [])
        ]
        if missing_audio and not next((x for x in missing_audio if x["audio"] == self.master), None):
            self.log.warning(
                f"This title has {len(missing_audio) + 1} separate Manifests, alternative master found: "
                f"{[x['audio'] for x in missing_audio]}, "
                f"you can select master with the --master flag"
            )

        manifest = original_manifest
        if not self.master == "ORIGINAL":
            _ = self.session.get(original_manifest["tracking"]["urls"]["dubsubchange"], params={"timecode": 0}).json()

            master_info = next((x for x in original_manifest['language']['options'] if x["option_id"] == f"D-{self.master}"), None)
            if not master_info:
                raise ValueError(
                    f"Master '{self.master}' not found, available masters: {', '.join(x['audio'] for x in title_audios)}"
                )

            encode = "dashwv_ma" if "dashwv_ma" in master_info["encodes"] else "dashwv"

            payway_params["content_id"] = master_info['content_id']
            payway_params["preferred_audio"] = self.master
            payway_params["stream_type"] = encode
            payway_params["user_hash"] = self.user_info["session_userhash"]

            response = self.session.post(
                url=self.config["endpoints"]["media"],
                params=payway_params,
                data={"user_token": self.user_info["user_token"], "payway_token": payway_token},
            ).json()
            if not response.get("response"):
                raise ValueError(response)

            manifest = response["response"]
            _ = self.session.get(manifest["tracking"]["urls"]["stop"], params={"timecode": 0}).json()
            self.log.info(manifest)
        mpd_url = manifest["media"]["video_url"]

        manifest_language = (
            title.language 
            if manifest["media"].get("audio", {}).get("selected", "") == "ORIGINAL"
            else manifest["media"].get("audio", {}).get("selected", "")
        )

        tracks = DASH.from_url(url=mpd_url, session=self.session).to_tracks(language=manifest_language)
        
        # remove subtitles track as they are not available in ClaroVideo DASH manifests
        tracks.subtitles.clear()  # No subtitles available in ClaroVideo DASH manifests
        if manifest["media"].get("subtitles"):
            for _, subtitle in manifest["media"]["subtitles"]["options"].items():
                tracks.add(Subtitle(
                    id_=md5(subtitle["external"].encode()).hexdigest(),
                    url=subtitle['external'],
                    # metadata
                    codec=Subtitle.Codec.WebVTT, 
                    language=subtitle["internal"],
                    #is_original_lang=title.original_lang and is_close_match(sub["languageCode"], [title.original_lang]),
                    #forced="ForcedNarrative" in sub["type"],
                    #sdh=sub["type"].lower() == "sdh"  # TODO: what other sub types? cc? forced?
                ), warn_only=True)  # expecting possible dupes, ignore
                
        # Extraemos los segundos del JSON
        duration_in_seconds = manifest['media']['duration'].get('seconds', 0)

        for track in tracks:
            track.extra = {"manifest": manifest}
            #track.needs_proxy = True
            if str(track.language) == "or" or str(track.language) == "und":
                track.language = Language.get(manifest_language)
            if str(track.language) == "pt":
                track.language = Language.get("pt-BR")
            if str(track.language) == "es":
                track.language = Language.get("es-419")
                
            track.is_original_lang = is_close_match(track.language, [title.language])
            track.name =  Language.get(track.language).display_name()
            
            #FileSize
            if isinstance(track, Video) and duration_in_seconds > 0 and track.bitrate:
                track.extra = {'size': int((track.bitrate * duration_in_seconds) / 8)}
        
        return tracks
                
    def get_chapters(self, title):
        return []
    
    def get_widevine_service_certificate(self, *, challenge: bytes, title, track) -> Optional[bytes]:
        return None
    
    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        challenge_b64 = base64.b64encode(challenge).decode()

        manifest_info = track.extra["manifest"]
        challenge_info = json.loads(manifest_info["media"]["challenge"])

        payload = {"token": challenge_info["token"], "device_id": self.device_id, "widevineBody": challenge_b64}

        response = requests.post(
            url=manifest_info["media"]["server_url"],
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
                "Referer": "https://www.clarovideo.com/",
                "Origin": "https://www.clarovideo.com",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            json=payload,
            proxies=self.session.proxies,
        )
        if not response.ok:
            raise ValueError(response.text)

        return response.content
    
    def configure(self):
        self.log.info(" + Logging in...")

        try:
            self.session.headers.update({
                "Origin": "https://www.clarovideo.com",
                "Referer": "https://www.clarovideo.com/",
            })
            response = self.session.post(
                url=self.config["endpoints"]["login"],
                params=self.config["params"],
                data={"username": self.credential.username, "password": self.credential.password},
            )
            
            response.raise_for_status()
            response = response.json()
        
            #self.log.info(json.dumps(response, indent=4))
            if "errors" in response:
                self.log.error(f"Login failed: {response['errors']['error']}")
                sys.exit(1)
                
            self.user_info = response["response"]
            self.config["params"]["user_id"] = self.user_info["user_id"]

            self.device_id = self.get_device_id(self.user_info["session_stringvalue"])

            self.config["payway_params"]["region"] = self.region
            self.config["payway_params"]["device_id"] = self.device_id
            self.config["payway_params"]["HKS"] = f"({self.user_info['session_stringvalue']})"
            self.config["payway_params"]["user_id"] = self.user_info["user_id"]
            self.log.info(" + Login successful")
            
        except Exception as e:
            self.log.error(f" + Login failed: {e}")

    def get_device_id(self, user_hks) -> str:
        self.config["params"]["HKS"] = user_hks

        response = self.session.post(
            url=self.config["endpoints"]["device"],
            params=self.config["params"],
        ).json()["response"]

        device_id = next(x["real_device_id"] for x in response["devices"] if x["device_category"] == "web")

        return device_id
    
    def parse_title(self, ctx, title):
        title = title or ctx.parent.params.get("title")
        if not title:
            self.log.error(" - No title ID provided")
        if not getattr(self, "TITLE_RE"):
            self.title = title
            return {}
        for regex in as_list(self.TITLE_RE):
            m = re.search(regex, title)
            if m:
                self.title = m.group("id")
                return m.groupdict()
        self.log.warning(f" - Couldn't parse title ID from '{title!r}', using as-is")
        self.title = title