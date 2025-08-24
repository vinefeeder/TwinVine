import logging
from abc import ABCMeta, abstractmethod
from http.cookiejar import CookieJar
from typing import Optional, Union, Any
from urllib.parse import urlparse
import click
import requests
from requests.adapters import HTTPAdapter, Retry
from rich.padding import Padding
from rich.rule import Rule
from envied.core.service import Service
from envied.core.titles import Movies, Movie, Titles_T, Title_T
from envied.core.cacher import Cacher
from envied.core.config import config
from envied.core.console import console
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.tracks import Chapters, Tracks, Subtitle, Chapter
from envied.core.utilities import get_ip_info
from envied.core.manifests import HLS, DASH

from bs4 import BeautifulSoup
import hashlib
import base64
import re
import json

class YTBE(Service):
    """
    \b
    YTBE = Service code for Youtube vod (https://youtube.com)

    \b
    Version: 1.0.0
    Author: sk8ord13
    Authorization: Cookies
    Robustness:
      Widevine:
        L3: 1080p

    """

    GEOFENCE = ('en',)

    LICENSE_SERVER_URL: str = 'https://www.youtube.com/youtubei/v1/player/get_drm_license'
    YOUTUBE_VIDEO_INFO_URL: str = 'https://www.youtube.com/youtubei/v1/player'
    API_KEY: str = 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8'

    @staticmethod
    @click.command(name="YTBE", short_help="https://youtube.com", help=__doc__)
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return YTBE(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        self.last_response_data: dict[str, Any] = {}
        super().__init__(ctx)

    def get_titles(self) -> Titles_T:
        youtube_url = f'https://www.youtube.com/watch?v={self.title}'

        cookies_d: dict[str, str] = self.session.cookies.get_dict()

        sapisid = cookies_d.get('__Secure-3PAPISID', '')
        epoch = "1699161578"
        origin = "https://www.youtube.com"
        data = f"{epoch} {sapisid} {origin}"
        sha1 = hashlib.sha1()
        sha1.update(data.encode('utf-8'))
        sapisidhash = f"SAPISIDHASH {epoch}_{sha1.hexdigest()}"

        response = self.session.get(youtube_url)
        soup = BeautifulSoup(response.text, 'html.parser')

        yt_scripts = soup.find_all('script', string=re.compile('ytcfg.set'))

        user_agent_extracted = "YouTube/15.49.4 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.45 Mobile Safari/537.36 EdgA/46.0.0.1 GoogleTV/YouTube/16.12.34 (compatible; Widevine/1.4.8)"
        client_name_extracted = "null"
        client_version_extracted = "null"
        id_token_extracted = "null"
        vision_data_extracted = "null"
        session_id_extracted = "null"
        logged_yt_extracted = False

        for yt_script in yt_scripts:
            yt_script_content = yt_script.string
            try:
                match = re.search(r'ytcfg\.set\((.*?)\);', yt_script_content, re.DOTALL)
                if match:
                    ytcfg_set_content = json.loads(match.group(1))
                    id_token_extracted = ytcfg_set_content.get("ID_TOKEN", id_token_extracted)
                    vision_data_extracted = ytcfg_set_content.get("INNERTUBE_CONTEXT", {}).get("client", {}).get("visitorData", vision_data_extracted)
                    session_id_extracted = ytcfg_set_content.get("SESSION_INDEX", session_id_extracted)
                    client_context = ytcfg_set_content.get('INNERTUBE_CONTEXT', {}).get('client', {})
                    user_agent_extracted = client_context.get('userAgent', user_agent_extracted)
                    client_version_extracted = client_context.get('clientVersion', client_version_extracted)
                    client_name_extracted = client_context.get('clientName', client_name_extracted)
                    logged_yt_extracted = ytcfg_set_content.get("LOGGED_IN", logged_yt_extracted)
            except json.JSONDecodeError:
                pass
        
        headers = {
            'authorization': sapisidhash,
            'origin': origin,
            'user-agent': user_agent_extracted,
            'X-YouTube-Client-Version': client_version_extracted,
            'X-Youtube-Identity-Token': id_token_extracted,
            'x-goog-authuser': session_id_extracted,
            'x-goog-visitor-id': vision_data_extracted,
            'x-youtube-bootstrap-logged-in': f"{logged_yt_extracted}",
            'x-youtube-client-version': client_version_extracted
        }

        params_get_titles = {'key': self.API_KEY}

        json_data_payload = {
            'context': {
                'client': {
                    'userAgent': user_agent_extracted,
                    'clientName': client_name_extracted,
                    'clientVersion': client_version_extracted,
                },
            },
            'videoId': self.title
        }

        response_data = self.session.post(self.YOUTUBE_VIDEO_INFO_URL, params=params_get_titles, headers=headers, json=json_data_payload).json()

        streaming_data = response_data.get('streamingData')
        if not streaming_data:
            return Movies([])

        title_name = response_data['videoDetails']['title']
        
        self.last_response_data = response_data
        self.last_response_data['ytcfg_params'] = {
            'user_agent': user_agent_extracted,
            'client_name': client_name_extracted,
            'client_version': client_version_extracted,
            'id_token': id_token_extracted,
            'vision_data': vision_data_extracted,
            'session_id': session_id_extracted,
            'logged_yt': logged_yt_extracted,
            'video_id': self.title
        }
        self.last_response_data['request_headers'] = headers
        
        return Movies([Movie(
            id_=self.title,
            service=self.__class__,
            name=title_name,
            year=None,
            language=None
        )])

    def get_tracks(self, title_obj: Title_T) -> Tracks:
        response_data = self.last_response_data
        dashManifestUrl = response_data.get('streamingData', {}).get('dashManifestUrl')

        return DASH.from_url(dashManifestUrl, session=self.session).to_tracks(language="en")

    def get_chapters(self, title: Title_T) -> Chapters:
        return []

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[str]:
        response_data = self.last_response_data
        drm_params = response_data.get("streamingData", {}).get("drmParams")

        ytcfg_params = response_data.get('ytcfg_params', {})
        user_agent = ytcfg_params.get('user_agent')
        clientName = ytcfg_params.get('client_name')
        clientVersion = ytcfg_params.get('client_version')
        video_id = ytcfg_params.get('video_id')
        session_id = ytcfg_params.get('session_id')

        request_headers = response_data.get('request_headers', {})

        lic_url = self.LICENSE_SERVER_URL

        params_license = {'key': self.API_KEY}

        json_data = {
            'context': {
                'client': {
                    'userAgent': user_agent,
                    'clientName': clientName,
                    'clientVersion': clientVersion,
                },
            },
            'drmSystem': 'DRM_SYSTEM_WIDEVINE',
            'videoId': video_id,
            'cpn': 'MsQQaCE9gAkD9iLF',
            'sessionId': session_id,
            'drmParams': drm_params
        }

        json_data["licenseRequest"] = base64.b64encode(challenge).decode("utf-8")

        get_res = requests.post(lic_url, params=params_license, headers=request_headers, json=json_data).json()
        license_b64 = get_res.get("license")
        if license_b64:
            return license_b64.replace("-", "+").replace("_", "/")
