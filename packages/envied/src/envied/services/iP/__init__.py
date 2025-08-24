from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import warnings
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Union

import click
from bs4 import XMLParsedAsHTMLWarning
from click import Context
from envied.core.manifests import DASH, HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Audio, Chapters, Subtitle, Tracks, Video
from envied.core.utils.collections import as_list
from envied.core.utils.sslciphers import SSLCiphers

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


class iP(Service):
    """
    \b
    Service code for the BBC iPlayer streaming service (https://www.bbc.co.uk/iplayer).

    \b
    Version: 1.0.1
    Author: stabbedbybrick
    Authorization: None
    Security: None

    \b
    Tips:
        - Use full title URL as input for best results.
        - Use --list-titles before anything, iPlayer's listings are often messed up.
    \b
        - Use --range HLG to request H.265 UHD tracks
        - See which titles are available in UHD:
            https://www.bbc.co.uk/iplayer/help/questions/programme-availability/uhd-content
    """

    ALIASES = ("bbciplayer", "bbc", "iplayer")
    GEOFENCE = ("gb",)
    TITLE_RE = r"^(?:https?://(?:www\.)?bbc\.co\.uk/(?:iplayer/(?P<kind>episode|episodes)/|programmes/))?(?P<id>[a-z0-9]+)(?:/.*)?$"
  
    @staticmethod
    @click.command(name="iP", short_help="https://www.bbc.co.uk/iplayer", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> "iP":
        return iP(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        super().__init__(ctx)
        self.title = title
        self.vcodec = ctx.parent.params.get("vcodec")
        self.range = ctx.parent.params.get("range_")

        self.session.headers.update({"user-agent": "BBCiPlayer/5.17.2.32046"})

        if self.range and self.range[0].name == "HLG":
            if not self.config.get("certificate"):
                raise CertificateMissingError("HLG/H.265 tracks cannot be requested without a TLS certificate.")

            self.session.headers.update({"user-agent": self.config["user_agent"]})
            self.vcodec = "H.265"

    def search(self) -> Generator[SearchResult, None, None]:
        r = self.session.get(self.config["endpoints"]["search"], params={"q": self.title})
        r.raise_for_status()

        results = r.json().get("new_search", {}).get("results", [])
        for result in results:
            programme_type = result.get("type", "unknown")
            category = result.get("labels", {}).get("category", "")
            path = "episode" if programme_type == "episode" else "episodes"
            yield SearchResult(
                id_=result.get("id"),
                title=result.get("title"),
                description=result.get("synopses", {}).get("small"),
                label=f"{programme_type} - {category}",
                url=f"https://www.bbc.co.uk/iplayer/{path}/{result.get('id')}",
            )

    def get_titles(self) -> Union[Movies, Series]:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError("Could not parse ID from title - is the URL/ID format correct?")

        groups = match.groupdict()
        pid = groups.get("id")
        kind = groups.get("kind")

        # Attempt to get brand/series data first
        data = self.get_data(pid, slice_id=None)

        # Handle case where the input is a direct episode URL and get_data fails
        if data is None and kind == "episode":
            return Series([self.fetch_episode(pid)])

        if data is None:
            raise MetadataError(f"Metadata not found for '{pid}'. If it's an episode, use the full URL.")

        # If it's a "series" with only one item, it might be a movie.
        if data.get("count", 0) < 2:
            r = self.session.get(self.config["endpoints"]["episodes"].format(pid=pid))
            r.raise_for_status()
            episodes_data = r.json()
            if not episodes_data.get("episodes"):
                raise MetadataError(f"Episode metadata not found for '{pid}'.")

            movie_data = episodes_data["episodes"][0]
            return Movies(
                [
                    Movie(
                        id_=movie_data.get("id"),
                        name=movie_data.get("title"),
                        year=(movie_data.get("release_date_time", "") or "").split("-")[0],
                        service=self.__class__,
                        language="en",
                        data=data,
                    )
                ]
            )

        # It's a full series
        seasons = [self.get_data(pid, x["id"]) for x in data.get("slices") or [{"id": None}]]
        episode_ids = [
            episode.get("episode", {}).get("id")
            for season in seasons
            for episode in season.get("entities", {}).get("results", [])
            if not episode.get("episode", {}).get("live")
            and episode.get("episode", {}).get("id")
        ]

        episodes = self.get_episodes(episode_ids)
        return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        versions = self._get_available_versions(title.id)
        if not versions:
            raise NoStreamsAvailableError("No available versions for this title were found.")

        connections = [self.check_all_versions(version["pid"]) for version in versions]
        connections = [c for c in connections if c]
        if not connections:
            if self.vcodec == "H.265":
                raise NoStreamsAvailableError("Selection unavailable in UHD.")
            raise NoStreamsAvailableError("Selection unavailable. Title may be missing or geo-blocked.")

        media = self._select_best_media(connections)
        if not media:
            raise NoStreamsAvailableError("Could not find a suitable media stream.")

        tracks = self._select_tracks(media, title.language)

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        return Chapters()

    def _get_available_versions(self, pid: str) -> list[dict]:
        """Fetch all available versions for a programme ID."""
        r = self.session.get(url=self.config["endpoints"]["playlist"].format(pid=pid))
        r.raise_for_status()
        playlist = r.json()

        versions = playlist.get("allAvailableVersions")
        if versions:
            return versions

        # Fallback to scraping webpage if API returns no versions
        self.log.info("No versions in playlist API, falling back to webpage scrape.")
        r = self.session.get(self.config["base_url"].format(type="episode", pid=pid))
        r.raise_for_status()
        match = re.search(r"window\.__IPLAYER_REDUX_STATE__\s*=\s*(.*?);\s*</script>", r.text)
        if match:
            redux_data = json.loads(match.group(1))
            # Filter out audio-described versions
            return [
                {"pid": v.get("id")}
                for v in redux_data.get("versions", {}).values()
                if v.get("kind") != "audio-described" and v.get("id")
            ]

        return []

    def _select_best_media(self, connections: list[list[dict]]) -> list[dict]:
        """Selects the media group corresponding to the highest available video quality."""
        heights = sorted(
            {
                int(c["height"])
                for media_list in connections
                for c in media_list
                if c.get("height", "").isdigit()
            },
            reverse=True,
        )

        if not heights:
            self.log.warning("No video streams with height information were found.")
            # Fallback: return the first available media group if any exist.
            return connections[0] if connections else None

        highest_height = heights[0]
        self.log.debug(f"Available resolutions (p): {heights}. Selecting highest: {highest_height}p.")

        best_media_list = next(
            (
                media_list
                for media_list in connections
                if any(conn.get("height") == str(highest_height) for conn in media_list)
            ),
            None,  # Default to None if no matching group is found (should be impossible if heights is not empty)
        )

        return best_media_list

    def _select_tracks(self, media: list[dict], lang: str):
        for video_stream_info in (m for m in media if m.get("kind") == "video"):
            connections = sorted(video_stream_info["connection"], key=lambda x: x.get("priority", 99))

            if self.vcodec == "H.265":
                connection = connections[0]
            else:
                connection = next((c for c in connections if c["supplier"] == "mf_akamai" and c["transferFormat"] == "dash"), None)

            break

        if not self.vcodec == "H.265":
            if connection["transferFormat"] == "dash":
                connection["href"] = "/".join(
                    connection["href"]
                    .replace("dash", "hls")
                    .split("?")[0]
                    .split("/")[0:-1]
                    + ["hls", "master.m3u8"]
                )
                connection["transferFormat"] = "hls"
            elif connection["transferFormat"] == "hls":
                connection["href"] = "/".join(
                    connection["href"]
                    .replace(".hlsv2.ism", "")
                    .split("?")[0]
                    .split("/")[0:-1]
                    + ["hls", "master.m3u8"]
                )

        if connection["transferFormat"] == "dash":
            tracks = DASH.from_url(url=connection["href"], session=self.session).to_tracks(language=lang)
        elif connection["transferFormat"] == "hls":
            tracks = HLS.from_url(url=connection["href"], session=self.session).to_tracks(language=lang)
        else:
            raise ValueError(f"Unsupported transfer format: {connection['transferFormat']}")

        for video in tracks.videos:
            # UHD DASH manifest has no range information, so we add it manually
            if video.codec == Video.Codec.HEVC:
                video.range = Video.Range.HLG

            if any(re.search(r"-audio_\w+=\d+", x) for x in as_list(video.url)):
                # create audio stream from the video stream
                audio_url = re.sub(r"-video=\d+", "", as_list(video.url)[0])
                audio = Audio(
                    # use audio_url not video url, as to ignore video bitrate in ID
                    id_=hashlib.md5(audio_url.encode()).hexdigest()[0:7],
                    url=audio_url,
                    codec=Audio.Codec.from_codecs(video.data["hls"]["playlist"].stream_info.codecs),
                    language=video.data["hls"]["playlist"].media[0].language,
                    bitrate=int(self.find(r"-audio_\w+=(\d+)", as_list(video.url)[0]) or 0),
                    channels=video.data["hls"]["playlist"].media[0].channels,
                    descriptive=False,  # Not available
                    descriptor=Audio.Descriptor.HLS,
                    drm=video.drm,
                    data=video.data,
                )
                if not tracks.exists(by_id=audio.id):
                    # some video streams use the same audio, so natural dupes exist
                    tracks.add(audio)
                # remove audio from the video stream
                video.url = [re.sub(r"-audio_\w+=\d+", "", x) for x in as_list(video.url)][0]
                video.codec = Video.Codec.from_codecs(video.data["hls"]["playlist"].stream_info.codecs)
                video.bitrate = int(self.find(r"-video=(\d+)", as_list(video.url)[0]) or 0)

        for caption in [x for x in media if x["kind"] == "captions"]:
            connection = sorted(caption["connection"], key=lambda x: x["priority"])[0]
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(connection["href"].encode()).hexdigest()[0:6],
                    url=connection["href"],
                    codec=Subtitle.Codec.from_codecs("ttml"),
                    language=lang,
                    is_original_lang=True,
                    forced=False,
                    sdh=True,
                )
            )
            break

        return tracks

    def get_data(self, pid: str, slice_id: str) -> dict:
        """Fetches programme metadata from the GraphQL-like endpoint."""
        json_data = {
            "id": "9fd1636abe711717c2baf00cebb668de",
            "variables": {"id": pid, "perPage": 200, "page": 1, "sliceId": slice_id},
        }
        r = self.session.post(self.config["endpoints"]["metadata"], json=json_data)
        r.raise_for_status()
        return r.json().get("data", {}).get("programme")

    def check_all_versions(self, vpid: str) -> list:
        """Checks media availability for a given version PID, trying multiple mediators."""
        session = self.session
        cert_path = None
        params = {}

        if self.vcodec == "H.265":
            if not self.config.get("certificate"):
                raise CertificateMissingError("TLS certificate not configured.")

            session.mount("https://", SSLCiphers())
            endpoint_template = self.config["endpoints"]["secure"]
            mediators = ["securegate.iplayer.bbc.co.uk", "ipsecure.stage.bbc.co.uk"]
            mediaset = "iptv-uhd"

            cert_binary = base64.b64decode(self.config["certificate"])
            with tempfile.NamedTemporaryFile(mode="w+b", delete=False, suffix=".pem") as cert_file:
                cert_file.write(cert_binary)
                cert_path = cert_file.name

            params["cert"] = cert_path
        else:
            endpoint_template = self.config["endpoints"]["open"]
            mediators = ["open.live.bbc.co.uk", "open.stage.bbc.co.uk"]
            mediaset = "iptv-all"

        for mediator in mediators:
            if self.vcodec == "H.265":
                url = endpoint_template.format(mediator, vpid, mediaset)
            else:
                url = endpoint_template.format(mediator, mediaset, vpid)

            try:
                r = session.get(url, **params)
                r.raise_for_status()
                availability = r.json()

                if availability.get("media"):
                    return availability["media"]
                if availability.get("result"):
                    self.log.warning(
                        f"Mediator '{mediator}' reported an error: {availability['result']}"
                    )

            except Exception as e:
                self.log.debug(f"Failed to check mediator '{mediator}': {e}")
            
            finally:
                if cert_path is not None:
                    Path(cert_path).unlink(missing_ok=True)

        return None

    def fetch_episode(self, pid: str) -> Episode:
        """Fetches and parses data for a single episode."""
        r = self.session.get(self.config["endpoints"]["episodes"].format(pid=pid))
        r.raise_for_status()
        data = r.json()

        if not data.get("episodes"):
            return None

        episode_data = data["episodes"][0]
        subtitle = episode_data.get("subtitle", "")
        year = (episode_data.get("release_date_time", "") or "").split("-")[0]

        series_match = next(re.finditer(r"Series (\d+).*?:|Season (\d+).*?:|(\d{4}/\d{2}): Episode \d+", subtitle), None)
        season_num = 0
        if series_match:
            season_str = next(g for g in series_match.groups() if g is not None)
            season_num = int(season_str.replace("/", ""))
        elif not data.get("slices"):  # Fallback for single-season shows
            season_num = 1

        num_match = next(re.finditer(r"(\d+)\.|Episode (\d+)", subtitle), None)
        number = 0
        if num_match:
            number = int(next(g for g in num_match.groups() if g is not None))
        else:
            number = episode_data.get("numeric_tleo_position", 0)

        name_match = re.search(r"\d+\. (.+)", subtitle)
        name = ""
        if name_match:
            name = name_match.group(1)
        elif not re.search(r"Series \d+: Episode \d+", subtitle):
            name = subtitle

        return Episode(
            id_=episode_data.get("id"),
            service=self.__class__,
            title=episode_data.get("title"),
            season=season_num,
            number=number,
            name=name,
            language="en",
            year=year,
        )

    def get_episodes(self, episode_ids: list) -> list[Episode]:
        """Fetches multiple episodes concurrently."""
        with ThreadPoolExecutor(max_workers=10) as executor:
            tasks = executor.map(self.fetch_episode, episode_ids)
        return [task for task in tasks if task is not None]
    
    def find(self, pattern, string, group=None):
        if group:
            m = re.search(pattern, string)
            if m:
                return m.group(group)
        else:
            return next(iter(re.findall(pattern, string)), None)


class iPlayerError(Exception):
    """Base exception for this service."""
    pass


class CertificateMissingError(iPlayerError):
    """Raised when an TLS certificate is required but not provided."""
    pass


class NoStreamsAvailableError(iPlayerError):
    """Raised when no playable streams are found for a title."""
    pass


class MetadataError(iPlayerError):
    """Raised when metadata for a title cannot be found."""
    pass