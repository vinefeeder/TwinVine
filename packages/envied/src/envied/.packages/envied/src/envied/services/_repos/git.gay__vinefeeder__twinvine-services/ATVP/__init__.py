import base64
import json
import re
from datetime import datetime

import click
import m3u8
import requests

from envied.core.downloaders import n_m3u8dl_re
from envied.core.manifests import m3u8 as m3u8_parser
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Audio, Subtitle, Tracks, Video
from envied.core.utils.collections import as_list
from pyplayready.cdm import Cdm as PlayReadyCdm


class ATVP(Service):
    """
    Service code for Apple's TV Plus streaming service (https://tv.apple.com).

    \b
    WIP: decrypt and removal of bumper/dub cards

    \b
    Authorization: Cookies
    Security: UHD@L1 FHD@L1 HD@L3
    """

    ALIASES = ["ATVP", "appletvplus", "appletv+"]
    TITLE_RE = (
        r"^(?:https?://tv\.apple\.com(?:/[a-z]{2})?/(?:movie|show|episode)/[a-z0-9-]+/)?(?P<id>umc\.cmc\.[a-z0-9]+)"  # noqa: E501
    )

    VIDEO_CODEC_MAP = {"H264": ["avc"], "H265": ["hvc", "hev", "dvh"]}
    AUDIO_CODEC_MAP = {"AAC": ["HE", "stereo"], "AC3": ["ac3"], "EC3": ["ec3", "atmos"]}

    @staticmethod
    @click.command(name="ATVP", short_help="https://tv.apple.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return ATVP(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.title = title
        self.cdm = ctx.obj.cdm
        if not isinstance(self.cdm, PlayReadyCdm):
            self.log.warning("PlayReady CDM not provided, exiting")
            raise SystemExit(1)
        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.alang = ctx.parent.params["lang"]
        self.subs_only = ctx.parent.params["subs_only"]
        self.quality = ctx.parent.params["quality"]

        self.extra_server_parameters = None
        # initialize storefront with a default value.
        self.storefront = 'us'  # or any default value

    def get_titles(self):
        self.configure()
        r = None
        for i in range(2):
            try:
                self.params = {
                    "utsk": "6e3013c6d6fae3c2::::::9318c17fb39d6b9c",
                    "caller": "web",
                    "sf": self.storefront,
                    "v": "46",
                    "pfm": "appletv",
                    "mfr": "Apple",
                    "locale": "en-US",
                    "l": "en",
                    "ctx_brand": "tvs.sbd.4000",
                    "count": "100",
                    "skip": "0",
                }
                r = self.session.get(
                    url=self.config["endpoints"]["title"].format(type={0: "shows", 1: "movies"}[i], id=self.title),
                    params=self.params,
                )
            except requests.HTTPError as e:
                if e.response.status_code != 404:
                    raise
            else:
                if r.ok:
                    break
        if not r:
            raise self.log.exit(f" - Title ID {self.title!r} could not be found.")
        try:
            title_information = r.json()["data"]["content"]
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load title manifest: {r.text}")

        if title_information["type"] == "Movie":
            movie = Movie(
                id_=self.title,
                service=self.__class__,
                name=title_information["title"],
                year=datetime.fromtimestamp(title_information["releaseDate"] / 1000).year,
                language=title_information["originalSpokenLanguages"][0]["locale"],
                data=title_information,
            )
            return Movies([movie])
        else:
            r = self.session.get(
                url=self.config["endpoints"]["tv_episodes"].format(id=self.title),
                params=self.params,
            )
            try:
                episodes = r.json()["data"]["episodes"]
            except json.JSONDecodeError:
                raise ValueError(f"Failed to load episodes list: {r.text}")

            episodes_list = [
                Episode(
                    id_=episode["id"],
                    service=self.__class__,
                    title=episode["showTitle"],
                    season=episode["seasonNumber"],
                    number=episode["episodeNumber"],
                    name=episode.get("title"),
                    year=datetime.fromtimestamp(title_information["releaseDate"] / 1000).year,
                    language=title_information["originalSpokenLanguages"][0]["locale"],
                    data={**episode, "originalSpokenLanguages": title_information["originalSpokenLanguages"]},
                )
                for episode in episodes
            ]
            return Series(episodes_list)

    def get_tracks(self, title):
        # call configure() before using self.storefront
        self.configure()

        self.params = {
            "utsk": "6e3013c6d6fae3c2::::::9318c17fb39d6b9c",
            "caller": "web",
            "sf": self.storefront,
            "v": "46",
            "pfm": "appletv",
            "mfr": "Apple",
            "locale": "en-US",
            "l": "en",
            "ctx_brand": "tvs.sbd.4000",
            "count": "100",
            "skip": "0",
        }
        r = self.session.get(
            url=self.config["endpoints"]["manifest"].format(id=title.data["id"]),
            params=self.params,
        )
        try:
            stream_data = r.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load stream data: {r.text}")
        stream_data = stream_data["data"]["content"]["playables"][0]

        if not stream_data["isEntitledToPlay"]:
            raise self.log.exit(" - User is not entitled to play this title")

        self.extra_server_parameters = stream_data["assets"]["fpsKeyServerQueryParameters"]
        r = requests.get(
            url=stream_data["assets"]["hlsUrl"],
            headers={"User-Agent": "AppleTV6,2/11.1"},
        )
        res = r.text

        master = m3u8.loads(res, r.url)
        tracks = m3u8_parser.parse(
            master=master,
            language=title.data["originalSpokenLanguages"][0]["locale"] or "en",
            session=self.session,
        )

        # Set track properties based on type
        for track in tracks:
            if isinstance(track, Video):
                # Convert codec string to proper Video.Codec enum if needed
                if isinstance(track.codec, str):
                    codec_str = track.codec.lower()
                    if codec_str in ["avc", "h264", "h.264"]:
                        track.codec = Video.Codec.AVC
                    elif codec_str in ["hvc", "hev", "hevc", "h265", "h.265", "dvh"]:
                        track.codec = Video.Codec.HEVC
                    else:
                        print(f"Unknown video codec '{track.codec}', keeping as string")

                # Set pr_pssh for PlayReady license requests
                if track.drm:
                    for drm in track.drm:
                        if hasattr(drm, 'data') and 'pssh_b64' in drm.data:
                            track.pr_pssh = drm.data['pssh_b64']
            elif isinstance(track, Audio):
                # Extract bitrate from URL
                bitrate = re.search(r"&g=(\d+?)&", track.url)
                if not bitrate:
                    bitrate = re.search(r"_gr(\d+)_", track.url)  # alternative pattern
                if bitrate:
                    track.bitrate = int(bitrate.group(1)[-3::]) * 1000  # e.g. 128->128,000, 2448->448,000
                else:
                    raise ValueError(f"Unable to get a bitrate value for Track {track.id}")
                codec_str = track.codec.replace("_vod", "") if track.codec else ""
                if codec_str == "DD+":
                    track.codec = Audio.Codec.EC3
                elif codec_str == "DD":
                    track.codec = Audio.Codec.AC3
                elif codec_str in ["HE", "stereo", "AAC"]:
                    track.codec = Audio.Codec.AAC
                elif codec_str == "atmos":
                    track.codec = Audio.Codec.EC3
                else:
                    if not hasattr(track.codec, "value"):
                        print(f"Unknown audio codec '{codec_str}', defaulting to AAC")
                        track.codec = Audio.Codec.AAC

                # Set pr_pssh for PlayReady license requests
                if track.drm:
                    for drm in track.drm:
                        if hasattr(drm, 'data') and 'pssh_b64' in drm.data:
                            track.pr_pssh = drm.data['pssh_b64']
            elif isinstance(track, Subtitle):
                codec_str = track.codec if track.codec else ""
                if codec_str.lower() in ["vtt", "webvtt"]:
                    track.codec = Subtitle.Codec.WebVTT
                elif codec_str.lower() in ["srt", "subrip"]:
                    track.codec = Subtitle.Codec.SubRip
                elif codec_str.lower() in ["ttml", "dfxp"]:
                    track.codec = Subtitle.Codec.TimedTextMarkupLang
                elif codec_str.lower() in ["ass", "ssa"]:
                    track.codec = Subtitle.Codec.SubStationAlphav4
                else:
                    if not hasattr(track.codec, "value"):
                        print(f"Unknown subtitle codec '{codec_str}', defaulting to WebVTT")
                        track.codec = Subtitle.Codec.WebVTT

                # Set pr_pssh for PlayReady license requests
                if track.drm:
                    for drm in track.drm:
                        if hasattr(drm, 'data') and 'pssh_b64' in drm.data:
                            track.pr_pssh = drm.data['pssh_b64']

        # Try to filter by CDN, but fallback to all tracks if filtering fails
        try:
            filtered_tracks = [
                x
                for x in tracks
                if any(
                    param.startswith("cdn=vod-ap") or param == "cdn=ap"
                    for param in as_list(x.url)[0].split("?")[1].split("&")
                )
            ]

            for track in tracks:
                if track not in tracks.attachments:
                    track.downloader = n_m3u8dl_re
                    if isinstance(track, (Video, Audio)):
                        track.needs_repack = True

            if filtered_tracks:
                return Tracks(filtered_tracks)
            else:
                return Tracks(tracks)

        except Exception:
            return Tracks(tracks)

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def get_pssh(self, track) -> None:
        res = self.session.get(as_list(track.url)[0])
        playlist = m3u8.loads(res.text, uri=res.url)
        keys = list(filter(None, (playlist.session_keys or []) + (playlist.keys or [])))
        for key in keys:
            if key.keyformat and "playready" in key.keyformat.lower():
                track.pr_pssh = key.uri.split(",")[-1]
                return

    def get_playready_license(self, *, challenge: bytes, title, track) -> str:
        if isinstance(challenge, str):
            challenge = challenge.encode()

        self.get_pssh(track)

        res = self.session.post(
            url=self.config["endpoints"]["license"],
            json={
                "streaming-request": {
                    "version": 1,
                    "streaming-keys": [
                        {
                            "challenge": base64.b64encode(challenge).decode("utf-8"),
                            "key-system": "com.microsoft.playready",
                            "uri": f"data:text/plain;charset=UTF-16;base64,{track.pr_pssh}",
                            "id": 0,
                            "lease-action": "start",
                            "adamId": self.extra_server_parameters["adamId"],
                            "isExternal": True,
                            "svcId": self.extra_server_parameters["svcId"],
                        },
                    ],
                },
            },
        ).json()
        return res["streaming-response"]["streaming-keys"][0]["license"]

    # Service specific functions

    def configure(self):
        cc = self.session.cookies.get_dict()["itua"]
        r = self.session.get(
            "https://gist.githubusercontent.com/BrychanOdlum/2208578ba151d1d7c4edeeda15b4e9b1/raw/8f01e4a4cb02cf97a48aba4665286b0e8de14b8e/storefrontmappings.json"
        ).json()
        for g in r:
            if g["code"] == cc:
                self.storefront = g["storefrontId"]

        environment = self.get_environment_config()
        if not environment:
            raise ValueError("Failed to get AppleTV+ WEB TV App Environment Configuration...")
        self.session.headers.update(
            {
                "User-Agent": self.config["user_agent"],
                "Authorization": f"Bearer {environment['developerToken']}",
                "media-user-token": self.session.cookies.get_dict()["media-user-token"],
                "x-apple-music-user-token": self.session.cookies.get_dict()["media-user-token"],
            }
        )

    def get_environment_config(self):
        """Loads environment config data from WEB App's serialized server data."""
        res = self.session.get("https://tv.apple.com").text

        script_match = re.search(
            r'<script[^>]*id=["\']serialized-server-data["\'][^>]*>(.*?)</script>',
            res,
            re.DOTALL,
        )
        if script_match:
            try:
                script_content = script_match.group(1).strip()
                data = json.loads(script_content)
                if data and len(data) > 0 and "data" in data[0] and "configureParams" in data[0]["data"]:
                    return data[0]["data"]["configureParams"]
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                print(f"Failed to parse serialized server data: {e}")

        return None
