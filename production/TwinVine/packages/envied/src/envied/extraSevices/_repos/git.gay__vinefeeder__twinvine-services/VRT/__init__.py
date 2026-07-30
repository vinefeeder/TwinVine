import json
import re
import time
import base64
import warnings  # Added
from http.cookiejar import CookieJar
from typing import Optional, List
from langcodes import Language

import click
import jwt
from bs4 import XMLParsedAsHTMLWarning  # Added
from collections.abc import Generator
from envied.core.search_result import SearchResult
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Tracks, Subtitle

# Ignore the BeautifulSoup XML warning caused by STPP subtitles
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# GraphQL Fragments and Queries
FRAGMENTS = """
fragment tileFragment on Tile {
  ... on ITile {
    title
    action { ... on LinkAction { link } }
  }
}
"""

QUERY_PROGRAM = """
query VideoProgramPage($pageId: ID!) {
  page(id: $pageId) {
    ... on ProgramPage {
      title
      components {
        __typename
        ... on PaginatedTileList { listId title }
        ... on StaticTileList { listId title }
        ... on ContainerNavigation {
          items {
            title
            components {
              __typename
              ... on PaginatedTileList { listId }
              ... on StaticTileList { listId }
            }
          }
        }
      }
    }
  }
}
"""

QUERY_PAGINATED_LIST = FRAGMENTS + """
query PaginatedTileListPage($listId: ID!, $after: ID) {
  list(listId: $listId) {
    ... on PaginatedTileList {
      paginatedItems(first: 50, after: $after) {
        edges { node { ...tileFragment } }
        pageInfo { endCursor hasNextPage }
      }
    }
    ... on StaticTileList {
      items { ...tileFragment }
    }
  }
}
"""

QUERY_PLAYBACK = """
query EpisodePage($pageId: ID!) {
  page(id: $pageId) {
    ... on PlaybackPage {
      title
      player { modes { streamId } }
    }
  }
}
"""

