from __future__ import annotations

import json
import re
from collections.abc import Generator
from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union
import click
from click import Context
from envied.core.credential import Credential
from envied.core.manifests.dash import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapters, Subtitle, Tracks
import requests
import uuid
# debugging
from rich.console import Console
console = Console()

def get_widevine_license_url(manifest_str):
    # Try JSON first
    try:
        data = json.loads(manifest_str)
        for source in data.get("sources", []):
            widevine = source.get("key_systems", {}).get("com.widevine.alpha")
            if widevine and "license_url" in widevine:
                return widevine["license_url"]
    except json.JSONDecodeError:
        pass

    # Fallback for XML style
    match = re.search(r'bc:licenseAcquisitionUrl="([^"]+)"', manifest_str)
    if match:
        return match.group(1)

    return None


class TPTV(Service):
    """
    Service code for TPTVencore streaming service (https://www.TPTVencore.co.uk/).

    \b
    version 1.1.0 
    Date: June 2026
    Author: A_n_g_e_l_a
    Authorization: email/password for service in envied.yaml
    Robustness:
        DRM free... with rare exceptions L3

    \b
    Note:
        TPTV will not allow the usual -w S01-S04 syntax as TPTV is eclictic in what it serves. 
        Series and episodes carry little meaning on this platform.
        It is not possible to remove S00E00 from the end of a video title - envied insists.

        This service is best used with Vinefeeder for its front end. 
        Then use a search term in preference to a url.

        Dealing with COLLECTION:
        In envied, any url with COLLECTION in capitals will attempt to list the whole collection.
        
        In vinefeeder, COLLECTION is ignored and the descriptive in the url is used as a search term.
        eg  https://tptvencore.co.uk/details/COLLECTION/collection/01KRKTC8WZ7X584YPA5NBFYJSS/the-danziger-collection
        will be treated as a search for "the danziger collection" in vinefeeder, but will attempt to list the whole collection in envied.
        Series and episodes are not well defined in TPTV, the collection listing is one way to find the episodes of a series.
        If you look on ay COLLECTON page at TPTVencore there are tabs showing series listings. Use vinefeeder to search for the series name 
        and to produce a selectable list.

        If COLLECTION name only returns one tab of results then manually from the web-page select each link url.
        They are of the form https://tptvencore.co.uk/playback/item/6386553443112

        Some restricted content will not download.
    \b
    Tips:
        Use complete url in all cases.
        SERIES: https://tptvencore.co.uk/details/COLLECTION/collection/01KRKTC8WZ7X584YPA5NBFYJSS/the-danziger-collection
        Note: TPTV do not specify Series and Episodes numbers in any meaningful and organized way. 
        They MAY sometimes be in the program title, but often incomplete. 
        FILM: https://tptvencore.co.uk/details/VIDEO/item/6390689314112/the-manster
        EPISODE: https://tptvencore.co.uk/details/VIDEO/item/6348388311112/maigret-peter-the-lett
        EPISODE: https://tptvencore.co.uk/playback/item/6386553443112
        TPTV makes no distinction between films and episodes, so the above is just a convention to help you find what you want.



    """

    GEOFENCE = ("gb",)
    ALIASES = ("TPTVencore",)

    @staticmethod
    @click.command(name="TPTV", short_help="https://www.tptvencore.co.uk/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> TPTV:
        return TPTV(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.profile = ctx.parent.params.get("profile")
        if not self.profile:
            self.profile = "default"
        self.session = requests.session()
        self.session.headers.update(self.config["headers"])

    def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        cache = self.cache.get(f"tokens_{credential.sha1}")
        # first contact
        fc_headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/151.0',
            'Accept': '*/*',
            'Accept-Language': 'en-GB,en;q=0.5',
            'api-key': self.config['session']['api-key'],
            'Content-Type': 'application/json',
            'Origin': 'https://tptvencore.co.uk',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'cross-site',
            'Priority': 'u=0',

            }
 
        
        r = self.session.get(self.config["endpoints"]["initial"], headers=fc_headers)
        if r.status_code != 200:
            raise ConnectionError   
        else:
            headers = r.headers
            self.session.headers.update({'session': headers.get('session')})
            
        
        # login
        if cache and not cache.expired:
            # cached
            self.log.info(" + Using cached Tokens...")
            tokens = cache.data
        else:
            self.log.info(" + Logging in...")
            random_uuid = str(uuid.uuid4())
            r = self.session.post(
                self.config["endpoints"]["login"],
                headers=self.session.headers,

                json= {
                "deviceInfo": {
                    "id": random_uuid,
                    "hardware": {
                    "manufacturer": "UNKNOWN/UNKNOWN",
                    "model": "Zimbadoo",
                    "version": "151.0"
                    },
                    "os": {
                    "name": "RisingHillOS",
                    "version": "x86_64"
                    },
                    "display": {
                    "width": 3840,
                    "height": 2048,
                    "formFactor": "THEATRE"
                    },
                    "legal": {}
                },
                "values": {
                    "email":  credential.username,
                    "password": credential.password
                }
                }
            )
            try:
                res = r.json()
            except json.JSONDecodeError:
                raise ValueError(f"Failed to refresh tokens: {r.text}")

            tokens = res
            self.log.info(" + Acquired tokens...")

            cache.set(tokens)

        self.authorization = tokens

    def search(self) -> Generator[SearchResult, None, None]:
        print(f"Searching not implemented in Envied for TPTVencore. Please use VineFeeder instead.")
        return None

    def get_titles(self) -> Union[Movies, Series]:
        data = self.get_data(self.title)

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0",
            "Accept": "*/*",
            "Accept-Language": "en-GB,en;q=0.5",
            "Referer": "https://tptvencore.co.uk/",
            #"tenant": "encore",
            "Origin": "https://tptvencore.co.uk",
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "Priority": "u=4",
            "session": self.session.headers["session"],
        }

        se_pattern = re.compile(
            r"\(\s*S\s*(?P<season>\d+)\s*,\s*EP\s*(?P<episode>\d+)\s*\)",
            re.IGNORECASE,
        )

        def extract_se(title: str) -> tuple[int, int]:
            match = se_pattern.search(title or "")
            if not match:
                return 0, 0

            return int(match.group("season")), int(match.group("episode"))

        def fetch_json(item_id: str | int) -> dict | None:
            """
            Try item endpoint first, then collection endpoint.
            Return decoded JSON or None if both fail.
            """
            urls = [
                f"https://tptvencore.co.uk/api/core/catalog/item/{item_id}?locale=en",
                f"https://tptvencore.co.uk/api/core/catalog/collection/{item_id}?page=1&pageSize=20&locale=en",
                f"https://tptvencore.co.uk/api/core/catalog/collection/{item_id}?locale=en",
            ]

            for url in urls:
                response = self.session.get(url, headers=headers)
                if response.status_code == 200:
                    return response.json()

            return None

        def make_episode(item: dict) -> Episode:
            title = item.get("title", "")
            season, number = extract_se(title)

            return Episode(
                id_=item["id"],
                service=self.__class__,
                title=title,
                season=season,
                number=number,
                name="",
                language="en",
                data=item,
            )

        episodes = []

        for item_id in data:
            # Skip bogus IDs
            if len(str(item_id)) > 20:
                continue

            mydata = fetch_json(item_id)
            if not mydata:
                continue

            titles = mydata.get("data")

            if not titles:
                continue

            # Collection endpoint may return a list of items
            if isinstance(titles, list):
                for item in titles:
                    episodes.append(make_episode(item))

            # Item endpoint returns a single dict
            elif isinstance(titles, dict):
                episodes.append(make_episode(titles))

        return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        playlist = f"https://edge.api.brightcove.com/playback/v1/accounts/6272132012001/videos/{title.data.get('id')}"

        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0',
            'Accept': '*/*',
            'Accept-Language': 'en-GB,en;q=0.5',
      
            'Referer': 'https://tptvencore.co.uk/',
            'BCOV-Policy': 'BCpkADawqM1yq3Go9abHJ4lBZ0wrYStC-pS1W01hdlACHxsiIz9AvQXy1wa3iqyd6yVJLXLZnZjFkKI2BCJjbtxiJqyPMZjIezEWKrI1TTSbugkD6dAXs7Ucxq09P9zQ8ZRU4ZjTa83VFhiL',
            'Origin': 'https://tptvencore.co.uk',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'cross-site',
            'Priority': 'u=4',
        }

        r = requests.get(playlist, headers=headers)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        data = r.json()
        
        self.manifest = data["sources"][2].get("src")

        tracks = DASH.from_url(self.manifest, self.session).to_tracks(title.language)
        tracks.videos[0].data = data
        
        # odd couple of DRM vids found

        self.license = get_widevine_license_url(r.text)
        
        
        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
      
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        r = self.session.post(url=self.license, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.content

    def get_data(self, url: str) -> dict:
        self.session.headers.update({'tenant': 'encore'})
        if 'COLLECTION' in url:
            prod_id = url.split('/')[-2]
            url = f"https://tptvencore.co.uk/api/core/catalog/collection/{prod_id}"

            r = self.session.get(url)
            if r.status_code != 200:
                raise ConnectionError(r.text)
            
            myjson = r.json()
            data = myjson.get("data", {})
            product_links = []
            for child in data:
                product_links.append(child.get('id', {})) if child.get('id', {}) else None
            return product_links
        elif 'playback' in url:  # single item
            prod_id = url.split('/')[-1] 
            return [prod_id]
        elif 'details' in url:
            prod_id = url.split('/')[-2]
            return [prod_id]
        else:
            raise ValueError("URL format not recognized for data retrieval in proc get_data().")