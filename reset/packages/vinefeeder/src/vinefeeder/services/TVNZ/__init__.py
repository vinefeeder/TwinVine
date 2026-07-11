from __future__ import annotations

import copy
from importlib.resources import files
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import click
from envied.core import service
import yaml
from beaupy import select_multiple
from rich.console import Console

from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import split_options, list_prettify

from envied.core import binaries
from envied.core.config import config as envied_config
from envied.core.proxies import Basic, Gluetun, Hola, NordVPN, SurfsharkVPN, WindscribeVPN
from envied.core.titles import Movies, Series
from envied.services.TVNZ import TVNZ as EnviedTVNZ

console = Console()


class TvnzLoader(BaseLoader):
    """
    Vinefeeder adapter for TVNZ using some code from StabbedByBrick's service.
    This service was written by AI.

    Keep TVNZ API/login/catalogue logic in envied.services.TVNZ.
    Vinefeeder's job here is only:

    1. search and display programme-level results;
    2. expand a chosen TV series into individual envied Episode titles;
    3. let the user choose individual episodes with beaupy;
    4. call envied once per selected single title.
    """

    options = ""

    def __init__(self):
        headers = {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        }
        super().__init__(headers)
        self.options_list: list[str] = []
        self.category = None
        self._tvnz_service: EnviedTVNZ | None = None

    # ---------------------------------------------------------------------
    # Vinefeeder entry points
    # ---------------------------------------------------------------------

    def receive(
        self,
        inx: int | None,
        search_term: str | None,
        category=None,
        hlg_status=False,
        opts=None,
    ):
        if opts is not None:
            TvnzLoader.options = opts

        self.options_list = self._normalised_options()
        search_term = (search_term or "").strip()

        if not search_term:
            print("No search term or URL supplied.")
            return None

        # Direct download from the GUI URL field.
        if inx == 1 and self._looks_like_url(search_term):
            return self._download_url(search_term)

        # Keyword search.
        if inx == 3:
            print(f"Searching TVNZ for {search_term}")
            return self.fetch_videos(search_term)

        # Greedy mode: URL means expand it; plain text means search it.
        if inx == 0:
            if self._looks_like_url(search_term):
                return self.second_fetch(self._normalise_tvnz_url(search_term))
            return self.fetch_videos(search_term)

        # Category browse is now only a light wrapper.  TVNZ's old category API
        # was part of the code we are intentionally not duplicating.
        if inx == 2 and self._looks_like_url(search_term):
            self.category = category
            return self.fetch_videos_by_category(search_term)

        print(f"Unknown TVNZ request: inx={inx!r}, search_term={search_term!r}")
        return None

    def fetch_videos(self, search_term: str):
        """
        First pass: use envied TVNZ search(), then let Vinefeeder choose a title.

        The data stored in BaseLoader.series_data is deliberately small:
        just enough for Vinefeeder's programme picker and second_fetch().
        """
        self.clear_series_data()

        try:
            service = self._envied_tvnz(search_term, authenticate=True)
            self._tvnz_service = service
            results = list(service.search())
        except SystemExit:
            return None
        except Exception as e:
            print(f"TVNZ search failed: {e}")
            return None

        if not results:
            print(f"No TVNZ matches found for {search_term!r}.")
            return None

        for result in results:
            title = result.title or "Unknown Title"
            self.add_episode(
                title,
                {
                    "type": result.label,
                    "title": result.title,
                    "url": self._normalise_tvnz_url(result.url or str(result.id)),
                    "synopsis": result.description or "No synopsis available.",
                },
            )

        selected_series = self.display_series_list()
        if selected_series:
            return self.second_fetch(selected_series)

        return None

    def second_fetch(self, selected: str):
        """
        Expand the selected TVNZ result.

        * tvseries with 2+ episodes: show a beaupy multi-select list.
        * tvseries with 1 episode: call envied for that one episode.
        * movie/event/highlight/news clip/sport clip: call envied directly.
        """
        self.options_list = self._normalised_options()

        selected_url = self._selected_to_url(selected)
        if not selected_url:
            print(f"No TVNZ URL found for {selected!r}.")
            return None

        selected_url = self._normalise_tvnz_url(selected_url)

        try:
            service = self._tvnz_service

            if service is None:
                # Direct URL / greedy path: second_fetch may be entered without fetch_videos()
                service = self._envied_tvnz(selected_url, authenticate=True)
            else:
                # Reuse the authenticated envied TVNZ instance,
                # but point it at the selected TVNZ URL before get_titles_cached().
                service.title = selected_url

            titles = service.get_titles_cached()

        except SystemExit:
            return None
        except Exception as e:
            print(f"TVNZ title lookup failed for {selected_url}: {e}")
            return None
        # Movies also covers TVNZ sport events, highlights and clips in the
        # envied TVNZ implementation.
        if isinstance(titles, Movies):
            return self._download_url(selected_url)

        if not isinstance(titles, Series):
            # Defensive fallback for any future envied title container.
            return self._download_url(selected_url)

        episodes = list(titles)

        # Single tvepisode, or a "series" container that only has one playable
        # item: no list required.
        if len(episodes) <= 1:
            if not episodes:
                print(f"No playable TVNZ episodes found for {selected_url}.")
                return None
            return self._download_url(self._title_to_url(episodes[0], fallback=selected_url))

        beaupylist = []
        for episode in episodes:
            url = self._title_to_url(episode, fallback=selected_url)
            beaupylist.append(
                [
                    self._season_episode_label(episode),
                    episode.name or episode.title,
                    url,
                    self._synopsis(episode.data),
                ]
            )

        selected_episodes = select_multiple(
            beaupylist,
            preprocessor=lambda val: list_prettify(val),
            minimal_count=1,
            cursor_style="pink1",
            pagination=True,
            page_size=8,
        )

        for item in selected_episodes:
            url = item[2]
            if not url:
                print(f"No valid TVNZ URL for {item[0]} {item[1]}")
                continue
            self._download_url(url)

        return None

    def fetch_videos_by_category(self, browse_url: str):
        """
        TVNZ category browsing used to rely on the old public web/category API.

        With the current envied TVNZ service the reliable path is:
        category URL -> derive a search phrase -> envied TVNZ search().
        """
        term = self._search_term_from_url(browse_url)
        if not term:
            print(f"Cannot derive a TVNZ search term from {browse_url!r}.")
            return None
        return self.fetch_videos(term)

    # ---------------------------------------------------------------------
    # Envied adapter
    # ---------------------------------------------------------------------

    def _envied_tvnz(self, title: str, authenticate: bool = True) -> EnviedTVNZ:
        """
        Build just enough Click context for envied.services.TVNZ.TVNZ to run
        outside the envied CLI command.

        This deliberately mirrors the envied command path:
        ctx.obj.config is the service config;
        ctx.obj.cdm may be None because Vinefeeder is only reading metadata;
        ctx.obj.proxy_providers is populated from envied.yaml where possible.
        """
        parent = click.Context(click.Command("dl"))
        parent.params = {
            "profile": self._option_value("--profile", "-p"),
            "proxy": self._option_value("--proxy"),
            "proxy_query": None,
            "proxy_provider": None,
            "no_proxy": self._has_option("--no-proxy"),
            "vcodec": [],
            "range_": [],
            "best_available": False,
            "no_cache": self._has_option("--no-cache"),
            "reset_cache": self._has_option("--reset-cache"),
        }

        ctx = click.Context(click.Command("TVNZ"), parent=parent)
        ctx.obj = SimpleNamespace(
            config=self._load_envied_tvnz_config(),
            cdm=None,
            proxy_providers=self._load_envied_proxy_providers(parent.params["no_proxy"]),
        )

        service = EnviedTVNZ(ctx, title=title)

        if authenticate:
            # TVNZ.__init__ has already resolved the correct credential/cache
            # object. Passing that same credential avoids Service.authenticate()
            # replacing it with None.
            service.authenticate(None, getattr(service, "credential", None))

        return service

    def _load_envied_tvnz_config(self) -> dict[str, Any]:
        packaged_config: dict[str, Any] = {}

        try:
            config_text = files("envied.services.TVNZ").joinpath("config.yaml").read_text(
                encoding="utf-8"
            )
            packaged_config = yaml.safe_load(config_text) or {}
        except Exception:
            packaged_config = {}

        user_config = copy.deepcopy(envied_config.services.get("TVNZ") or {})
        merged = self._deep_merge(packaged_config, user_config)

        if not merged:
            raise RuntimeError(
                "No envied TVNZ service config found. Check envied.yaml and "
                "packages/envied/src/envied/services/TVNZ/config.yaml."
            )

        return merged

    @classmethod
    def _load_envied_proxy_providers(cls, no_proxy: bool) -> list[Any]:
        if no_proxy:
            return []

        providers: list[Any] = []
        proxy_cfg = envied_config.proxy_providers or {}

        try:
            if proxy_cfg.get("basic"):
                providers.append(Basic(**proxy_cfg["basic"]))
            if proxy_cfg.get("nordvpn"):
                providers.append(NordVPN(**proxy_cfg["nordvpn"]))
            if proxy_cfg.get("surfsharkvpn"):
                providers.append(SurfsharkVPN(**proxy_cfg["surfsharkvpn"]))
            if proxy_cfg.get("windscribevpn"):
                providers.append(WindscribeVPN(**proxy_cfg["windscribevpn"]))
            if proxy_cfg.get("gluetun"):
                providers.append(Gluetun(**proxy_cfg["gluetun"]))
            if binaries.HolaProxy:
                providers.append(Hola())
        except Exception as e:
            print(f"Could not load envied proxy providers for TVNZ metadata lookup: {e}")

        return providers

    # ---------------------------------------------------------------------
    # Download command construction
    # ---------------------------------------------------------------------

    def _download_url(self, url: str):
        url = self._normalise_tvnz_url(url)
        command = ["uv", "run", "envied", "dl", *self.options_list, "TVNZ", url]
        self.runsubprocess(command)
        return None

    # ---------------------------------------------------------------------
    # Small helpers
    # ---------------------------------------------------------------------

    def _normalised_options(self) -> list[str]:
        try:
            return [x for x in split_options(TvnzLoader.options or "") if x]
        except Exception:
            return []

    def _option_value(self, long_name: str, short_name: str | None = None) -> str | None:
        names = {long_name}
        if short_name:
            names.add(short_name)

        for i, token in enumerate(self.options_list):
            if token in names and i + 1 < len(self.options_list):
                return self.options_list[i + 1]
            if token.startswith(f"{long_name}="):
                return token.split("=", 1)[1]

        return None

    def _has_option(self, name: str) -> bool:
        return name in self.options_list

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        return value.startswith("http://") or value.startswith("https://")

    @staticmethod
    def _normalise_tvnz_url(url: str) -> str:
        # Envied's TITLE_RE accepts tvnz.co.nz and www.tvnz.co.nz.  Keep this
        # mainly to remove accidental whitespace and make search-created URLs
        # consistent.
        return url.strip().replace("https://www.tvnz.co.nz/", "https://tvnz.co.nz/")

    def _selected_to_url(self, selected: str) -> str | None:
        if self._looks_like_url(selected):
            return selected

        data = self.get_series_data()
        rows = data.get(selected) or []
        if not rows:
            return None

        return rows[0].get("url")

    @staticmethod
    def _title_to_url(title: Any, fallback: str) -> str:
        data = getattr(title, "data", None) or {}

        content_type = data.get("cty")
        content_id = data.get("nu") or getattr(title, "id", None)

        if content_type and content_id:
            return f"https://tvnz.co.nz/{content_type}/{content_id}"

        # Defensive fallbacks for possible future TVNZ shapes.
        for key in ("url", "href", "path"):
            value = data.get(key)
            if isinstance(value, str) and value:
                if value.startswith("http"):
                    return value
                return f"https://tvnz.co.nz{value if value.startswith('/') else '/' + value}"

        page = data.get("page")
        if isinstance(page, dict):
            value = page.get("url")
            if isinstance(value, str) and value:
                if value.startswith("http"):
                    return value
                return f"https://tvnz.co.nz{value if value.startswith('/') else '/' + value}"

        return fallback

    @staticmethod
    def _season_episode_label(episode: Any) -> str:
        season = getattr(episode, "season", 0)
        number = getattr(episode, "number", 0)

        try:
            return f"S{int(season):02}E{int(number):02}"
        except Exception:
            return f"S{season}E{number}"

    @classmethod
    def _synopsis(cls, data: Any) -> str:
        if not isinstance(data, dict):
            return "No synopsis available."

        # TVNZ catalogue values are often localised lists like:
        # [{"n": "Some text"}]
        for key in ("losd", "sd", "synopsis", "description"):
            value = data.get(key)
            text = cls._first_localised_text(value)
            if text:
                return text

        return "No synopsis available."

    @staticmethod
    def _first_localised_text(value: Any) -> str | None:
        if isinstance(value, str):
            return value

        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                text = first.get("n")
                if isinstance(text, str):
                    return text
            if isinstance(first, str):
                return first

        if isinstance(value, dict):
            text = value.get("n")
            if isinstance(text, str):
                return text

        return None

    @staticmethod
    def _search_term_from_url(url: str) -> str:
        path = urlparse(url).path.strip("/")
        if not path:
            return ""

        # Usually the last useful slug is the most specific category/title text.
        slug = path.split("/")[-1]
        return slug.replace("-", " ").replace("_", " ").strip()

    @classmethod
    def _deep_merge(cls, defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(defaults or {})

        for key, value in (overrides or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = cls._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)

        return result
