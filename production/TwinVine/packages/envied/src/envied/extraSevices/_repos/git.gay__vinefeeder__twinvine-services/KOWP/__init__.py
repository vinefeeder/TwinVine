import json
import re
from http.cookiejar import CookieJar
from typing import Optional, List, Dict, Any

import click
from langcodes import Language

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.search_result import SearchResult
from envied.core.titles import Episode, Series, Title_T, Titles_T
from envied.core.tracks import Subtitle, Tracks
from envied.core.utilities import is_close_match

class KOWP(Service):
    """
    Service code for Kocowa Plus (kocowa.com).
    Version: 1.0.0

    Auth: Credential (username + password)
    Security: FHD@L3
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?kocowa\.com/[^/]+/season/)?(?P<title_id>\d+)"
    GEOFENCE = ()
    NO_SUBTITLES = False

    @staticmethod
    @click.command(name="kowp", short_help="https://www.kocowa.com")
    @click.argument("title", type=str)
    @click.option("--extras", is_flag=True, default=False, help="Include teasers/extras")
    @click.pass_context
    def cli(ctx, **kwargs):
        return KOWP(ctx, **kwargs)

    def __init__(self, ctx, title: str, extras: bool = False):
        super().__init__(ctx)
        match = re.match(self.TITLE_RE, title)
        if match:
            self.title_id = match.group("title_id")
        else:
            self.title_id = title  # fallback to use as search keyword
        self.include_extras = extras
        self.brightcove_account_id = None
        self.brightcove_pk = None
        self.cdm = ctx.obj.cdm

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        if not credential:
            raise ValueError("KOWP requires username and password")

        payload = {
            "username": credential.username,
            "password": credential.password,
            "device_id": f"{credential.username}_browser",
            "device_type": "browser",
            "device_model": "Firefox",
            "device_version": "firefox/143.0",
            "push_token": None,
            "app_version": "v4.0.16",
        }
        r = self.session.post(
            self.config["endpoints"]["login"],
            json=payload,
            headers={"Authorization": "anonymous", "Origin": "https://www.kocowa.com"}
        )
        r.raise_for_status()
        res = r.json()
        if res.get("code") != "0000":
            raise PermissionError(f"Login failed: {res.get('message')}")

        self.access_token = res["object"]["access_token"]

        r = self.session.post(
            self.config["endpoints"]["middleware_auth"],
            json={"token": f"wA-Auth.{self.access_token}"},
            headers={"Origin": "https://www.kocowa.com"}
        )
        r.raise_for_status()
        self.middleware_token = r.json()["token"]

        self._fetch_brightcove_config()

    def _fetch_brightcove_config(self):
        """Fetch Brightcove account_id and policy_key from Kocowa's public config endpoint."""
        try:
            r = self.session.get(
                "https://middleware.bcmw.kocowa.com/api/config",
                headers={
                    "Origin": "https://www.kocowa.com",
                    "Referer": "https://www.kocowa.com/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
                }
            )
            r.raise_for_status()
            config = r.json()

            self.brightcove_account_id = config.get("VC_ACCOUNT_ID")
            self.brightcove_pk = config.get("BCOV_POLICY_KEY")

            if not self.brightcove_account_id:
                raise ValueError("VC_ACCOUNT_ID missing in /api/config response")
            if not self.brightcove_pk:
                raise ValueError("BCOV_POLICY_KEY missing in /api/config response")

            self.log.info(f"Brightcove config loaded: account_id={self.brightcove_account_id}")

        except Exception as e:
            raise RuntimeError(f"Failed to fetch or parse Brightcove config: {e}")

    def get_titles(self) -> Titles_T:
        all_episodes = []
        offset = 0
        limit = 20
        series_title = None # Store the title from the first request

        while True:
            url = self.config["endpoints"]["metadata"].format(title_id=self.title_id)
            sep = "&" if "?" in url else "?"
            url += f"{sep}offset={offset}&limit={limit}"

            r = self.session.get(
                url,
                headers={"Authorization": self.access_token, "Origin": "https://www.kocowa.com"}
            )
            r.raise_for_status()
            data = r.json()["object"]

            # Extract the series title only from the very first page
            if series_title is None and "meta" in data:
                series_title = data["meta"]["title"]["en"]

            page_objects = data.get("next_episodes", {}).get("objects", [])
            if not page_objects:
                break

            for ep in page_objects:
                is_episode = ep.get("detail_type") == "episode"
                is_extra = ep.get("detail_type") in ("teaser", "extra")
                if is_episode or (self.include_extras and is_extra):
                    all_episodes.append(ep)

            offset += limit
            total = data.get("next_episodes", {}).get("total_count", 0)
            if len(all_episodes) >= total or len(page_objects) < limit:
                break

        # If we never got the series title, exit with an error
        if series_title is None:
            raise ValueError("Could not retrieve series metadata to get the title.")

        episodes = []
        for ep in all_episodes:
            meta = ep["meta"]
            ep_type = "Episode" if ep["detail_type"] == "episode" else ep["detail_type"].capitalize()
            ep_num = meta.get("episode_number", 0)
            title = meta["title"].get("en") or f"{ep_type} {ep_num}"
            desc = meta["description"].get("en") or ""

            episodes.append(
                Episode(
                    id_=str(ep["id"]),
                    service=self.__class__,
                    title=series_title,
                    season=meta.get("season_number", 1),
                    number=ep_num,
                    name=title,
                    description=desc,
                    year=None,
                    language=Language.get("en"),
                    data=ep,
                )
            )

        return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        # Authorize playback
        r = self.session.post(
            self.config["endpoints"]["authorize"].format(episode_id=title.id),
            headers={"Authorization": f"Bearer {self.middleware_token}"}
        )
        r.raise_for_status()
        auth_data = r.json()
        if not auth_data.get("Success"):
            raise PermissionError("Playback authorization failed")
        self.playback_token = auth_data["token"]

        # Fetch Brightcove manifest
        manifest_url = (
            f"https://edge.api.brightcove.com/playback/v1/accounts/{self.brightcove_account_id}/videos/ref:{title.id}"
        )
        r = self.session.get(
            manifest_url,
            headers={"Accept": f"application/json;pk={self.brightcove_pk}"}
        )
        r.raise_for_status()
        manifest = r.json()

        # Get DASH URL + Widevine license
        dash_url = widevine_url = None
        for src in manifest.get("sources", []):
            if src.get("type") == "application/dash+xml":
                dash_url = src["src"]
                widevine_url = (
                    src.get("key_systems", {})
                    .get("com.widevine.alpha", {})
                    .get("license_url")
                )
                if dash_url and widevine_url:
                    break

        if not dash_url or not widevine_url:
            raise ValueError("No Widevine DASH stream found")

        self.widevine_license_url = widevine_url
        tracks = DASH.from_url(dash_url, session=self.session).to_tracks(language=title.language)

        for sub in manifest.get("text_tracks", []):
            srclang = sub.get("srclang")
            if not srclang or srclang == "thumbnails":
                continue

            subtitle_track = Subtitle(
                id_=sub["id"],
                url=sub["src"],
                codec=Subtitle.Codec.WebVTT,
                language=Language.get(srclang),
                sdh=True,  # Kocowa subs are SDH - mark them as such
                forced=False,
            )
            tracks.add(subtitle_track)

        return tracks

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        r = self.session.post(
            self.widevine_license_url,
            data=challenge,
            headers={
                "BCOV-Auth": self.playback_token,
                "Content-Type": "application/octet-stream",
                "Origin": "https://www.kocowa.com",
                "Referer": "https://www.kocowa.com/",
            }
        )
        r.raise_for_status()
        return r.content

    def search(self) -> List[SearchResult]:
       url = "https://prod-fms.kocowa.com/api/v01/fe/gks/autocomplete"
       params = {
           "search_category": "All",
           "search_input": self.title_id,
           "include_webtoon": "true",
       }

       r = self.session.get(
           url,
           params=params,
           headers={
               "Authorization": self.access_token,
               "Origin": "https://www.kocowa.com ",
               "Referer": "https://www.kocowa.com/ ",
           }
       )
       r.raise_for_status()
       response = r.json()
       contents = response.get("object", {}).get("contents", [])

       results = []
       for item in contents:
           if item.get("detail_type") != "season":
               continue 

           meta = item["meta"]
           title_en = meta["title"].get("en") or "[No Title]"
           description_en = meta["description"].get("en") or ""
           show_id = str(item["id"])

           results.append(
               SearchResult(
                   id_=show_id,
                   title=title_en,
                   description=description_en,
                   label="season",
                   url=f"https://www.kocowa.com/en_us/season/{show_id}/"
               )
           )
       return results

    def get_chapters(self, title: Title_T) -> list:
        return []

