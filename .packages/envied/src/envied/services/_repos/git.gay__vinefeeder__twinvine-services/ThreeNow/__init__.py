from __future__ import annotations

import json
import math
import re
from collections.abc import Generator
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union
from urllib.parse import urljoin

import click
from click import Context
from lxml import etree
from pywidevine.cdm import Cdm as WidevineCdm
from requests import Request

from envied.core.cdm.detect import is_playready_cdm
from envied.core.console import console
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.manifests.ism import ISM
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T
from envied.core.tracks import Audio, Chapter, Chapters, Tracks, Video


class ThreeNow(Service):
    """
    envied.support for the ThreeNow streaming service (https://www.threenow.co.nz).
    Supports TV Series / Episodes. Movies and sports are yet to be supported.
    Note - ThreeNow is still stuck in the past with 720p content.

    Version: 1.0.0
    Author: jungyein
    Authorization: None
    CDM Support:
      WV L3: 720p, AAC2.0
      PR SL2000: 720p, AAC2.0

    Tips:
        - ISM ONLY works with n_m3u8dl_re and not aria2c or requests
    """

    GEOFENCE = ("nz", "au")
    TITLE_RE = (
        r"^(?:(?:https?://)?(?:www\.)?threenow\.co\.nz/)?"
        r"(?:shows/(?:[^/?#]+/)*?)?"
        r"(?P<show_id>[Ss]?\d+(?:-\d+)*)"
        r"(?:/(?P<episode_id>[Mm]?\d+(?:-\d+)*))?/?(?:[?#].*)?$"
    )

    @staticmethod
    @click.command(name="ThreeNow", short_help="https://www.threenow.co.nz", help=__doc__)
    @click.argument("title", type=str)
    @click.option(
        "-p",
        "--playlist",
        type=click.Choice(["dash", "ism"], case_sensitive=False),
        default="dash",
        help="playlist type, dash or ism",
    )
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> ThreeNow:
        return ThreeNow(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str, playlist: str):
        self.title = title
        self.playlist = playlist.lower()

        if self.playlist == "ism" and not is_playready_cdm(getattr(ctx.obj, "cdm", None)):
            raise click.UsageError(
                "ThreeNow ISM streams require a PlayReady CDM. Configure/select a PlayReady CDM or use -p dash.",
                ctx,
            )

        super().__init__(ctx)
        self.license = {}

        self.session.headers.update(self.config["headers"])

    def search(self) -> Generator[SearchResult, None, None]:
        self.log.error("ThreeNow search is not implemented yet")
        yield from ()

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        self.session.verify = False

    def get_titles(self) -> Union[Movies, Series]:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(f"Could not parse ID from title: {self.title}")

        self.show_id = match.group("show_id")
        self.episode_id = match.group("episode_id")

        page = self._request("GET", "shows/{}".format(self.show_id))
        # with open(f"debug/ThreeNow_{self.show_id.replace('/', '_')}.json", "w") as f:
        #     f.write(json.dumps(page, indent=4))

        seasons = page.get("seasons") or []
        if not seasons:
            raise ValueError(f"Could not find show from url: {self.title}")

        title = page.get("name", "")
        episodes = []

        for season in seasons:
            for episode in season.get("episodes", []):
                if self.episode_id and episode.get("videoId") != self.episode_id:
                    continue

                episodeData = episode.copy()
                episodeData["seasonNumber"] = season.get("seasonNumber", "1")
                episodeData["episodeNumber"] = episode.get("episode", "1")
                episodes.extend(self._episode(episodeData, title))
                self.log.info(f"Parsed episode: {title} - {episode.get('name')} (S{episodeData['seasonNumber']:02}E{episodeData['episodeNumber']:02})")

        if not episodes:
            raise ValueError(f"Could not find episode from url: {self.title}")

        return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        brightcodeEpisodeId = title.data.get("externalMediaId")
        if not brightcodeEpisodeId:
            raise ValueError("Unable to find Brightcove video ID for this episode")

        account_id = self.config["endpoints"]["brightcove_account"]
        self.drm_token = None
        data = self._request(
            "GET", self.config["endpoints"]["brightcove"].format(account_id, brightcodeEpisodeId),
            headers={"BCOV-POLICY": self.config["policy"]},
        )

        sources = data["sources"]
        dash_source = next((
            source for source in sources
            if source.get("type") == "application/dash+xml"
            and (source.get("key_systems") or {}).get("com.widevine.alpha")),
            None,
        ) or next((
            source for source in sources
            if (source.get("key_systems") or {}).get("com.widevine.alpha")),
            None,
        )
        if not dash_source:
            raise ValueError("Could not find a DASH source")

        dash_playready_source = next((
            source for source in sources
            if source.get("type") == "application/dash+xml"
            and (source.get("key_systems") or {}).get("com.microsoft.playready")),
            dash_source,
        )
        dash_key_systems = dash_source.get("key_systems") or {}
        dash_playready_key_systems = dash_playready_source.get("key_systems") or {}
        self.license[Video.Descriptor.DASH] = {
            "widevine": dash_key_systems.get("com.widevine.alpha", {}).get("license_url"),
            "playready": dash_playready_key_systems.get("com.microsoft.playready", {}).get("license_url"),
        }
        source_manifest = dash_source.get("src")

        # manifest = self.trim_duration(source_manifest)
        tmpDashTracks = DASH.from_url(source_manifest, self.session).to_tracks(title.language)

        dashTracks = Tracks()

        highestDashAudioBitrate = 0
        for tmpTrack in tmpDashTracks:
            # some rare cases have a 1080p video which cannot be decrypted
            # if isinstance(tmpTrack, Video):
            #    if tmpTrack.height == 1080:
            #        continue
            if isinstance(tmpTrack, Audio):
                highestDashAudioBitrate = max(highestDashAudioBitrate, tmpTrack.bitrate or 0)
            dashTracks.add(tmpTrack)

        if self.playlist == "ism":
            ism_source = next((
                source for source in sources
                if source.get("type") == "application/vnd.ms-sstr+xml"),
                None,
            )
            if not ism_source:
                raise ValueError("Could not find an ISM source")

            ism_key_systems = ism_source.get("key_systems") or {}
            self.license[Video.Descriptor.ISM] = {
                "playready": ism_key_systems.get("com.microsoft.playready", {}).get("license_url"),
            }
            source_manifest = ism_source.get("src")

            # manifest = self.trim_duration(source_manifest, "ism")
            ismTracks = ISM.from_url(source_manifest, self.session).to_tracks(title.language)
            tracks = dashTracks

            for tmpTrack in ismTracks:
                if isinstance(tmpTrack, Audio):
                    # Filter out ISM audio tracks with bitrate <= highest DASH audio track
                    # to avoid duplicate audio tracks. To keep all ISM audio tracks,
                    # comment out this if block.
                    if (tmpTrack.bitrate or 0) <= highestDashAudioBitrate:
                        continue
                tracks.add(tmpTrack)
        else:
            tracks = dashTracks

        # sort the videos by quality
        tracks.sort_videos()
        tracks.sort_audio()

        # return the list of tracks
        console.log(tracks)
        console.log()

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        chapters = {}

        for cue_point in title.data.get("cuePoints") or []:
            timestamp = cue_point.get("time") if isinstance(cue_point, dict) else cue_point
            if timestamp is not None:
                chapters[float(timestamp)] = None

        credits_timestamp = title.data.get("creditsCuePoint")
        if credits_timestamp is not None:
            chapters[float(credits_timestamp)] = "Credits"

        return Chapters(Chapter(timestamp=timestamp, name=name) for timestamp, name in sorted(chapters.items()))

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return WidevineCdm.common_privacy_cert

    def _get_license_url(self, descriptor: Video.Descriptor, drm: str) -> str:
        try:
            selectedLicense = self.license[descriptor][drm]
        except KeyError as e:
            raise ValueError(f"No {drm} license endpoint for {descriptor.name}") from e

        if not selectedLicense:
            raise ValueError(f"No {drm} license endpoint for {descriptor.name}")

        return selectedLicense

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        selectedLicense = self._get_license_url(track.descriptor, "playready")

        headers = {"Authorization": f"Bearer {self.drm_token}"} if self.drm_token else self.session.headers

        r = self.session.post(selectedLicense, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content

    def get_widevine_license(self, challenge: bytes, title: Title_T, track: AnyTrack, **_: Any) -> str:
        selectedLicense = self._get_license_url(track.descriptor, "widevine")

        headers = {"Authorization": f"Bearer {self.drm_token}"} if self.drm_token else self.session.headers
        r = self.session.post(selectedLicense, headers=headers, data=challenge)
        r.raise_for_status()

        return r.content

    # Service specific

    def _show(self, episodes: list, title: str) -> Episode:
        return [
            Episode(
                id_=episode.get("videoId"),
                service=self.__class__,
                title=title,
                season=int(episode.get("seasonNumber")) if episode.get("seasonNumber") else 0,
                number=int(episode.get("episodeNumber")) if episode.get("episodeNumber") else 0,
                name=episode.get("title"),
                language="en",
                data=episode,
                description=episode.get("synopsis") if episode.get("synopsis") else None,
            )
            for episode in episodes
        ]

    def _movie(self, movies: list, title: str) -> Movie:
        return [
            Movie(
                id_=movie.get("videoId"),
                service=self.__class__,
                name=title,
                year=None,
                language="en",
                data=movie,
            )
            for movie in movies
        ]

    def _episode(self, video: dict, title: str) -> list[Episode]:
        kind = video.get("type")
        name = video.get("title") or video.get("name")

        if kind == "sportVideo" and video.get("_embedded"):
            _type = next((x for x in video["_embedded"].values() if x.get("type") == "competition"), None)
            title = _type.get("title") if _type else title
            name = video.get("title", "") + " " + video.get("phase", "")

        return [
            Episode(
                id_=video.get("videoId"),
                service=self.__class__,
                title=title,
                season=int(video.get("seasonNumber")) if video.get("seasonNumber") else 0,
                number=int(video.get("episodeNumber")) if video.get("episodeNumber") else 0,
                name=name if name[:6] != "Season" else None,
                language="en",
                data=video,
                description=video.get("synopsis") if video.get("synopsis") else None,
            )
        ]

    def _request(
        self,
        method: str,
        api: str,
        params: dict = None,
        headers: dict = None,
        payload: dict = None,
    ) -> Any[dict | str]:
        url = urljoin(self.config["endpoints"]["base_api"], api)
        if headers:
            self.session.headers.update(headers)

        prep = self.session.prepare_request(Request(method, url, params=params, json=payload))
        response = self.session.send(prep)

        try:
            # with open(f"debug/ThreeNow_{api.replace('/', '_')}.json", "w") as f:
            #     f.write(response.text)
            data = json.loads(response.content)

            if data.get("message"):
                raise ConnectionError(f"{response.status_code} - {data.get('message')}")

            return data

        except Exception:
            raise ConnectionError("Request failed: {} - {}".format(response.status_code, response.text))

    def trim_duration(self, source_manifest: str, playlist: str = "dash") -> str:
        """
        The last segment on all tracks may return a 404 for some reason, causing a failed download.
        Trim the manifest by exactly one segment to account for that.
        """

        if playlist == "dash":
            manifest = DASH.from_url(source_manifest, self.session).manifest
            trimmed = self._trim_dash_manifest(manifest)
        else:
            manifest = ISM.from_url(source_manifest, self.session).manifest
            trimmed = self._trim_ism_manifest(manifest)

        if not trimmed:
            self.log.warning("Unable to trim the final %s manifest segment", playlist.upper())

        # with open("debug/ThreeNow_trimmed.mpd", "w") as f:
        #     f.write(etree.tostring(manifest, encoding="unicode"))

        return etree.tostring(manifest, encoding="unicode")

    def _trim_dash_manifest(self, manifest: Any) -> bool:
        trimmed = False

        for segment_timeline in manifest.findall(".//SegmentTimeline"):
            if self._trim_dash_segment_timeline(segment_timeline):
                segment_template = segment_timeline.getparent()
                if segment_template is not None:
                    self._trim_dash_end_number(segment_template)
                trimmed = True

        for segment_template in manifest.findall(".//SegmentTemplate"):
            if segment_template.find("SegmentTimeline") is not None:
                continue
            if self._trim_dash_end_number(segment_template):
                trimmed = True
                continue

            period = self._find_parent(segment_template, "Period")
            period_duration = period.get("duration") if period is not None else None
            period_duration = period_duration or manifest.get("mediaPresentationDuration")
            segment_duration = float(segment_template.get("duration") or 0)
            timescale = float(segment_template.get("timescale") or 1)
            if not period_duration or not segment_duration:
                continue

            segment_count = math.ceil(DASH.pt_to_sec(period_duration) / (segment_duration / timescale))
            if segment_count > 1:
                start_number = int(segment_template.get("startNumber") or 1)
                segment_template.set("endNumber", str(start_number + segment_count - 2))
                trimmed = True

        for segment_list in manifest.findall(".//SegmentList"):
            segment_urls = segment_list.findall("SegmentURL")
            if len(segment_urls) > 1:
                segment_list.remove(segment_urls[-1])
                trimmed = True

        return trimmed

    @staticmethod
    def _trim_dash_segment_timeline(segment_timeline: Any) -> bool:
        segments = segment_timeline.findall("S")
        if not segments:
            return False

        last_segment = segments[-1]
        repeat = int(last_segment.get("r") or 0)
        if repeat > 0:
            last_segment.set("r", str(repeat - 1))
            return True

        if len(segments) == 1:
            return False

        segment_timeline.remove(last_segment)
        return True

    @staticmethod
    def _trim_dash_end_number(segment_template: Any) -> bool:
        end_number = segment_template.get("endNumber")
        if not end_number:
            return False

        start_number = int(segment_template.get("startNumber") or 1)
        end_number = int(end_number)
        if end_number <= start_number:
            return False

        segment_template.set("endNumber", str(end_number - 1))
        return True

    def _trim_ism_manifest(self, manifest: Any) -> bool:
        trimmed = False
        end_times = []
        manifest_duration = int(manifest.get("Duration") or 0)

        for stream_index in manifest.findall("StreamIndex"):
            segments = self._get_ism_segments(stream_index, manifest_duration)
            if len(segments) <= 1:
                continue

            trimmed_segments = segments[:-1]
            self._replace_ism_segments(stream_index, trimmed_segments)
            end_times.append(trimmed_segments[-1][0] + trimmed_segments[-1][1])
            trimmed = True

        if trimmed and end_times:
            manifest.set("Duration", str(max(end_times)))

        return trimmed

    @staticmethod
    def _get_ism_segments(stream_index: Any, manifest_duration: int) -> list[tuple[int, int]]:
        fragments = stream_index.findall("c")
        fragment_time = 0
        segments = []

        for idx, fragment in enumerate(fragments):
            fragment_time = int(fragment.get("t", fragment_time))
            repeat = int(fragment.get("r") or 1)
            duration = int(fragment.get("d") or 0)
            if not duration:
                try:
                    next_time = int(fragments[idx + 1].get("t"))
                except (IndexError, TypeError):
                    next_time = manifest_duration
                duration = int((next_time - fragment_time) / repeat) if repeat else 0

            for _ in range(repeat):
                segments.append((fragment_time, duration))
                fragment_time += duration

        return segments

    @staticmethod
    def _replace_ism_segments(stream_index: Any, segments: list[tuple[int, int]]) -> None:
        for fragment in stream_index.findall("c"):
            stream_index.remove(fragment)

        for start_time, duration in segments:
            fragment = etree.SubElement(stream_index, "c")
            fragment.set("t", str(start_time))
            fragment.set("d", str(duration))

    @staticmethod
    def _find_parent(element: Any, tag: str) -> Optional[Any]:
        parent = element.getparent()
        while parent is not None:
            if parent.tag == tag:
                return parent
            parent = parent.getparent()
        return None
