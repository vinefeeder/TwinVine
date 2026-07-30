from __future__ import annotations

import html
import json
import re
from collections.abc import Generator
from typing import Any
from urllib.parse import urljoin, urlparse

import click
from bs4 import BeautifulSoup
from click import Context
from requests import Request
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Series, Title_T
from envied.core.tracks import Chapters, Tracks


class BLAZ(Service):
    """
    \b
    Service code for BLAZE TV's free streaming service (https://watch.blaze.tv/).

    \b
    Version: 1.0.0
    Author: billybanana
    Authorization: None
    Geofence: GB/UK
    Robustness:
      HLS: 720p, AAC2.0

    \b
    Tips:
        - Search is supported:
          envied.dl BLAZ "outback wreckers"
        - Show URLs, public Blaze URLs, slugs, and episode URLs are supported:
          https://www.blaze.tv/outback-wreckers
          https://watch.blaze.tv/shows/4090b77a-538c-11f1-b4ab-021644e4b9e7/outback-wreckers
          outback-wreckers
          https://watch.blaze.tv/watch/vod/53361682/island-dreams-or-french-connection

    """

    ALIASES = ("blaze",)
    GEOFENCE = ("GB", "UK")
    TITLE_RE = (
        r"^(?:https?://)?"
        r"(?:(?:www\.)?blaze\.tv/|watch\.blaze\.tv/shows/[a-f0-9-]+/)?"
        r"(?P<slug>[a-z0-9-]+)$"
    )
    EPISODE_RE = r"^(?:https?://watch\.blaze\.tv)?/watch/vod/(?P<uvid>\d+)(?:/(?P<slug>[a-z0-9-]+))?"

    @staticmethod
    @click.command(name="BLAZ", short_help="https://watch.blaze.tv/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> BLAZ:
        return BLAZ(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.session.headers.update(self.config["headers"])

    def search(self) -> Generator[SearchResult, None, None]:
        query = self._clean_text(self.title).lower()

        for item in self._catalogue():
            haystack = f"{item['title']} {item['slug']}".lower()
            if query and query not in haystack:
                continue

            yield SearchResult(
                id_=item["url"],
                title=item["title"],
                description=item.get("description"),
                label="SERIES",
                url=item["url"],
            )

    def get_titles(self) -> Series:
        episode_match = re.match(self.EPISODE_RE, self.title)
        if episode_match:
            return Series(self._episode(episode_match.group("uvid"), self.title))

        show_url = self._resolve_show_url(self.title)
        html_text = self._request("GET", show_url)
        return Series(self._series(show_url, html_text))

    def get_tracks(self, title: Title_T) -> Tracks:
        token = self._request("GET", self.config["endpoints"]["token"].format(uvid=title.id))
        if not token.get("token") or not token.get("expiry"):
            raise ValueError(f"Failed to retrieve player token: {token}")

        data = title.data or {}
        content_type = data.get("type", "replay").lower()
        key = data.get("key") or self.config["playback"]["key"]

        stream = self._request(
            "POST",
            self.config["endpoints"]["streams"].format(type=content_type, uvid=title.id),
            params={
                "key": key,
                "platform": self.config["playback"]["platform"],
            },
            headers={
                "Accept": "application/json",
                "Origin": self.config["endpoints"]["base"],
                "Referer": data.get("url") or self.config["endpoints"]["base"],
                "Token": token["token"],
                "Token-Expiry": str(token["expiry"]),
                "Userid": "123456",
                "Uvid": str(title.id),
            },
        )

        response = stream.get("response", {})
        if response.get("error"):
            raise ValueError(f"Streams API error: {response['error']}")
        manifest = response.get("stream")
        if not manifest:
            raise ValueError(f"Streams API did not return a manifest: {stream}")

        return HLS.from_url(manifest, self.session).to_tracks(language=title.language)

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> str | None:
        return None

    def get_widevine_license(self, **_: Any) -> bytes | str | None:
        return None

    # Service specific

    def _catalogue(self) -> list[dict[str, str]]:
        html_text = self._request("GET", self.config["endpoints"]["series"])
        soup = BeautifulSoup(html_text, "html.parser")
        by_url: dict[str, dict[str, str]] = {}

        for a in soup.find_all("a", href=True):
            href = urljoin(self.config["endpoints"]["base"], a["href"])
            if "/shows/" not in urlparse(href).path:
                continue
            by_url[href] = {
                "url": href,
                "slug": self._slug_from_show_url(href),
                "title": self._title_from_node(a) or self._title_from_slug(self._slug_from_show_url(href)),
                "description": None,
            }

        for match in re.finditer(r"https://watch\.blaze\.tv/shows/[^'\"<>\s]+", html_text):
            href = html.unescape(match.group(0))
            if href in by_url:
                continue

            snippet = html_text[max(0, match.start() - 1200): match.end() + 1200]
            title = self._title_from_snippet(snippet) or self._title_from_slug(self._slug_from_show_url(href))
            by_url[href] = {
                "url": href,
                "slug": self._slug_from_show_url(href),
                "title": title,
                "description": None,
            }

        return sorted(by_url.values(), key=lambda item: item["title"].lower())

    def _resolve_show_url(self, title: str) -> str:
        if re.match(r"^https?://watch\.blaze\.tv/shows/", title):
            return title

        match = re.match(self.TITLE_RE, title)
        if not match:
            raise ValueError(f"Could not parse BLAZE title input: {title}")

        slug = match.group("slug")
        for item in self._catalogue():
            if item["slug"] == slug:
                return item["url"]

        raise ValueError(f"Could not resolve BLAZE show slug: {slug}")

    def _series(self, show_url: str, html_text: str) -> list[Episode]:
        soup = BeautifulSoup(html_text, "html.parser")
        show_title = self._show_title(soup)

        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(show_url, a["href"])
            match = re.search(r"/watch/vod/(\d+)/([^/?#]+)", href)
            if not match:
                continue

            uvid, slug = match.groups()
            if uvid in seen:
                continue
            seen.add(uvid)

            player = self._player_data(html_text, uvid)
            entries.append(
                {
                    "uvid": uvid,
                    "slug": slug,
                    "url": href,
                    "name": self._episode_title(a, slug),
                    "description": self._episode_description(a),
                    "player": player,
                }
            )

        if not entries:
            raise ValueError(f"No BLAZE VOD episodes found for {show_url}")

        total = len(entries)
        episodes = []
        for index, item in enumerate(entries):
            number = total - index
            data = {
                "url": item["url"],
                "slug": item["slug"],
                "type": item["player"].get("type", "replay"),
                "key": item["player"].get("key") or self.config["playback"]["key"],
                "poster": item["player"].get("poster"),
            }
            episodes.append(
                Episode(
                    id_=item["uvid"],
                    service=self.__class__,
                    title=show_title,
                    season=1,
                    number=number,
                    name=item["name"],
                    language="en",
                    data=data,
                    description=item["description"],
                )
            )

        return episodes

    def _episode(self, uvid: str, episode_url: str) -> list[Episode]:
        if episode_url.startswith("/"):
            episode_url = urljoin(self.config["endpoints"]["base"], episode_url)
        elif not episode_url.startswith("http"):
            episode_url = f"{self.config['endpoints']['base']}/watch/vod/{uvid}/{episode_url}"

        html_text = self._request("GET", episode_url)
        soup = BeautifulSoup(html_text, "html.parser")
        player = self._player_data(html_text, uvid)

        return [
            Episode(
                id_=uvid,
                service=self.__class__,
                title=self._show_title(soup),
                season=1,
                number=0,
                name=self._page_title(soup) or self._title_from_slug(urlparse(episode_url).path.rstrip("/").split("/")[-1]),
                language="en",
                data={
                    "url": episode_url,
                    "type": player.get("type", "replay"),
                    "key": player.get("key") or self.config["playback"]["key"],
                    "poster": player.get("poster"),
                },
            )
        ]

    def _player_data(self, html_text: str, uvid: str) -> dict[str, str]:
        soup = BeautifulSoup(html_text, "html.parser")
        node = soup.find(attrs={"data-uvid": uvid})
        data: dict[str, str] = {}
        if node:
            for key, value in node.attrs.items():
                if key.startswith("data-"):
                    data[key[5:].replace("-", "_")] = str(value)

        data.setdefault("uvid", uvid)
        data.setdefault("type", "replay")
        data.setdefault("key", self.config["playback"]["key"])
        return data

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        prep = self.session.prepare_request(Request(method, endpoint, **kwargs))
        response = self.session.send(prep)

        if response.status_code not in (200, 201):
            raise ConnectionError(response.text)

        content_type = response.headers.get("content-type", "")
        if "json" in content_type or response.text.strip().startswith(("{", "[")):
            try:
                return json.loads(response.content)
            except json.JSONDecodeError as e:
                raise ValueError(f"Failed to parse JSON: {response.text}") from e

        return response.text

    @staticmethod
    def _clean_text(value: str | None) -> str:
        return " ".join(html.unescape(value or "").split())

    @classmethod
    def _title_from_slug(cls, slug: str) -> str:
        return cls._clean_text(slug.replace("-", " ").title())

    @staticmethod
    def _slug_from_show_url(url: str) -> str:
        return urlparse(url).path.rstrip("/").split("/")[-1]

    @classmethod
    def _title_from_node(cls, node: Any) -> str:
        text = cls._clean_text(node.get_text(" ", strip=True))
        text = re.sub(r"\bPlay\b", "", text, flags=re.I)
        text = re.sub(r"\b\d+\s+Episodes?\b", "", text, flags=re.I)
        return cls._clean_text(text)

    @classmethod
    def _title_from_snippet(cls, snippet: str) -> str | None:
        alt = re.search(r'alt="([^"]+)"', snippet)
        if alt:
            title = cls._clean_text(alt.group(1))
            if title and title.lower() not in ("image", "menu"):
                return title

        overlay = re.search(r'overlay-title[^>]*>\s*([^<]+)', snippet, re.I)
        if overlay:
            return cls._clean_text(overlay.group(1))

        return None

    @classmethod
    def _show_title(cls, soup: BeautifulSoup) -> str:
        h1 = soup.find("h1")
        if h1:
            title = cls._clean_text(h1.get_text(" ", strip=True))
            if title and title.lower() not in ("series", "watch"):
                return title

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = re.sub(r"^BLAZE\s*::\s*", "", cls._clean_text(og_title["content"])).strip()
            if title and title.lower() not in ("series", "watch"):
                return title

        return "BLAZE"

    @classmethod
    def _page_title(cls, soup: BeautifulSoup) -> str | None:
        for selector in ({"property": "og:title"}, {"name": "twitter:title"}):
            meta = soup.find("meta", attrs=selector)
            if meta and meta.get("content"):
                title = re.sub(r"^BLAZE\s*::\s*", "", cls._clean_text(meta["content"])).strip()
                if title and title.lower() not in ("series", "watch"):
                    return title

        title_tag = soup.find("title")
        if title_tag:
            title = re.sub(r"^BLAZE\s*::\s*", "", cls._clean_text(title_tag.get_text())).strip()
            if title and title.lower() not in ("series", "watch"):
                return title

        return None

    @classmethod
    def _episode_title(cls, link: Any, slug: str) -> str:
        title = cls._title_from_node(link)
        return title or cls._title_from_slug(slug)

    @classmethod
    def _episode_description(cls, link: Any) -> str | None:
        node = link
        for _ in range(6):
            if not node:
                return None
            text = cls._clean_text(node.get_text(" ", strip=True))
            if len(text) > 80:
                title = cls._title_from_node(link)
                text = cls._clean_text(text.replace(title, "", 1))
                return text or None
            node = node.parent
        return None
