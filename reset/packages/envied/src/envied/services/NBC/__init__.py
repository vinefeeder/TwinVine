from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Generator
from typing import Optional, Union
from urllib.parse import urlencode

import click
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from envied.core.constants import AnyTrack
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Series, Title_T, Titles_T
from envied.core.tracks import Chapters, Tracks


# NBC ships the page metadata AND the obfuscated config (which contains
# drmProxySecret) inline in every nbc.com page's HTML, both inside a
# `PRELOAD={...}` JS global. Layout we depend on:
#   PRELOAD.pages[<url-path>].base       — same shape as the legacy GraphQL response
#                                          (replaces friendship.nbc.com/v3/graphql)
#   PRELOAD.client.oc                    — base64-encoded encrypted config blob
#
# Obfuscated-config payload format (after base64-decode):
#   bytes  0..12   : AES-GCM IV (12 bytes)
#   bytes 12..44   : AES-256 key (32 bytes - the key is shipped next to the
#                    ciphertext, this is obfuscation, not security)
#   bytes 44..-4   : AES-GCM ciphertext (includes 16-byte auth tag)
#   bytes -4..     : COMPATIBILITY_VERSION (uint32 big-endian) — sanity check
# Decrypted plaintext is UTF-8 JSON. Currently exposes `{coreVideo: {drmProxySecret}}`.
_OC_COMPATIBILITY_VERSION = 1


