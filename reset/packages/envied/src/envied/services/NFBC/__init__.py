from __future__ import annotations
import json
import re
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Optional
import click
from langcodes import Language
from envied.core.credential import Credential
from envied.core.manifests import HLS
from envied.core.service import Service
from envied.core.titles import Movie, Movies, Title_T, Titles_T
from envied.core.tracks import Chapters, Tracks


class NFBC(Service):
    """
    Service code for National Film Board of Canada (https://www.nfb.ca)

    Author: n0stal6ic
    Authorization: None
    Geofence: CA, US
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?nfb\.ca/film/)?(?P<slug>[a-z0-9_-]+)/?$"
    GEOFENCE = ("CA", "US")

    @staticmethod
    @click.command(name="NFBC", short_help="https://www.nfb.ca", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return NFBC(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        m = re.match(self.TITLE_RE, title)
        if not m:
            raise ValueError(f"Could not parse NFB URL or slug: {title!r}")
        self.slug = m.group("slug")

    def authenticate(
        self,
        cookies: Optional[CookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        self.session.headers.update({
            "User-Agent": self.config["client"]["user_agent"],
            "Referer": "https://www.nfb.ca/",
        })

    def search(self) -> Generator:
        return
        yield

    def _get_film_page(self, slug: str) -> str:
        url = self.config["endpoints"]["film_page"].format(slug=slug)
        r = self.session.get(url)
        r.raise_for_status()
        return r.text

    def _extract_player_config(self, html: str) -> dict:
        match = re.search(
            r"window\.PLAYER_OPTIONS\['\d+'\]\s*=\s*(\{.*?\})\s*</script>",
            html,
            re.DOTALL,
        )
        if not match:
            raise ValueError("Could not find player config in film page.")
        return json.loads(match.group(1))

    def _get_metadata(self, registry_id: int) -> dict:
        r = self.session.get(
            self.config["endpoints"]["works_api"],
            params={
                "registry_ids": registry_id,
                "include_fields": [
                    "description",
                    "year",
                    "duration",
                    "directors",
                    "cataloging_language",
                    "availability",
                    "geoblocked",
                    "cc",
                    "dv",
                ],
                "subset": "nfb/published",
                "size": 1,
            },
        )
        r.raise_for_status()
        items = r.json().get("items") or []
        return items[0] if items else {}

    def get_titles(self) -> Titles_T:
        html = self._get_film_page(self.slug)
        player = self._extract_player_config(html)
        registry_id = player.get("registryId")
        meta = self._get_metadata(registry_id) if registry_id else {}

        title_str = meta.get("title") or player.get("gtm", {}).get("title") or self.slug
        year = meta.get("year")
        desc = meta.get("description")
        lang_code = meta.get("cataloging_language") or "en"
        try:
            language = Language.get(lang_code)
        except Exception:
            language = Language.get("en")

        return Movies([Movie(
            id_=self.slug,
            service=self.__class__,
            name=title_str,
            year=int(year) if year else None,
            description=desc,
            language=language,
            data={"player": player, "meta": meta},
        )])

    def get_tracks(self, title: Title_T) -> Tracks:
        player = title.data["player"]
        hls_url = player.get("source")
        if not hls_url:
            raise ValueError(f"No HLS source found for '{title.id}'.")

        self.log.debug(f"HLS URL: {hls_url}")
        tracks = HLS.from_url(url=hls_url, session=self.session).to_tracks(language=title.language)

        dv_url = player.get("dvSource")
        if dv_url:
            self.log.debug(f"Audio description HLS: {dv_url}")
            dv_tracks = HLS.from_url(url=dv_url, session=self.session).to_tracks(language=title.language)
            for audio in dv_tracks.audio:
                audio.descriptive = True
                tracks.add(audio)

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()