class VRT(Service):
    """
    Service code for VRT MAX (vrt.be)
    Version: 2.1.1
    Auth: Gigya + OIDC flow
    Security: FHD @ L3 (Widevine)
    Supports: 
     - Movies: https://www.vrt.be/vrtmax/a-z/rikkie-de-ooievaar-2/
       Series: https://www.vrt.be/vrtmax/a-z/schaar-steen-papier/
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?vrt\.be/vrtmax/a-z/)?(?P<slug>[^/]+)(?:/(?P<season_num>\d+)/(?P<episode_slug>[^/]+))?/?$"

    @staticmethod
    @click.command(name="VRT", short_help="https://www.vrt.be/vrtmax/")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return VRT(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        self.cdm = ctx.obj.cdm
        
        m = re.match(self.TITLE_RE, title)
        if m:
            self.slug = m.group("slug")
            self.is_series_root = m.group("episode_slug") is None
            if "vrtmax/a-z" in title:
                self.page_id = "/" + title.split("vrt.be/")[1].split("?")[0]
            else:
                self.page_id = f"/vrtmax/a-z/{self.slug}/"
        else:
            self.search_term = title

        self.access_token = None
        self.video_token = None

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        cache = self.cache.get("auth_data")
        if cache and not cache.expired:
            self.log.info("Using cached VRT session.")
            self.access_token = cache.data["access_token"]
            self.video_token = cache.data["video_token"]
            return

        if not credential or not credential.username or not credential.password: return

        self.log.info(f"Logging in to VRT as {credential.username}...")
        login_params = {
            "apiKey": self.config["settings"]["api_key"],
            "loginID": credential.username,
            "password": credential.password,
            "format": "json",
            "sdk": "Android_6.1.0"
        }
        r = self.session.post(self.config["endpoints"]["gigya_login"], data=login_params)
        gigya_data = r.json()
        if gigya_data.get("errorCode") != 0: raise PermissionError("Gigya login failed")

        sso_params = {"UID": gigya_data["UID"], "UIDSignature": gigya_data["UIDSignature"], "signatureTimestamp": gigya_data["signatureTimestamp"]}
        r = self.session.get(self.config["endpoints"]["vrt_sso"], params=sso_params)
        
        match = re.search(r'var response = "(.*?)";', r.text)
        token_data = json.loads(match.group(1).replace('\\"', '"'))
        self.access_token = token_data["tokens"]["access_token"]
        self.video_token = token_data["tokens"]["video_token"]

        decoded = jwt.decode(self.access_token, options={"verify_signature": False})
        cache.set(data={"access_token": self.access_token, "video_token": self.video_token}, expiration=int(decoded["exp"] - time.time()) - 300)

    def _get_gql_headers(self):
        return {
            "x-vrt-client-name": self.config["settings"]["client_name"],
            "x-vrt-client-version": self.config["settings"]["client_version"],
            "x-vrt-zone": "default",
            "authorization": f"Bearer {self.access_token}" if self.access_token else None,
            "Content-Type": "application/json"
        }

    def get_titles(self) -> Titles_T:
        if not self.is_series_root:
            r = self.session.post(self.config["endpoints"]["graphql"], json={"query": QUERY_PLAYBACK, "variables": {"pageId": self.page_id}}, headers=self._get_gql_headers())
            data = r.json()["data"]["page"]
            return Movies([Movie(id_=data["player"]["modes"][0]["streamId"], service=self.__class__, name=data["title"], language=Language.get("nl"), data={"page_id": self.page_id})])

        r = self.session.post(self.config["endpoints"]["graphql"], json={"query": QUERY_PROGRAM, "variables": {"pageId": self.page_id}}, headers=self._get_gql_headers())
        program_data = r.json().get("data", {}).get("page")
        if not program_data:
            raise ValueError(f"Series page not found: {self.page_id}")
            
        series_name = program_data["title"]
        episodes = []
        list_ids = []

        for comp in program_data.get("components", []):
            typename = comp.get("__typename")
            if typename in ("PaginatedTileList", "StaticTileList") and "listId" in comp:
                list_ids.append((comp.get("title") or "Episodes", comp["listId"]))
            elif typename == "ContainerNavigation":
                for item in comp.get("items", []):
                    item_title = item.get("title", "Episodes")
                    for sub in item.get("components", []):
                        if "listId" in sub:
                            list_ids.append((item_title, sub["listId"]))

        seen_lists = set()
        unique_list_ids = []
        for title, lid in list_ids:
            if lid not in seen_lists:
                unique_list_ids.append((title, lid))
                seen_lists.add(lid)

        for season_title, list_id in unique_list_ids:
            after = None
            while True:
                r_list = self.session.post(self.config["endpoints"]["graphql"], json={"query": QUERY_PAGINATED_LIST, "variables": {"listId": list_id, "after": after}}, headers=self._get_gql_headers())
                list_resp = r_list.json().get("data", {}).get("list")
                if not list_resp: break
                
                items_container = list_resp.get("paginatedItems")
                nodes = [e["node"] for e in items_container["edges"]] if items_container else list_resp.get("items", [])

                for node in nodes:
                    if not node.get("action"): continue
                    link = node["action"]["link"]
                    s_match = re.search(r'/(\d+)/.+s(\d+)a(\d+)', link)
                    episodes.append(Episode(
                        id_=link,
                        service=self.__class__,
                        title=series_name,
                        season=int(s_match.group(2)) if s_match else 1,
                        number=int(s_match.group(3)) if s_match else 0,
                        name=node["title"],
                        language=Language.get("nl"),
                        data={"page_id": link}
                    ))
                
                if items_container and items_container["pageInfo"]["hasNextPage"]:
                    after = items_container["pageInfo"]["endCursor"]
                else:
                    break

        if not episodes:
            raise ValueError("No episodes found for this series.")

        return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        page_id = title.data["page_id"]
        r_meta = self.session.post(self.config["endpoints"]["graphql"], json={"query": QUERY_PLAYBACK, "variables": {"pageId": page_id}}, headers=self._get_gql_headers())
        stream_id = r_meta.json()["data"]["page"]["player"]["modes"][0]["streamId"]

        p_info = base64.urlsafe_b64encode(json.dumps(self.config["player_info"]).encode()).decode().replace("=", "")
        r_tok = self.session.post(self.config["endpoints"]["player_token"], json={"identityToken": self.video_token, "playerInfo": f"eyJhbGciOiJIUzI1NiJ9.{p_info}."})
        vrt_player_token = r_tok.json()["vrtPlayerToken"]

        r_agg = self.session.get(self.config["endpoints"]["aggregator"].format(stream_id=stream_id), params={"client": self.config["settings"]["client_id"], "vrtPlayerToken": vrt_player_token})
        agg_data = r_agg.json()
        
        dash_url = next(u["url"] for u in agg_data["targetUrls"] if u["type"] == "mpeg_dash")
        tracks = DASH.from_url(dash_url, session=self.session).to_tracks(language=title.language)
        self.drm_token = agg_data["drm"]

        for sub in agg_data.get("subtitleUrls", []):
            tracks.add(Subtitle(id_=sub.get("label", "nl"), url=sub["url"], codec=Subtitle.Codec.WebVTT, language=Language.get(sub.get("language", "nl"))))

        for tr in tracks.videos + tracks.audio:
            if tr.drm: tr.drm.license = lambda challenge, **kw: self.get_widevine_license(challenge, title, tr)

        return tracks

    def get_widevine_license(self, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        r = self.session.post(self.config["endpoints"]["license"], data=challenge, headers={"x-vudrm-token": self.drm_token, "Origin": "https://www.vrt.be", "Referer": "https://www.vrt.be/"})
        return r.content

    def get_chapters(self, title: Title_T) -> list[Chapter]: 
        return []