class NBC(Service):
    """
    \b
    Service code for NBC.com (https://www.nbc.com).

    \b
    Version: 0.1.0
    Authorization: None (free-tier content only — TV-provider auth not implemented)
    Robustness:
      Widevine: L3

    \b
    Tips:
        - Input may be either:
          SERIES:  https://www.nbc.com/<show-slug>                          (current season only)
          EPISODE: https://www.nbc.com/<show-slug>/video/<episode-slug>/<id>
        - Series URLs enumerate only the most recent season — older seasons are
          typically behind Peacock auth and not surfaced by the free-tier API.
        - Content with active TV-provider entitlement windows will fail — wait until the
          episode's free window opens (typically ~7 days after first air).
    """

    GEOFENCE = ("us",)

    TITLE_RE = re.compile(
        r"^https?://www\.nbc\.com/(?P<show>[a-zA-Z0-9_-]+)"
        r"(?:/video/[a-zA-Z0-9_-]+/(?P<id>\d+))?/?$"
    )

    @staticmethod
    @click.command(name="NBC", short_help="https://www.nbc.com", help=__doc__)
    @click.argument("title", type=str, required=True)
    @click.pass_context
    def cli(ctx, **kwargs) -> NBC:
        return NBC(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)
        # URL parsing happens lazily in get_titles() — search() accepts a free-text
        # query that won't match TITLE_RE.

    # Service API

    def search(self) -> Generator[SearchResult, None, None]:
        algolia = self.config["algolia"]
        entity_types = ["series", "episodes", "movies"]
        facet_filters = json.dumps([[f"algoliaProperties.entityType:{t}" for t in entity_types]])
        algolia_params = urlencode({
            "query": self.title,
            "facetFilters": facet_filters,
            "page": 0,
            "hitsPerPage": 20,
        })
        body = {"requests": [{"indexName": algolia["index"], "params": algolia_params}]}
        r = self.session.post(
            algolia["url"],
            headers={
                **self.config["headers"],
                "content-type": "application/x-www-form-urlencoded",
                "x-algolia-api-key": algolia["api_key"],
                "x-algolia-application-id": algolia["app_id"],
            },
            json=body,
        )
        r.raise_for_status()
        for hit in r.json().get("results", [{}])[0].get("hits") or []:
            entity_type = (hit.get("algoliaProperties") or {}).get("entityType")
            if entity_type == "series":
                series_data = hit.get("series") or {}
                slug = series_data.get("seriesName") or series_data.get("urlAlias")
                if not slug:
                    continue
                yield SearchResult(
                    id_=hit.get("objectID") or slug,
                    title=series_data.get("shortTitle") or slug,
                    description=series_data.get("shortDescription"),
                    label="Series",
                    url=f"https://www.nbc.com/{slug}",
                )
            elif entity_type == "episodes":
                ep_data = hit.get("episegment") or {}
                video = hit.get("video") or {}
                season = hit.get("season") or {}
                series_data = hit.get("series") or {}
                permalink = (video.get("permalink") or "").replace("http://", "https://")
                if not permalink:
                    continue
                season_n = season.get("seasonNumber")
                episode_n = ep_data.get("episodeNumber")
                label_bits = [series_data.get("shortTitle")]
                if season_n is not None and episode_n is not None:
                    label_bits.append(f"S{season_n:02d}E{episode_n:02d}")
                yield SearchResult(
                    id_=video.get("mpxGuid") or hit.get("objectID"),
                    title=ep_data.get("title") or "(untitled)",
                    description=ep_data.get("shortDescription"),
                    label=" · ".join(b for b in label_bits if b),
                    url=permalink,
                )

    def get_titles(self) -> Titles_T:
        match = self.TITLE_RE.match(self.title)
        if not match:
            raise ValueError(f"Could not parse NBC URL: {self.title!r}")
        show_slug = match.group("show")
        mpx_guid = match.group("id")
        self.show_slug = show_slug  # cached for _episode_from_meta fallback
        if mpx_guid:
            return Series([self._episode_from_url()])
        return Series(self._show(show_slug))

    def get_tracks(self, title: Title_T) -> Tracks:
        manifest_url = self._fetch_manifest_url(title)
        return DASH.from_url(url=manifest_url).to_tracks(language=title.language)

    def get_chapters(self, title: Episode) -> Chapters:
        return Chapters()

    def certificate(self, **_):
        return None  # use common privacy cert

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        # hash = HMAC-SHA256(drm_proxy_secret, str(time_ms) + "widevine")
        # The secret is extracted per-title from the obfuscated `oc` blob on the
        # episode/show page HTML — see _fetch_page / _decrypt_oc.
        secret = title.data.get("drm_proxy_secret")
        if not secret:
            raise ValueError(
                "NBC: title has no drm_proxy_secret on its data — was it created outside of get_titles()?"
            )
        time_ms = str(int(time.time() * 1000))
        url_hash = hmac.new(
            secret.encode(), (time_ms + "widevine").encode(), hashlib.sha256
        ).hexdigest()

        r = self.session.post(
            self.config["endpoints"]["license_url"],
            params={"time": time_ms, "hash": url_hash, "device": "web"},
            headers={**self.config["headers"], "content-type": "application/octet-stream"},
            data=challenge,
        )
        if not r.ok:
            raise ConnectionError(f"NBC license request failed (HTTP {r.status_code}): {r.text[:200]}")
        return r.content

    # Service-specific helpers ---------------------------------------------------

    def _episode_from_url(self) -> Episode:
        # The page URL path after nbc.com/, e.g.
        # "law-and-order-special-victims-unit/video/monster/9000448060"
        path = re.match(r"https?://www\.nbc\.com/(.+?)/?$", self.title).group(1)
        page, secret = self._fetch_page(path)
        return self._episode_from_meta(page["metadata"], secret)

    def _show(self, slug: str) -> list[Episode]:
        # Show landing page returns the current season's episodes (older seasons are
        # typically Peacock-only and don't appear in itemLabelsConfig here).
        page, secret = self._fetch_page(slug)
        episodes: list[Episode] = []
        for section in page.get("data", {}).get("sections") or []:
            if section.get("component") != "LinksSelectableGroup":
                continue
            section_data = section.get("data") or {}
            if section_data.get("optionalTitle") != "Episodes":
                continue
            for shelf in section_data.get("items") or []:
                for tile in (shelf.get("data") or {}).get("items") or []:
                    tile_data = tile.get("data") or {}
                    if tile_data.get("programmingType") != "Full Episode":
                        continue
                    episodes.append(self._episode_from_meta(tile_data, secret))
        if not episodes:
            raise ValueError(f"NBC: no episodes found for show {slug!r}")
        return episodes

    def _episode_from_meta(self, meta: dict, drm_proxy_secret: str) -> Episode:
        """Build an Episode from a VIDEO-page `metadata` dict or a VideoTile `data` dict.
        Both shapes expose the same field set (mpxGuid, mpxAccountId, season/episode
        numbers, secondaryTitle, seriesShortTitle, programmingType, duration, permalink).
        """
        return Episode(
            id_=meta["mpxGuid"],
            title=meta.get("seriesShortTitle") or self.show_slug,
            season=int(meta["seasonNumber"]) if meta.get("seasonNumber") else 0,
            number=int(meta["episodeNumber"]) if meta.get("episodeNumber") else 0,
            name=meta.get("secondaryTitle"),
            language="en-US",
            service=self.__class__,
            data={
                "mpxAccountId": meta["mpxAccountId"],
                "mpxGuid": meta["mpxGuid"],
                "programmingType": meta.get("programmingType", "Full Episode"),
                "duration": meta.get("duration"),
                "permalink": meta.get("permalink"),
                "drm_proxy_secret": drm_proxy_secret,
            },
        )

    def _fetch_page(self, url_path: str) -> tuple[dict, str]:
        """Fetch an nbc.com page and return (page_base, drm_proxy_secret).

        `page_base` is the same dict shape that friendship.nbc.com used to return
        as `data.page` (keys: metadata, analytics, data, ...).
        """
        url = f"https://www.nbc.com/{url_path.lstrip('/')}"
        r = self.session.get(url, headers=self.config["headers"])
        if r.status_code != 200:
            raise ConnectionError(f"NBC page {r.status_code}: {url}")
        preload = self._extract_preload(r.text)
        pages = preload.get("pages") or {}
        # The URL we requested is the key; sometimes the path is normalised with
        # a leading slash, sometimes without — match whichever key is present.
        page = next(iter(pages.values()), None)
        if not page or "base" not in page:
            raise ValueError(f"NBC page {url}: PRELOAD has no pages[*].base")
        oc_b64 = (preload.get("client") or {}).get("oc")
        if not oc_b64:
            raise ValueError(f"NBC page {url}: PRELOAD has no client.oc")
        secret = self._decrypt_oc(oc_b64)["coreVideo"]["drmProxySecret"]
        return page["base"], secret

    @staticmethod
    def _extract_preload(html: str) -> dict:
        """Locate `PRELOAD={...}` in the inline <script> and parse the JSON object.
        The PRELOAD assignment is the only statement in its <script> tag, so we just
        slice from `PRELOAD=` to the closing `</script>` and strip JS punctuation.
        """
        marker = "PRELOAD="
        start = html.find(marker + "{")
        if start < 0:
            raise ValueError("NBC: PRELOAD global not found in page HTML")
        start += len(marker)
        end = html.find("</script>", start)
        if end < 0:
            raise ValueError("NBC: unterminated <script> after PRELOAD")
        # JSON parser tolerates leading/trailing whitespace but not a trailing semicolon.
        return json.loads(html[start:end].strip().rstrip(";"))

    def _decrypt_oc(self, oc_b64: str) -> dict:
        """AES-GCM-decrypt the obfuscated-config blob from PRELOAD.client.oc."""
        raw = base64.b64decode(oc_b64)
        if len(raw) <= 12 + 32 + 4:
            raise ValueError(f"NBC: oc payload too small ({len(raw)} bytes)")
        iv, key, ct, ver_bytes = raw[:12], raw[12:44], raw[44:-4], raw[-4:]
        ver = int.from_bytes(ver_bytes, "big")
        if ver != _OC_COMPATIBILITY_VERSION:
            # Not fatal - payload still decrypts. Worth knowing about because the
            # underlying layout may have shifted; if downstream parsing then fails
            # this warning will be the first breadcrumb.
            self.log.warning(
                f"NBC: oc COMPATIBILITY_VERSION mismatch (got {ver}, expected {_OC_COMPATIBILITY_VERSION}); "
                f"obfuscated-config layout may have changed"
            )
        plaintext = AESGCM(key).decrypt(iv, ct, None)
        return json.loads(plaintext.decode("utf-8"))

    def _fetch_manifest_url(self, title: Episode) -> str:
        url = self.config["endpoints"]["lemonade_url"].format(
            account=title.data["mpxAccountId"],
            guid=title.data["mpxGuid"],
        )
        self.session.headers.update(self.config["headers"])
        r = self.session.get(
            url,
            params={
                "platform": "web",
                "browser": "other",
                "programmingType": title.data.get("programmingType", "Full Episode"),
            },
        )
        if r.status_code != 200:
            raise ConnectionError(f"NBC lemonade {r.status_code}: {r.text[:200]}")
        playback = r.json()
        manifest_url = playback.get("playbackUrl")
        if not manifest_url:
            raise ValueError(f"NBC lemonade returned no playbackUrl: {playback}")

        # Match the in-browser request which appends locale flags to unlock all
        # audio / subtitle / forced-narrative tracks in the DASH manifest.
        sep = "&" if "?" in manifest_url else "?"
        return f"{manifest_url}{sep}audio=all&subtitle=all&forcedNarrative=true"
