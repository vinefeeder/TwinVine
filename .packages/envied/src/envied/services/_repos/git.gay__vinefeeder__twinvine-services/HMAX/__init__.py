import hashlib
import json
import re
import uuid
from datetime import datetime
from hashlib import md5
from typing import Optional, Union, Generator
from http.cookiejar import CookieJar
from collections import defaultdict
from copy import deepcopy
from zlib import crc32

import click
import requests
import xmltodict
from lxml import etree
from langcodes import Language

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Attachment, Chapter, Chapters, Subtitle, Tracks, Video
from envied.core.utilities import is_close_match


class HMAX(Service):
    ALIASES = ("HMAX", "max", "hbomax")
    #GEOFENCE = ("US",)

    TITLE_RE = r"^(?:https?://(?:www\.|play\.)?hbomax\.com/)?(?P<type>[^/]+)/(?P<id>[^/]+)"

    VIDEO_CODEC_MAP = {
        "H264": [Video.Codec.AVC],
        "H265": [Video.Codec.HEVC]
    }

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="HMAX", short_help="https://max.com")
    @click.argument("title", type=str)
    @click.option("-vcodec", "--video-codec", default=None, help="Video codec preference")
    @click.option("-acodec", "--audio-codec", default=None, help="Audio codec preference")
    @click.pass_context
    def cli(ctx, **kwargs):
        return HMAX(ctx, **kwargs)

    def __init__(self, ctx, title, video_codec, audio_codec):
        super().__init__(ctx)
        
        self.title = title
        self.vcodec = video_codec
        self.acodec = audio_codec
        
        range_param = ctx.parent.params.get("range_")
        self.range = range_param[0].name if range_param else "SDR"
        
        if self.range == 'HDR10':
            self.vcodec = "H265"

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")
        
        try:
            token = next(cookie.value for cookie in cookies if cookie.name == "st")
            session_data = next(cookie.value for cookie in cookies if cookie.name == "session")
            device_id = json.loads(session_data)
        except (StopIteration, json.JSONDecodeError):
            raise EnvironmentError("Required authentication cookies not found.")
        
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
        
        auth_token = self._get_device_token()
        self.session.headers.update({
            "x-wbd-session-state": auth_token
        })

    def search(self) -> Generator[SearchResult, None, None]:
        search_url = "https://default.prd.api.hbomax.com/search"
        
        try:
            response = self.session.get(search_url, params={"q": self.title})
            response.raise_for_status()
            
            search_data = response.json()
            
            for result in search_data.get("results", []):
                yield SearchResult(
                    id_=result.get("id"),
                    title=result.get("title", "Unknown"),
                    label=result.get("type", "UNKNOWN").upper(),
                    url=f"https://play.hbomax.com/{result.get('type', 'content')}/{result.get('id')}"
                )
                
        except Exception as e:
            self.log.warning(f"Search functionality not fully implemented: {e}")
            return
            yield

    def get_titles(self) -> Titles_T:
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

            image_map = {}  # accumulate images across all seasons
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
                # Accumulate image objects across all seasons
                image_map.update({
                    obj["id"]: obj
                    for obj in data["included"]
                    if obj.get("type") == "image"
                })

            episode_titles = []
            release_date = episodes[0]["attributes"].get("airDate") or episodes[0]["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year
            
            season_map = {int(item[1].split("=")[-1]): item[0] for item in season_parameters}

            for episode in episodes:
                # Resolve image objects for this episode using relationship IDs
                ep_image_ids = [
                    img["id"]
                    for img in episode.get("relationships", {}).get("images", {}).get("data", [])
                ]
                ep_images = [image_map[img_id] for img_id in ep_image_ids if img_id in image_map]
                ep_data = dict(episode)
                ep_data["_images"] = ep_images
                episode_titles.append(
                    Episode(
                        id_=episode['id'],
                        service=self.__class__,
                        title=content_title,
                        season=season_map.get(episode['attributes'].get('seasonNumber')),
                        number=episode['attributes']['episodeNumber'],
                        name=episode['attributes']['name'],
                        year=year,
                        data=ep_data
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
        self.log.info(f" + Manifest: {manifest_url}")

        dash_manifest = DASH.from_url(url=manifest_url, session=self.session)
        tracks = dash_manifest.to_tracks(language=title.language)

        
        self.log.debug(tracks)

        tracks.videos = self._dedupe(tracks.videos)
        tracks.audio = self._dedupe(tracks.audio)
        
        tracks.subtitles.clear()

        new_subtitles = self._process_max_subtitles(dash_manifest, title.language)
        
        for subtitle in new_subtitles:
            tracks.add(subtitle)

        if self.vcodec:
            tracks.videos = [x for x in tracks.videos if x.codec in self.VIDEO_CODEC_MAP[self.vcodec]]

        if self.acodec:
            tracks.audio = [x for x in tracks.audio if (x.codec or "")[:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        for track in tracks:
            if isinstance(track, Video):
                codec = track.data.get("dash", {}).get("representation", {}).get("codecs", "")
                track.hdr10 = track.range == Video.Range.HDR10
                track.dv = codec[:4] in ("dvh1", "dvhe")
            if isinstance(track, Subtitle) and not track.codec:
                track.codec = Subtitle.Codec.WebVTT

        title.data['info'] = video_info
        
        for track in tracks.audio:
            if hasattr(track, 'data') and track.data.get("dash", {}).get("adaptation_set"):
                role = track.data["dash"]["adaptation_set"].find("Role")
                if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                    track.descriptive = True

        # Attachment: episode/movie image resolved from relationships._images
        # _images is built in get_titles by cross-referencing the included image objects
        # HBO Max image attributes use "src" (not "url") and "kind" for type
        try:
            images = title.data.get("_images", [])
            image_url = None
            # Prefer landscape (default/wide) images; fallback to first available
            PREFERRED_KINDS = ("default", "tile", "episode", "cover", "banner",
                               "centered-background-small", "background")
            kind_map = {}
            for img in images:
                attrs = img.get("attributes", {})
                kind = attrs.get("kind", "").lower()
                src = attrs.get("src", "") or attrs.get("url", "")
                if src and kind not in kind_map:
                    kind_map[kind] = src
            for k in PREFERRED_KINDS:
                if k in kind_map:
                    image_url = kind_map[k]
                    break
            # Fallback: take first available src
            if not image_url and images:
                for img in images:
                    attrs = img.get("attributes", {})
                    src = attrs.get("src", "") or attrs.get("url", "")
                    if src:
                        image_url = src
                        break
            if image_url:
                if isinstance(title, Movie):
                    image_name = title.name
                else:
                    image_name = f"{title.title} - S{title.season:02d}E{title.number:02d}"
                tracks.add(Attachment.from_url(
                    url=image_url,
                    name=image_name,
                    mime_type="image/jpeg",
                    session=self.session,
                ))
        except Exception as e:
            self.log.warning(f" - Attachment failed: {e}")

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
        return float(time_seconds)

    def _process_max_subtitles(self, dash_manifest, language):
        subtitle_groups = defaultdict(list)

        for period in dash_manifest.manifest.findall("Period"):
            for adaptation_set in period.findall("AdaptationSet"):
                content_type = adaptation_set.get("contentType")
                if content_type != "text":
                    continue

                lang = adaptation_set.get("lang")
                if not lang:
                    continue

                role = adaptation_set.find("Role")
                role_value = role.get("value") if role is not None else "subtitle"
                label = adaptation_set.find("Label")
                label_text = label.text if label is not None else ""

                key = (lang, role_value, label_text)
                subtitle_groups[key].append((period, adaptation_set))

        final_tracks = []

        for (lang, role_value, label_text), adapt_pairs in subtitle_groups.items():
            if not adapt_pairs:
                continue

            first_period, first_adapt = adapt_pairs[0]
            first_rep = first_adapt.find("Representation")
            if not first_rep:
                continue

            combined_adapt = deepcopy(first_adapt)
            combined_rep = combined_adapt.find("Representation")

            seg_template = combined_rep.find("SegmentTemplate")
            if seg_template is None:
                seg_template = combined_adapt.find("SegmentTemplate")
                if seg_template is None:
                    continue
                combined_adapt.remove(seg_template)
                seg_template = deepcopy(seg_template)
                combined_rep.append(seg_template)

            timeline = etree.Element("SegmentTimeline")

            segments_info = []
            for period, adapt in adapt_pairs:
                rep = adapt.find("Representation")
                if not rep:
                    continue

                template = rep.find("SegmentTemplate")
                if not template:
                    template = adapt.find("SegmentTemplate")
                if not template:
                    continue

                start_num = int(template.get("startNumber", 1))

                existing_timeline = template.find("SegmentTimeline")
                if existing_timeline is not None:
                    for s in existing_timeline.findall("S"):
                        t = int(s.get("t", 0))
                        d = int(s.get("d", 0))
                        r = int(s.get("r", 0))
                        segments_info.append((start_num, t, d, r))

            segments_info.sort(key=lambda x: x[0])

            if segments_info:
                for _, t, d, r in segments_info:
                    s_elem = etree.Element("S")
                    s_elem.set("t", str(t))
                    s_elem.set("d", str(d))
                    if r > 0:
                        s_elem.set("r", str(r))
                    timeline.append(s_elem)

                old_timeline = seg_template.find("SegmentTimeline")
                if old_timeline is not None:
                    seg_template.remove(old_timeline)
                seg_template.append(timeline)

                seg_template.set("startNumber", "1")
                seg_template.set("endNumber", str(len(segments_info)))

            track_id = hex(crc32(f"sub-{lang}-{role_value}-{label_text}".encode()))[2:]

            is_sdh = role_value == "caption" or "sdh" in label_text.lower()
            is_forced = role_value in ["forced-subtitle", "forced_subtitle"] or "forced" in label_text.lower()

            lang_obj = Language.get(lang)
            display_name = lang_obj.display_name()

            subtitle_track = Subtitle(
                id_=track_id,
                url=dash_manifest.url,
                codec=Subtitle.Codec.WebVTT,
                language=Language.get(lang),
                is_original_lang=bool(language and is_close_match(Language.get(lang), [language])),
                descriptor=Video.Descriptor.DASH,
                sdh=is_sdh,
                forced=is_forced,
                name=display_name,
                data={
                    "dash": {
                        "manifest": dash_manifest.manifest,
                        "period": first_period,
                        "adaptation_set": combined_adapt,
                        "representation": combined_rep,
                    }
                },
            )
            final_tracks.append(subtitle_track)

        return final_tracks
        
    @staticmethod
    def _dedupe(items: list) -> list:
        if not items:
            return items
        if isinstance(items[0].url, list):
            return items
        
        seen = {}
        for item in items:
            if hasattr(item, 'width') and hasattr(item, 'height'):
                key = f"{item.codec}_{item.width}x{item.height}_{item.bitrate}"
            elif hasattr(item, 'channels'):
                key = f"{item.codec}_{item.language}_{item.bitrate}_{item.channels}"
            else:
                key = item.url
            
            if key not in seen:
                seen[key] = item
        
        return list(seen.values())
