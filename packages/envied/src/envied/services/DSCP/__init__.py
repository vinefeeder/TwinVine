from __future__ import annotations

import json
import re
import sys
import uuid
from collections import defaultdict
from collections.abc import Generator
from copy import deepcopy
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urljoin
from zlib import crc32

import click
from click import Context
from langcodes import Language
from lxml import etree
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.session import session
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Audio, Chapter, Chapters, Subtitle, Track, Tracks
from envied.core.utilities import is_close_match


class DSCP(Service):
    """
    \b
    Service code for Discovery Plus streaming service (https://www.discoveryplus.com).
    Credit to @sp4rk.y for the subtitle fix.

    \b
    Version: 1.0.4
    Author: stabbedbybrick
    Authorization: Cookies for subscription, none for freely available titles
    Robustness:
        Widevine:
            L1: 2160p, 1080p
            L3: 720p
        PlayReady:
            SL3000: 2160p
            SL2000: 1080p, 720p

    \b
    Tips:
        - Input can be either complete title URL or just the path:
            SHOW: /show/eb26e00e-9582-4790-a61c-48d785926f58
            STANDALONE: /standalone/5012ae3f-d9bd-46ec-ad42-b8116b811441
            SPORT: /sport/9cc449de-2a64-524d-bcb6-cabd4ac70340
            EPISODE: /video/watch/8685efdd-a3c4-4892-b1d1-5f9f071cacf1/de67ea8e-a90f-4609-81af-4f09906f60b2

    \b
    Notes:
        - Language tags can be mislabelled or missing on some titles. List tracks with --list to verify.
        - All qualities, codecs, and ranges are included when available. Use -v H.265, -r HDR10, -q 1080p, etc. to select.

    \b
    Bonus tip: With some minor adjustments to the code and config, you can convert this to an HMAX service.
        - Replace all instances of "DSCP" with "HMAX"
        - Replace all instances of "dplus" with "beam"
        - Replace all instances of "discoveryplus" with "hbomax"

    """
    
    ALIASES = ("discoveryplus",)
    TITLE_RE = (
        r"^(?:https?://play.discoveryplus\.com?)?/(?P<type>show|mini-series|video|movie|topical|standalone|sport)/(?P<id>[a-z0-9-/]+)"
    )

    @staticmethod
    @click.command(name="DSCP", short_help="https://www.discoveryplus.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> DSCP:
        return DSCP(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        super().__init__(ctx)
        self.title = title

        self.profile = ctx.parent.params.get("profile")
        if not self.profile:
            self.profile = "default"

        self.cdm = ctx.obj.cdm
        if self.cdm is not None:
            self.drm_system = "playready"
            self.security_level = "SL3000"

            if self.cdm.security_level <= 3:
                self.drm_system = "widevine"
                self.security_level = "L1"

        self.base_url = self.config["endpoints"]["default_url"]

    def get_session(self) -> session:
        return session("OkHttp4_12", status_forcelist=[429, 502, 503, 504])

    def authenticate(self, cookies: CookieJar | None = None, credential: Credential | None = None) -> None:
        super().authenticate(cookies, credential)
        tokens = {}

        if cookies is not None:
            st_token = next((c.value for c in cookies if c.name == "st"), None)
            if not st_token:
                raise ValueError("- Unable to find token in cookies, try refreshing.")

            # Only use cache if cookies are present since it's not needed for free titles
            cache = self.cache.get(f"tokens_{self.profile}")
            if cache:
                self.log.info(" + Using cached Tokens...")
                tokens = cache.data
            else:
                self.log.info(" + Setting up new profile...")
                profile = {"token": st_token, "device_id": str(uuid.uuid1())}
                cache.set(profile)
                tokens = cache.data

        self.device_id = tokens.get("device_id") or str(uuid.uuid1())
        client_id = self.config["client_id"]

        self.session.headers.update({
            "user-agent": "androidtv dplus/20.8.1.2 (android/9; en-US; SHIELD Android TV-NVIDIA; Build/1)",
            "x-disco-client": "ANDROIDTV:9:dplus:20.8.1.2",
            "x-disco-params": "realm=bolt,bid=dplus,features=ar",
            "x-device-info": f"dplus/20.8.1.2 (NVIDIA/SHIELD Android TV; android/9-mdarcy; {self.device_id}/{client_id})",
        })

        access = self._request("GET", "/token", params={"realm": "bolt", "deviceId": self.device_id})
        
        self.access_token = access["data"]["attributes"]["token"]

        config = self._request("POST", "/session-context/headwaiter/v1/bootstrap")
        self.base_url = self.config["endpoints"]["template"].format(config["routing"]["tenant"], config["routing"]["homeMarket"])

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "include": "default",
            "decorators": "viewingHistory,isFavorite,playbackAllowed,contentAction,badges",
            "contentFilter[query]": self.title,
            "page[items.number]": "1",
            "page[items.size]": "8",
        }
        data = self._request("GET", "/cms/routes/search/result", params=params)

        results = [x.get("attributes") for x in data["included"] if x.get("type") == "show"]

        for result in results:
            yield SearchResult(
                id_=f"/show/{result.get('alternateId')}",
                title=result.get("name"),
                description=result.get("description"),
                label="show",
                url=f"/show/{result.get('alternateId')}",
            )

    def get_titles(self) -> Movies | Series:
        try:
            entity, content_id = (re.match(self.TITLE_RE, self.title).group(i) for i in ("type", "id"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        if entity in ("show", "mini-series", "topical"):
            episodes = self._show(content_id)
            return Series(episodes)

        elif entity in ("movie", "standalone"):
            movie = self._movie(content_id, entity)
            return Movies(movie)
        
        elif entity == "sport":
            sport = self._sport(content_id)
            return Movies(sport)

        elif entity == "video":
            episodes = self._episode(content_id)
            return Series(episodes)

        else:
            raise ValueError(f"Unknown content: {entity}")


    def get_tracks(self, title: Movie | Episode) -> Tracks:
        payload = {
            "appBundle": "com.wbd.stream",
            "applicationSessionId": self.device_id,
            "capabilities": {
                "codecs": {
                    "audio": {
                        "decoders": [
                            {"codec": "aac", "profiles": ["lc", "he", "hev2", "xhe"]},
                            {"codec": "eac3", "profiles": ["atmos"]},
                        ]
                    },
                    "video": {
                        "decoders": [
                            {
                                "codec": "h264",
                                "levelConstraints": {
                                    "framerate": {"max": 60, "min": 0},
                                    "height": {"max": 2160, "min": 48},
                                    "width": {"max": 3840, "min": 48},
                                },
                                "maxLevel": "5.2",
                                "profiles": ["baseline", "main", "high"],
                            },
                            {
                                "codec": "h265",
                                "levelConstraints": {
                                    "framerate": {"max": 60, "min": 0},
                                    "height": {"max": 2160, "min": 144},
                                    "width": {"max": 3840, "min": 144},
                                },
                                "maxLevel": "5.1",
                                "profiles": ["main10", "main"],
                            },
                        ],
                        "hdrFormats": ["hdr10", "hdr10plus", "dolbyvision", "dolbyvision5", "dolbyvision8", "hlg"],
                    },
                },
                "contentProtection": {
                    "contentDecryptionModules": [
                        {"drmKeySystem": self.drm_system, "maxSecurityLevel": self.security_level}
                    ]
                },
                "manifests": {"formats": {"dash": {}}},
            },
            "consumptionType": "streaming",
            "deviceInfo": {
                "player": {
                    "mediaEngine": {"name": "", "version": ""},
                    "playerView": {"height": 2160, "width": 3840},
                    "sdk": {"name": "", "version": ""},
                }
            },
            "editId": title.id,
            "firstPlay": False,
            "gdpr": False,
            "playbackSessionId": str(uuid.uuid4()),
            "userPreferences": {
                #'uiLanguage': 'en'
            },
        }

        playback = self._request(
            "POST", "/playback-orchestrator/any/playback-orchestrator/v1/playbackInfo",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json=payload,
        )
        
        original_language = next((
            x.get("language")
            for x in playback["videos"][0]["audioTracks"]
            if "Original" in x.get("displayName", "")
        ), "")

        manifest = (
            playback.get("fallback", {}).get("manifest", {}).get("url", "").replace("_fallback", "")
            or playback.get("manifest", {}).get("url")
        )

        license_url = (
            playback.get("fallback", {}).get("drm", {}).get("schemes", {}).get(self.drm_system, {}).get("licenseUrl")
            or playback.get("drm", {}).get("schemes", {}).get(self.drm_system, {}).get("licenseUrl")
        )

        title.data["license_url"] = license_url
        title.data["chapters"] = next((x.get("annotations") for x in playback["videos"] if x["type"] == "main"), None)

        dash = DASH.from_url(url=manifest, session=self.session)
        tracks = dash.to_tracks(language="en", period_filter=self._period_filter)

        for track in tracks:
            track.is_original_lang = str(track.language) == original_language
            track.name = "Original" if track.is_original_lang else track.name

            if isinstance(track, Audio):
                role = track.data["dash"]["representation"].find("Role")
                if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                    track.descriptive = True

            if isinstance(track, Subtitle):
                tracks.subtitles.remove(track)

        subtitles = self._process_subtitles(dash, original_language)
        tracks.add(subtitles)

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        if not (chapters := title.data.get("chapters")):
            return Chapters()
        
        ch_map = {0: Chapter(timestamp=0)}

        for c in chapters:
            s_type, c_type = c.get("secondaryType", "").lower(), c.get("type", "").lower()

            name = None
            if "recap" in s_type:
                name = "Recap"
            elif "intro" in s_type:
                name = "Intro"
            elif "credits" in c_type:
                name = "Credits"

            if (end := c.get("end")) is not None:
                ch_map.setdefault(end, Chapter(timestamp=end))

            if (start := c.get("start")) is not None:
                if start not in ch_map or name:
                    ch_map[start] = Chapter(name=name, timestamp=start)

        sorted_chapters = sorted(ch_map.values(), key=lambda x: x.timestamp)
        return Chapters(sorted_chapters)

    def get_widevine_service_certificate(self, challenge: bytes, title: Episode | Movie, **_: Any) -> str:
        if not (license_url := title.data.get("license_url")):
            return None
        
        return self.session.post(url=license_url, data=challenge).content
        

    def get_widevine_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (license_url := title.data.get("license_url")):
            return None

        r = self.session.post(url=license_url, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.status_code, r.text)

        return r.content
    
    def get_playready_license(self, *, challenge: bytes, title: Episode | Movie, track: Any) -> bytes | str | None:
        if not (license_url := title.data.get("license_url")):
            return None

        r = self.session.post(url=license_url, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.status_code, r.text)

        return r.content

    # Service specific functions

    @staticmethod
    def _process_subtitles(dash: DASH, language: str) -> list[Subtitle]:
        subtitle_groups = defaultdict(list)
        manifest = dash.manifest

        for period in manifest.findall("Period"):
            for adapt_set in period.findall("AdaptationSet"):
                if adapt_set.get("contentType") != "text" or not adapt_set.get("lang"):
                    continue

                role = adapt_set.find("Role")
                label = adapt_set.find("Label")
                key = (
                    adapt_set.get("lang"),
                    role.get("value") if role is not None else "subtitle",
                    label.text if label is not None else "",
                )
                subtitle_groups[key].append((period, adapt_set))

        final_tracks = []
        for (lang, role_value, label_text), adapt_set_group in subtitle_groups.items():
            first_period, first_adapt = adapt_set_group[0]
            if first_adapt.find("Representation") is None:
                continue

            s_elements_with_context = []
            for _, adapt_set in adapt_set_group:
                rep = adapt_set.find("Representation")
                if rep is None:
                    continue

                template = rep.find("SegmentTemplate") or adapt_set.find("SegmentTemplate")
                timeline = template.find("SegmentTimeline") if template is not None else None

                if timeline is not None:
                    start_num = int(template.get("startNumber", 1))
                    s_elements_with_context.extend((start_num, s_elem) for s_elem in timeline.findall("S"))

            s_elements_with_context.sort(key=lambda x: x[0])

            combined_adapt = deepcopy(first_adapt)
            combined_rep = combined_adapt.find("Representation")

            seg_template = combined_rep.find("SegmentTemplate")
            if seg_template is None:
                template_at_adapt = combined_adapt.find("SegmentTemplate")
                if template_at_adapt is not None:
                    seg_template = deepcopy(template_at_adapt)
                    combined_rep.append(seg_template)
                    combined_adapt.remove(template_at_adapt)
                else:
                    continue

            if seg_template.find("SegmentTimeline") is not None:
                seg_template.remove(seg_template.find("SegmentTimeline"))

            new_timeline = etree.Element("SegmentTimeline")
            new_timeline.extend(deepcopy(s) for _, s in s_elements_with_context)
            seg_template.append(new_timeline)

            seg_template.set("startNumber", "1")
            if "endNumber" in seg_template.attrib:
                del seg_template.attrib["endNumber"]

            track_id = hex(crc32(f"sub-{lang}-{role_value}-{label_text}".encode()) & 0xFFFFFFFF)[2:]
            lang_obj = Language.get(lang)
            track_name = "Original" if (language and is_close_match(lang_obj, [language])) else lang_obj.display_name()

            final_tracks.append(
                Subtitle(
                    id_=track_id,
                    url=dash.url,
                    codec=Subtitle.Codec.WebVTT,
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

    @staticmethod
    def _period_filter(period: Any) -> bool:
        """Shouldn't be needed for fallback manifest"""
        if not (duration := period.get("duration")):
            return False
        
        return DASH.pt_to_sec(duration) < 120

    def _show(self, title: str) -> Episode:
        params = {
            "include": "default",
            "decorators": "viewingHistory,badges,isFavorite,contentAction",
        }
        data = self._request("GET", "/cms/routes/show/{}".format(title), params=params)

        info = next(x for x in data["included"] if x.get("attributes", {}).get("alternateId", "") == title)
        content = next((x for x in data["included"] if "show-page-rail-episodes-tabbed-content" in x["attributes"].get("alias", "")), None)
        if not content:
            raise ValueError("Show not found")
        
        content_id = content.get("id")
        show_id = content["attributes"]["component"].get("mandatoryParams", "")
        season_params = [x.get("parameter") for x in content["attributes"]["component"]["filters"][0]["options"]]
        page = next(x for x in data["included"] if x.get("type", "") == "page")

        seasons = [
            self._request(
                "GET", "/cms/collections/{}?{}&{}".format(content_id, season, show_id),
                params={"include": "default", "decorators": "viewingHistory,badges,isFavorite,contentAction"},
            )
            for season in season_params
        ]

        videos = [[x for x in season["included"] if x["type"] == "video"] for season in seasons]

        return [
            Episode(
                id_=ep["relationships"]["edit"]["data"]["id"],
                service=self.__class__,
                title=page["attributes"].get("title") or info["attributes"].get("originalName"),
                year=ep["attributes"]["airDate"][:4] if ep["attributes"].get("airDate") else None,
                season=ep["attributes"].get("seasonNumber"),
                number=ep["attributes"].get("episodeNumber"),
                name=ep["attributes"]["name"],
                data=ep,
            )
            for episodes in videos
            for ep in episodes
            if ep.get("attributes", {}).get("videoType", "") == "EPISODE"
        ]

    def _episode(self, title: str) -> Episode:
        video_id = title.split("/")[1]

        params = {"decorators": "isFavorite", "include": "show"}
        content = self._request("GET", "/content/videos/{}".format(video_id), params=params)

        episode = content.get("data", {}).get("attributes")
        video_type = episode.get("videoType")
        relationships = content.get("data", {}).get("relationships")
        show = next((x for x in content["included"] if x.get("type", "") == "show"), {})

        show_title = show.get("attributes", {}).get("name") or show.get("attributes", {}).get("originalName")
        episode_name = episode.get("originalName") or episode.get("secondaryTitle")
        if video_type.lower() in ("clip", "standalone_event"):
            show_title = episode.get("originalName")
            episode_name = episode.get("secondaryTitle", "")

        return [
            Episode(
                id_=relationships.get("edit", {}).get("data", {}).get("id"),
                service=self.__class__,
                title=show_title,
                year=int(episode.get("airDate")[:4]) if episode.get("airDate") else None,
                season=episode.get("seasonNumber") or 0,
                number=episode.get("episodeNumber") or 0,
                name=episode_name,
                data=episode,
            )
        ]
    
    def _sport(self, title: str) -> Movie:
        title_id = title
        if len(title_id.split("/")) > 1:
            title_id = title_id.split("/")[-1]

        params = {
            "include": "default",
            "decorators": "viewingHistory,badges,isFavorite,contentAction",
        }
        data = self._request("GET", "/cms/routes/sport/{}".format(title), params=params)

        content = next((x for x in data["included"] if x.get("attributes", {}).get("alternateId", "") == title_id), None)
        if not content:
            raise ValueError(f"Content not found for title: {title}")

        movie = content.get("attributes")
        relationships = content.get("relationships")

        name = movie.get("name") or movie.get("originalName")
        year = int(movie.get("firstAvailableDate")[:4]) if movie.get("firstAvailableDate") else None

        return [
            Movie(
                id_=relationships.get("edit", {}).get("data", {}).get("id"),
                service=self.__class__,
                name=name + " - " + movie.get("secondaryTitle", ""),
                year=year,
                data=movie,
            )
        ]

    def _movie(self, title: str, entity: str) -> Movie:
        params = {
            "include": "default",
            "decorators": "isFavorite,playbackAllowed,contentAction,badges",
        }
        data = self._request("GET", "/cms/routes/movie/{}".format(title), params=params)

        movie = next((
            x for x in data["included"]if x.get("attributes", {}).get("videoType", "").lower() == entity), None
        )
        if not movie:
            raise ValueError("Movie not found")

        return [
            Movie(
                id_=movie["relationships"]["edit"]["data"]["id"],
                service=self.__class__,
                name=movie["attributes"].get("name") or movie["attributes"].get("originalName"),
                year=int(movie["attributes"]["airDate"][:4]) if movie["attributes"].get("airDate") else None,
                data=movie,
            )
        ]

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any[dict | str]:
        url = urljoin(self.base_url, endpoint)

        response = self.session.request(method, url, **kwargs)

        try:
            data = json.loads(response.content)

            if errors := data.get("errors", []):
                code = next((x.get("code", "") for x in errors), "")
                if "missingpackage" in code.lower():
                    self.log.error("\nError: Subscription is required for this title.")
                    sys.exit(1)

            return data

        except Exception as e:
            raise ConnectionError(f"Request failed for {url}: {e}")
