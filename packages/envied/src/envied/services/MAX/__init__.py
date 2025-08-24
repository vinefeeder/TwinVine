import hashlib
import json
import re
import uuid
from datetime import datetime
from hashlib import md5
from typing import Optional, Union, Generator
from http.cookiejar import CookieJar

import click
import requests
import xmltodict
from langcodes import Language

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Subtitle, Tracks, Video


class MAX(Service):
    """
    Service code for MAX's streaming service (https://max.com).
    Version: 1.0.0

    Authorization: Cookies
    Security: UHD@L1 FHD@L1 HD@L3

    Use full URL or title ID with type.
    Examples:
    - https://play.hbomax.com/movie/urn:hbo:movie:GUID
    - https://play.hbomax.com/show/urn:hbo:series:GUID
    - movie/GUID
    - show/GUID
    
    Note: This service is designed for users who have legal access to MAX content.
    Ensure you have proper subscription and authentication before use.
    """

    ALIASES = ("MAX", "max", "hbomax")
    GEOFENCE = ("US",)

    TITLE_RE = r"^(?:https?://(?:www\.|play\.)?hbomax\.com/)?(?P<type>[^/]+)/(?P<id>[^/]+)"

    VIDEO_CODEC_MAP = {
        "H264": ["avc1"],
        "H265": ["hvc1", "dvh1"]
    }

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="MAX", short_help="https://max.com")
    @click.argument("title", type=str)
    @click.option("-vcodec", "--video-codec", default=None, help="Video codec preference")
    @click.option("-acodec", "--audio-codec", default=None, help="Audio codec preference")
    @click.pass_context
    def cli(ctx, **kwargs):
        return MAX(ctx, **kwargs)

    def __init__(self, ctx, title, video_codec, audio_codec):
        super().__init__(ctx)
        
        self.title = title
        self.vcodec = video_codec
        self.acodec = audio_codec
        
        # Get range parameter for HDR support
        range_param = ctx.parent.params.get("range_")
        self.range = range_param[0].name if range_param else "SDR"
        
        if self.range == 'HDR10':
            self.vcodec = "H265"

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")
        
        # Extract authentication tokens from cookies
        try:
            token = next(cookie.value for cookie in cookies if cookie.name == "st")
            session_data = next(cookie.value for cookie in cookies if cookie.name == "session")
            device_id = json.loads(session_data)
        except (StopIteration, json.JSONDecodeError):
            raise EnvironmentError("Required authentication cookies not found.")
        
        # Configure headers based on device type
        self.session.headers.update({
                'User-Agent': 'BEAM-Android/1.0.0.104 (SONY/XR-75X95EL)',
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-disco-client': 'SAMSUNGTV:124.0.0.0:beam:4.0.0.118',
                'x-disco-params': 'realm=bolt,bid=beam,features=ar',
                'x-device-info': 'beam/4.0.0.118 (Samsung/Samsung-Unknown; Tizen/124.0.0.0; f198a6c1-c582-4725-9935-64eb6b17c3cd/87a996fa-4917-41ae-9b6d-c7f521f0cb78)',
                'traceparent': '00-315ac07a3de9ad1493956cf1dd5d1313-988e057938681391-01',
                'tracestate': f'wbd=session:{device_id}',
                'Origin': 'https://play.hbomax.com',
                'Referer': 'https://play.hbomax.com/',
            })
        
        # Get device token
        auth_token = self._get_device_token()
        self.session.headers.update({
            "x-wbd-session-state": auth_token
        })

    def search(self) -> Generator[SearchResult, None, None]:
        """
        Search for content on MAX platform.
        Note: This is a basic implementation - MAX's search API may require additional parameters.
        """
        # Basic search implementation - you may need to adjust based on actual API
        search_url = "https://default.prd.api.hbomax.com/search"
        
        try:
            response = self.session.get(search_url, params={"q": self.title})
            response.raise_for_status()
            
            search_data = response.json()
            
            # Parse search results - adjust based on actual API response structure
            for result in search_data.get("results", []):
                yield SearchResult(
                    id_=result.get("id"),
                    title=result.get("title", "Unknown"),
                    label=result.get("type", "UNKNOWN").upper(),
                    url=f"https://play.hbomax.com/{result.get('type', 'content')}/{result.get('id')}"
                )
                
        except Exception as e:
            self.log.warning(f"Search functionality not fully implemented: {e}")
            # Return empty generator if search fails
            return
            yield  # This makes it a generator function

    def get_titles(self) -> Titles_T:
        # Parse title input
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError("Invalid title format. Expected format: type/id or full URL")
        
        content_type = match.group('type')
        external_id = match.group('id')
        
        response = self.session.get(
            self.config['endpoints']['contentRoutes'] % (content_type, external_id)
        )
        response.raise_for_status()
        
        try:
            content_data = [x for x in response.json()["included"] if "attributes" in x and "title" in 
                               x["attributes"] and x["attributes"]["alias"] == "generic-%s-blueprint-page" % (re.sub(r"-", "", content_type))][0]["attributes"]
            content_title = content_data["title"]
        except:
            content_data = [x for x in response.json()["included"] if "attributes" in x and "alternateId" in 
                               x["attributes"] and x["attributes"]["alternateId"] == external_id and x["attributes"].get("originalName")][0]["attributes"]
            content_title = content_data["originalName"]

        if content_type == "sport" or content_type == "event":
            included_dt = response.json()["included"]

            for included in included_dt:
                for key, data in included.items():
                    if key == "attributes":
                        for k, d in data.items():
                            if d == "VOD":
                                event_data = included

            release_date = event_data["attributes"].get("airDate") or event_data["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year

            return Movies([
                Movie(
                    id_=external_id,
                    service=self.__class__,
                    name=content_title.title(),
                    year=year,
                    data=event_data,
                )
            ])
        
        if content_type == "movie" or content_type == "standalone":
            metadata = self.session.get(
                url=self.config['endpoints']['moviePages'] % external_id
            ).json()['data']
            
            try:
                edit_id = metadata['relationships']['edit']['data']['id']
            except:
                for x in response.json()["included"]:
                    if x.get("type") == "video" and x.get("relationships", {}).get("show", {}).get("data", {}).get("id") == external_id:
                        metadata = x

            release_date = metadata["attributes"].get("airDate") or metadata["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year
            
            return Movies([
                Movie(
                    id_=external_id,
                    service=self.__class__,
                    name=content_title,
                    year=year,
                    data=metadata,
                )
            ])

        if content_type in ["show", "mini-series", "topical"]:
            episodes = []
            if content_type == "mini-series":
                alias = "generic-miniseries-page-rail-episodes"
            elif content_type == "topical":
                alias = "generic-topical-show-page-rail-episodes"
            else:
                alias = "-%s-page-rail-episodes-tabbed-content" % (content_type)

            included_dt = response.json()["included"]
            
            season_data = [data for included in included_dt for key, data in included.items()
                           if key == "attributes" for k, d in data.items() if alias in str(d).lower()][0]

            season_data = season_data["component"]["filters"][0]
            
            seasons = [int(season["value"]) for season in season_data["options"]]
            
            season_parameters = [(int(season["value"]), season["parameter"]) for season in season_data["options"]
                for season_number in seasons if int(season["value"]) == int(season_number)]

            if not season_parameters:
                raise ValueError("No seasons found")

            for (value, parameter) in season_parameters:
                data = self.session.get(
                    url=self.config['endpoints']['showPages'] % (external_id, parameter)
                ).json()
                
                try:
                    episodes_dt = sorted([dt for dt in data["included"] if "attributes" in dt and "videoType" in 
                                    dt["attributes"] and dt["attributes"]["videoType"] == "EPISODE" 
                                    and int(dt["attributes"]["seasonNumber"]) == int(parameter.split("=")[-1])], 
                                    key=lambda x: x["attributes"]["episodeNumber"])
                except KeyError:
                    raise ValueError("Season episodes were not found")
                
                episodes.extend(episodes_dt)
            
            episode_titles = []
            release_date = episodes[0]["attributes"].get("airDate") or episodes[0]["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year
            
            season_map = {int(item[1].split("=")[-1]): item[0] for item in season_parameters}

            for episode in episodes:
                episode_titles.append(
                    Episode(
                        id_=episode['id'],
                        service=self.__class__,
                        title=content_title,
                        season=season_map.get(episode['attributes'].get('seasonNumber')),
                        number=episode['attributes']['episodeNumber'],
                        name=episode['attributes']['name'],
                        year=year,
                        data=episode
                    )
                )

            return Series(episode_titles)

    def get_tracks(self, title: Title_T) -> Tracks:
        edit_id = title.data['relationships']['edit']['data']['id']
        
        response = self.session.post(
            url=self.config['endpoints']['playbackInfo'],
            json={
                'appBundle': 'beam',
                'consumptionType': 'streaming',
                'deviceInfo': {
                    'deviceId': '2dec6cb0-eb34-45f9-bbc9-a0533597303c',
                    'browser': {
                        'name': 'chrome',
                        'version': '113.0.0.0',
                    },
                    'make': 'Microsoft',
                    'model': 'XBOX-Unknown',
                    'os': {
                        'name': 'Windows',
                        'version': '113.0.0.0',
                    },
                    'platform': 'XBOX',
                    'deviceType': 'xbox',
                    'player': {
                        'sdk': {
                            'name': 'Beam Player Console',
                            'version': '1.0.2.4',
                        },
                        'mediaEngine': {
                            'name': 'GLUON_BROWSER',
                            'version': '1.20.1',
                        },
                        'playerView': {
                            'height': 1080,
                            'width': 1920,
                        },
                    },
                },
                'editId': edit_id,
                'capabilities': {
                    'manifests': {
                        'formats': {
                            'dash': {},
                        },
                    },
                'codecs': {
                    'video': {
                        'hdrFormats': [
                            'hlg',
                            'hdr10',
                            'dolbyvision5',
                            'dolbyvision8',
                        ],
                        'decoders': [
                            {
                                'maxLevel': '6.2',
                                'codec': 'h265',
                                'levelConstraints': {
                                    'width': {
                                        'min': 1920,
                                        'max': 3840,
                                    },
                                    'height': {
                                        'min': 1080,
                                        'max': 2160,
                                    },
                                    'framerate': {
                                        'min': 15,
                                        'max': 60,
                                    },
                                },
                                'profiles': [
                                    'main',
                                    'main10',
                                ],
                            },
                            {
                                'maxLevel': '4.2',
                                'codec': 'h264',
                                'levelConstraints': {
                                    'width': {
                                        'min': 640,
                                        'max': 3840,
                                    },
                                    'height': {
                                        'min': 480,
                                        'max': 2160,
                                    },
                                    'framerate': {
                                        'min': 15,
                                        'max': 60,
                                    },
                                },
                                'profiles': [
                                    'high',
                                    'main',
                                    'baseline',
                                ],
                            },
                        ],
                    },
                    'audio': {
                        'decoders': [
                            {
                                'codec': 'aac',
                                'profiles': [
                                    'lc',
                                    'he',
                                    'hev2',
                                    'xhe',
                                ],
                            },
                        ],
                    },
                },
                'devicePlatform': {
                    'network': {
                        'lastKnownStatus': {
                            'networkTransportType': 'unknown',
                        },
                        'capabilities': {
                            'protocols': {
                                'http': {
                                    'byteRangeRequests': True,
                                },
                            },
                        },
                    },
                    'videoSink': {
                        'lastKnownStatus': {
                            'width': 1290,
                            'height': 2796,
                        },
                        'capabilities': {
                            'colorGamuts': [
                                'standard',
                                'wide',
                            ],
                            'hdrFormats': [
                                'dolbyvision',
                                'hdr10plus',
                                'hdr10',
                                'hlg',
                            ],
                        },
                    },
                },
                },
                'gdpr': False,
                'firstPlay': False,
                'playbackSessionId': str(uuid.uuid4()),
                'applicationSessionId': str(uuid.uuid4()),
                'userPreferences': {},
                'features': [],
            }
        )
        response.raise_for_status()

        playback_data = response.json()
        
        # Get video info for language
        video_info = next(x for x in playback_data['videos'] if x['type'] == 'main')
        title.language = Language.get(video_info['defaultAudioSelection']['language'])

        fallback_url = playback_data["fallback"]["manifest"]["url"]
        fallback_url = fallback_url.replace('fly', 'akm').replace('gcp', 'akm')

        try:
            self.wv_license_url = playback_data["drm"]["schemes"]["widevine"]["licenseUrl"]
        except (KeyError, IndexError):
            self.wv_license_url = None
            
        try:
            self.pr_license_url = playback_data["drm"]["schemes"]["playready"]["licenseUrl"]
        except (KeyError, IndexError):
            self.pr_license_url = None

        manifest_url = fallback_url.replace('_fallback', '')
        self.log.debug(f"MPD URL: {manifest_url}")
        self.log.debug(f"Fallback URL: {fallback_url}")
        self.log.debug(f"Widevine License URL: {self.wv_license_url}")
        self.log.debug(f"PlayReady License URL: {self.pr_license_url}")

        tracks = DASH.from_url(url=manifest_url, session=self.session).to_tracks(language=title.language)
        
        self.log.debug(tracks)

        tracks.videos = self._dedupe(tracks.videos)
        tracks.audio = self._dedupe(tracks.audio)
        
        # Remove partial subs and get VTT subtitles
        tracks.subtitles.clear()

        subtitles = self._get_subtitles(manifest_url, fallback_url)
        
        for subtitle in subtitles:
            tracks.add(
                Subtitle(
                    id_=md5(subtitle["url"].encode()).hexdigest()[0:6],
                    url=subtitle["url"],
                    codec=Subtitle.Codec.from_mime(subtitle['format']),
                    language=Language.get(subtitle["language"]),
                    forced=subtitle['name'] == 'Forced',
                    sdh=subtitle['name'] == 'SDH'
                )
            )

        # Apply codec filters
        if self.vcodec:
            tracks.videos = [x for x in tracks.videos if (x.codec or "")[:4] in self.VIDEO_CODEC_MAP[self.vcodec]]

        if self.acodec:
            tracks.audio = [x for x in tracks.audio if (x.codec or "")[:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        # Set track properties
        for track in tracks:
            if isinstance(track, Video):
                codec = track.data.get("dash", {}).get("representation", {}).get("codecs", "")
                track.hdr10 = track.range == Video.Range.HDR10
                track.dv = codec[:4] in ("dvh1", "dvhe")
            if isinstance(track, Subtitle) and not track.codec:
                track.codec = Subtitle.Codec.WebVTT

        # Store video info for chapters
        title.data['info'] = video_info
        
        # Mark descriptive audio tracks
        for track in tracks.audio:
            if hasattr(track, 'data') and track.data.get("dash", {}).get("adaptation_set"):
                role = track.data["dash"]["adaptation_set"].find("Role")
                if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                    track.descriptive = True

        self.log.debug(tracks)

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        chapters = []
        video_info = title.data.get('info', {})
        if 'annotations' in video_info:
            chapters.append(Chapter(timestamp=0.0, name='Chapter 1'))
            chapters.append(Chapter(timestamp=self._convert_timecode(video_info['annotations'][0]['start']), name='Credits'))
            chapters.append(Chapter(timestamp=self._convert_timecode(video_info['annotations'][0]['end']), name='Chapter 2'))

        return Chapters(chapters)

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        if not self.wv_license_url:
            return None
            
        response = self.session.post(
            url=self.wv_license_url,
            data=challenge
        )
        response.raise_for_status()
        return response.content

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        if not self.pr_license_url:
            return None
            
        # Handle both bytes and string challenge formats
        if isinstance(challenge, bytes):
            decoded_challenge = challenge.decode('utf-8')
        else:
            decoded_challenge = str(challenge)
            
        response = self.session.post(
            url=self.pr_license_url,
            data=decoded_challenge,
            headers={
                'Content-Type': 'text/xml; charset=utf-8',
                'SOAPAction': 'http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense'
            }
        )
    
        response.raise_for_status()
        return response.content

    def _get_device_token(self):
        response = self.session.post(self.config['endpoints']['bootstrap'])
        response.raise_for_status()
        return response.headers.get('x-wbd-session-state')

    @staticmethod
    def _convert_timecode(time_seconds):
        """Convert seconds to timestamp."""
        return float(time_seconds)

    def _get_subtitles(self, mpd_url, fallback_url):
        base_url = "/".join(fallback_url.split("/")[:-1]) + "/"
        xml = xmltodict.parse(requests.get(mpd_url).text)

        try:
            tracks = xml["MPD"]["Period"][0]["AdaptationSet"]
        except KeyError:
            tracks = xml["MPD"]["Period"]["AdaptationSet"]

        subs_tracks_js = []
        for subs_tracks in tracks:
            if subs_tracks.get('@contentType') == 'text':
                for x in self._force_instance(subs_tracks, "Representation"):
                    try:
                        path = re.search(r'(t/\w+/)', x["SegmentTemplate"]["@media"])[1]
                    except (AttributeError, KeyError):
                        path = 't/sub/'

                    is_sdh = False
                    is_forced = False
                    
                    role_value = subs_tracks.get("Role", {}).get("@value", "")
                    
                    if role_value == "caption":
                        url = base_url + path + subs_tracks['@lang'] + ('_sdh.vtt' if 'sdh' in subs_tracks.get("Label", "").lower() else '_cc.vtt')
                        is_sdh = True
                    elif role_value == "forced-subtitle":
                        url = base_url + path + subs_tracks['@lang'] + '_forced.vtt'
                        is_forced = True
                    elif role_value == "subtitle":
                        url = base_url + path + subs_tracks['@lang'] + '_sub.vtt'
                    else:
                        continue

                    subs_tracks_js.append({
                        "url": url,
                        "format": "vtt",
                        "language": subs_tracks["@lang"],
                        "name": "SDH" if is_sdh else "Forced" if is_forced else "Full",
                    })

        return self._remove_dupe(subs_tracks_js)

    @staticmethod
    def _force_instance(data, variable):
        if isinstance(data[variable], list):
            return data[variable]
        else:
            return [data[variable]]

    @staticmethod
    def _remove_dupe(items):
        seen = set()
        new_items = []
        for item in items:
            url = item['url']
            if url not in seen:
                new_items.append(item)
                seen.add(url)
        return new_items
        
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
