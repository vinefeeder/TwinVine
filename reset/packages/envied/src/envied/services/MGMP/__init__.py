from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from http.cookiejar import CookieJar
from typing import Any, Generator, Optional, Union

import click
from langcodes import Language

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapters, Subtitle, Tracks
from envied.services.MGMP import queries


class MGMP(Service):
    """
    Service code for MGM+ (https://www.mgmplus.com).

    \b
    Version: 2.0.2
    Author: sp4rk.y
    Date: 2026-02-19
    Authorization: Device Login Pair
    Robustness:
        Widevine:
            L3: 2160p

    \b
    Tips:
        - Input can be a full MGM+ URL or a movie/series slug/ID
        - Uses device login pair flow — a code will be displayed for activation
        - Visit https://www.mgmplus.com/activate and enter the code shown
        - Session tokens are cached automatically for reuse
        - Series watch URLs (`/series/<slug>/watch/season/<n>/episode/<n>`) are supported

    \b
    Notes:
        - Authentication uses the EPIX/MGM+ device pairing API (api.epixnow.com)
        - Primary playback uses AndroidTV GraphQL PlayFlow for DASH+Widevine (4K)
        - Fallback uses web session + Amazon playback pipeline for non-DRM content
        - DRM: BuyDRM KeyOS (AndroidTV path) or Amazon Widevine (fallback path)
        - Supports 4K UHD HEVC with Dolby Digital Plus / Atmos audio
        - Subtitles are sourced from WebVTT URLs in the PlayFlow response
    """

    ALIASES = ("mgmplus",)
    GEOFENCE = ("us",)
    TITLE_RE = r"^(?:https?://(?:www\.)?mgmplus\.com/(?:movie|series)/)?(?P<title>[^/?#]+)"
    MOVIE_URL_RE = r"^https?://(?:www\.)?mgmplus\.com/movie/(?P<slug>[^/?#]+)"
    SERIES_URL_RE = r"^https?://(?:www\.)?mgmplus\.com/series/(?P<slug>[^/?#]+)"
    SERIES_WATCH_RE = r"^https?://(?:www\.)?mgmplus\.com/series/(?P<slug>[^/?#]+)/watch/season/(?P<season>\d+)/episode/(?P<episode>\d+)"

    def __init__(self, ctx, title: str):
        self.title = title.strip()
        super().__init__(ctx)

        guid_cache = self.cache.get("session_guid")
        if guid_cache and guid_cache.data:
            self.session_guid = str(guid_cache.data)
        else:
            self.session_guid = str(uuid.uuid4())
            guid_cache.set(self.session_guid)

        vendor_cache = self.cache.get("vendor_id")
        if vendor_cache and vendor_cache.data:
            self.vendor_id = str(vendor_cache.data)
        else:
            self.vendor_id = uuid.uuid4().hex[:16]
            vendor_cache.set(self.vendor_id)

        self.session_token = None
        self.web_session_token = None
        self.amazon_device_id = str(uuid.uuid4())
        self.session.headers.update(self.config.get("headers", {}))

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        cache = self.cache.get("session_token")
        session_data = self._create_device_session()
        device_session = session_data["device_session"]

        if device_session.get("user") is not None:
            self.log.info("Refreshed AndroidTV session token")
            self._cache_session_token(device_session["session_token"], cache)
            return

        self.log.info("Starting device login pair flow...")
        anon_token = device_session["session_token"]

        code_data = self._get_registration_code(anon_token)
        code = code_data["device"]["code"]

        self.log.info("Go to: https://www.mgmplus.com/activate")
        self.log.info(f"Enter code: {code}")
        self.log.info("Waiting for activation...")

        for _ in range(200):
            time.sleep(3)
            session_data = self._create_device_session()
            device_session = session_data["device_session"]

            if device_session.get("user") is not None:
                self.log.info("Device paired successfully!")
                self._cache_session_token(device_session["session_token"], cache)
                return

        raise EnvironmentError("Device pairing timed out. Please try again.")

    def _cache_session_token(self, token: str, cache: Any = None) -> None:
        """Store token, apply it to headers, and cache with correct TTL."""
        self.session_token = token
        self._apply_session_token()

        if cache is None:
            cache = self.cache.get("session_token")

        ttl = 86400
        try:
            payload = self._decode_jwt_payload(token)
            exp = payload.get("exp", 0)
            remaining = int(exp - time.time())
            if remaining > 60:
                ttl = remaining
        except Exception:
            pass
        cache.set(self.session_token, expiration=ttl - 60)

    def _apply_session_token(self) -> None:
        """Extract GUID from JWT. Token is passed per-request, not on session headers."""
        self.session.headers.pop("x-session-token", None)
        try:
            payload = self._decode_jwt_payload(self.session_token)
            guid = payload.get("guid")
            if guid:
                self.session_guid = guid
        except Exception:
            pass

    def _is_token_expired(self, token: str) -> bool:
        """Check if a JWT session token has expired or will expire within 60 seconds."""
        try:
            payload = self._decode_jwt_payload(token)
            return time.time() >= payload.get("exp", 0) - 60
        except Exception:
            return True

    def _ensure_token_fresh(self) -> None:
        """Silently refresh the session token if it is near expiry."""
        if self.session_token and not self._is_token_expired(self.session_token):
            return
        self._refresh_android_session()

    def _refresh_android_session(self) -> None:
        """Force-refresh the AndroidTV device session token."""
        session_data = self._create_device_session()
        device_session = session_data["device_session"]
        if device_session.get("user") is None:
            raise EnvironmentError("Device is no longer paired. Please re-authenticate.")
        self._cache_session_token(device_session["session_token"])

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        """Decode the payload section of a JWT without verification."""
        parts = token.split(".")
        if len(parts) < 2:
            raise ValueError("Invalid JWT")
        payload_b64 = parts[1] + ("=" * (-len(parts[1]) % 4))
        return json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))

    def _create_device_session(self) -> dict:
        """Create a device session via the epixnow API. Returns user data when paired."""
        device_info = dict(self.config["client"]["device"])
        device_info["guid"] = self.session_guid
        device_info["vendor_id"] = self.vendor_id

        response = self.session.post(
            url=self.config["endpoints"]["sessions"],
            json={
                "apikey": self.config["client"]["apikey"],
                "amazon_receipt": {"receipt_id": "", "user_id": ""},
                "device": device_info,
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()

    def _get_registration_code(self, session_token: str) -> dict:
        """Request a device registration code for display on TV."""
        response = self.session.post(
            url=self.config["endpoints"]["registration_code"],
            data=b"",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-session-token": session_token,
            },
        )
        response.raise_for_status()
        return response.json()

    def search(self) -> Generator[SearchResult, None, None]:
        self._ensure_token_fresh()
        r = self.session.get(
            url=f"{self.config['endpoints']['search']}/{self.title}",
            params={"page": 1, "per": 16, "requested_types": "movies,shows"},
            headers={"x-session-token": self.session_token},
        )
        r.raise_for_status()
        results = r.json()

        for item in (results.get("data") or {}).get("items") or []:
            content = item.get("content") or {}
            content_type = item.get("type", "")
            title = content.get("title", "Unknown")
            content_id = content.get("short_name") or str(content.get("id", ""))

            label = "movie" if content_type == "movie" else "series" if content_type == "series" else content_type
            year = content.get("years") or content.get("release_year")
            description = content.get("synopsis") or content.get("short_description") or ""
            if len(description) > 200:
                description = description[:197] + "..."

            yield SearchResult(
                id_=content_id,
                title=title,
                description=description,
                label=f"{label} ({year})" if year else label,
                url=f"https://www.mgmplus.com/{'movie' if content_type == 'movie' else 'series'}/{content_id}",
            )

    def get_titles(self) -> Titles_T:
        parsed = self._parse_title_input(self.title)
        preferred_kind = parsed.get("kind")
        title_id = parsed["id"]

        if preferred_kind != "series":
            movie_id = title_id
            title_match = re.match(self.TITLE_RE, title_id)
            if title_match:
                extracted = title_match.group("title")
                movie_id = (
                    base64.b64encode(extracted.encode("utf-8")).decode("utf-8")
                    if extracted.startswith("movie;")
                    else extracted
                )

            movie = self._graphql_movie(movie_id)
            if movie:
                return Movies(
                    [
                        Movie(
                            id_=movie["id"],
                            service=self.__class__,
                            name=movie["title"],
                            year=movie.get("releaseYear"),
                            language=Language.get("en"),
                            description=movie.get("synopsis"),
                            data={
                                "resource_id": movie["id"],
                                "short_name": movie.get("shortName"),
                                "duration": movie.get("duration"),
                            },
                        )
                    ]
                )
            if preferred_kind == "movie":
                raise ValueError(f"Unable to find movie by id/slug: {self.title}")

        series = self._graphql_series(title_id)
        if not series:
            raise ValueError(f"Unable to find MGM+ title by id/slug: {self.title}")

        seasons_nodes = (series.get("seasons") or {}).get("nodes") or []
        canonical_year = self._get_series_canonical_year(seasons_nodes)

        season_filter = parsed.get("season")
        episode_filter = parsed.get("episode")

        if season_filter is not None and episode_filter is not None and series.get("shortName"):
            direct_episode = self._graphql_episode(series["shortName"], season_filter, episode_filter)
            if direct_episode:
                return Series(
                    [
                        Episode(
                            id_=direct_episode["id"],
                            service=self.__class__,
                            title=series["title"],
                            season=int(direct_episode.get("seasonNumber") or season_filter),
                            number=int(direct_episode.get("number") or episode_filter),
                            name=direct_episode.get("shortTitle"),
                            year=canonical_year or direct_episode.get("releaseYear"),
                            language=Language.get("en"),
                            description=direct_episode.get("description"),
                            data={
                                "resource_id": direct_episode["id"],
                                "duration": direct_episode.get("duration"),
                                "series_id": series.get("id"),
                                "series_short_name": (direct_episode.get("series") or {}).get("shortName")
                                or series.get("shortName"),
                                "season_id": None,
                            },
                        )
                    ]
                )

        episodes = []

        if season_filter is None or (season_filter is not None and season_filter > 0):
            for season in seasons_nodes:
                season_id = season.get("id")
                if not season_id:
                    continue

                if season_filter is not None and int(season.get("number") or 0) != season_filter:
                    continue

                for episode in self._graphql_episodes(season_id):
                    ep_season = int(episode.get("seasonNumber") or season.get("number") or 0)
                    ep_number = int(episode.get("number") or 0)

                    if season_filter is not None and ep_season != season_filter:
                        continue
                    if episode_filter is not None and ep_number != episode_filter:
                        continue

                    episodes.append(
                        Episode(
                            id_=episode["id"],
                            service=self.__class__,
                            title=series["title"],
                            season=ep_season,
                            number=ep_number,
                            name=episode.get("shortTitle"),
                            year=canonical_year or episode.get("releaseYear"),
                            language=Language.get("en"),
                            description=episode.get("description"),
                            data={
                                "resource_id": episode["id"],
                                "duration": episode.get("duration"),
                                "series_id": series.get("id"),
                                "series_short_name": (episode.get("series") or {}).get("shortName")
                                or series.get("shortName"),
                                "season_id": season_id,
                            },
                        )
                    )

        if not episodes:
            if season_filter is not None and episode_filter is not None:
                raise ValueError(
                    f"No episodes found for S{season_filter:02}E{episode_filter:02} in series '{series.get('title')}'."
                )
            raise ValueError(f"No episodes found for series '{series.get('title')}'.")

        return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        # Force-refresh sessions before each title to avoid stale state between episodes
        self._refresh_android_session()
        self.web_session_token = None

        resource_id = title.data["resource_id"]
        play_flow = self._get_play_flow_content(resource_id)

        if play_flow.get("__typename") == "ShowNotice":
            notice_title = play_flow.get("title", "Unavailable")
            notice_desc = play_flow.get("description", "")
            msg = f"{notice_title}: {notice_desc}" if notice_desc else notice_title
            self.log.info(f"Not available for streaming: {msg}")
            return Tracks()

        all_streams = play_flow.get("streams") or []
        stream = self._select_best_stream(all_streams)

        # Try Amazon playback for audio (and video fallback)
        amazon_result = None
        try:
            amazon_result = self._get_amazon_playback(resource_id)
        except Exception as e:
            self.log.warning(f"Amazon playback request failed: {e}")

        if stream:
            # MGM CENC video from AndroidTV PlayFlow
            manifest_url = stream["playlistUrl"]
            tracks = DASH.from_url(url=manifest_url, session=self.session).to_tracks(language=title.language)

            widevine = stream.get("widevine") or {}
            for track in [*tracks.videos, *tracks.audio]:
                track.data.setdefault("mgmp", {}).update(
                    {
                        "license_url": widevine.get("licenseServerUrl"),
                        "auth_token": widevine.get("authenticationToken"),
                    }
                )

            # Use Amazon audio if it offers better bitrates than MGM CENC
            if amazon_result:
                mpd_url, playback_envelope, session_handoff_token, playback_id, web_play_flow, manifest = amazon_result
                try:
                    amzn_tracks = DASH.from_url(url=mpd_url, session=self.session).to_tracks(language=title.language)
                    mgm_best = max((float(a.bitrate or 0) for a in tracks.audio), default=0)
                    amzn_best = max((float(a.bitrate or 0) for a in amzn_tracks.audio), default=0)

                    if amzn_tracks.audio and amzn_best > mgm_best:
                        for audio in amzn_tracks.audio:
                            audio.data["amazon"] = {
                                "playback_envelope": playback_envelope,
                                "session_handoff_token": session_handoff_token,
                                "playback_id": playback_id,
                            }
                            if "dash" in audio.data:
                                audio_track_id = audio.data["dash"]["adaptation_set"].get("audioTrackId")
                                sub_type = audio.data["dash"]["adaptation_set"].get("audioTrackSubtype")
                                if audio_track_id is not None:
                                    audio.language = Language.get(audio_track_id.split("_")[0])
                                if sub_type is not None and "descriptive" in sub_type.lower():
                                    audio.descriptive = True
                        tracks.audio = list(amzn_tracks.audio)
                        self._add_amazon_subtitles(tracks, manifest)
                except Exception:
                    pass

            return tracks

        # No MGM CENC streams — full Amazon fallback
        self.log.info("No DRM streams from AndroidTV, trying Amazon via web session...")
        if amazon_result:
            mpd_url, playback_envelope, session_handoff_token, playback_id, web_play_flow, manifest = amazon_result
            tracks = DASH.from_url(url=mpd_url, session=self.session).to_tracks(language=title.language)
            for track in [*tracks.videos, *tracks.audio]:
                track.data["amazon"] = {
                    "playback_envelope": playback_envelope,
                    "session_handoff_token": session_handoff_token,
                    "playback_id": playback_id,
                }
            # Fix audio language from DASH audioTrackId
            for audio in tracks.audio:
                if "dash" in audio.data:
                    audio_track_id = audio.data["dash"]["adaptation_set"].get("audioTrackId")
                    sub_type = audio.data["dash"]["adaptation_set"].get("audioTrackSubtype")
                    if audio_track_id is not None:
                        audio.language = Language.get(audio_track_id.split("_")[0])
                    if sub_type is not None and "descriptive" in sub_type.lower():
                        audio.descriptive = True

            # Add Amazon timed text subtitles
            self._add_amazon_subtitles(tracks, manifest)

            # Fallback to PlayFlow VTT if no timed text subtitles
            if not tracks.subtitles:
                cc = web_play_flow.get("closedCaptions") or {}
                vtt_url = (cc.get("vtt") or {}).get("location")
                if vtt_url:
                    tracks.add(
                        Subtitle(
                            url=vtt_url,
                            codec=Subtitle.Codec.WebVTT,
                            language=title.language or Language.get("en"),
                            forced=False,
                        )
                    )
            return tracks

        try:
            web_pf = self._web_play_flow(resource_id)
            if web_pf and web_pf.get("__typename") == "ShowNotice":
                notice_title = web_pf.get("title", "Unavailable")
                notice_desc = web_pf.get("description", "")
                msg = f"{notice_title}: {notice_desc}" if notice_desc else notice_title
                self.log.info(f"Not available for streaming: {msg}")
                return Tracks()
        except Exception:
            pass

        available = [
            f"{s.get('packagingSystem')}/{s.get('encryptionScheme')} widevine={bool(s.get('widevine'))}"
            for s in all_streams
        ]
        raise ValueError(f"No DASH streams found (AndroidTV or Amazon). Available: {available or 'none'}")

    def _add_amazon_subtitles(self, tracks: Tracks, manifest: dict) -> None:
        """Extract timed text subtitles from Amazon playback manifest response."""
        timed_text = (manifest.get("timedTextUrls") or {}).get("result") or {}
        subtitle_urls = timed_text.get("subtitleUrls") or []
        forced_urls = timed_text.get("forcedNarrativeUrls") or []

        for sub in subtitle_urls + forced_urls:
            sub_type = sub.get("type", "").lower()
            is_forced = sub_type == "forcednarrative"
            lang_code = sub.get("languageCode", "en")
            name = Language.get(lang_code).display_name()
            url = sub.get("url", "")
            if not url:
                continue
            # Prefer SRT format
            url = os.path.splitext(url)[0] + ".srt"
            tracks.add(
                Subtitle(
                    id_=sub.get(
                        "timedTextTrackId",
                        sub.get("timedSubtitleId", f"{lang_code}_{sub.get('type', '')}_{sub.get('subtype', '')}"),
                    ),
                    url=url,
                    codec=Subtitle.Codec.from_codecs("srt"),
                    language=lang_code,
                    name=name,
                    forced=is_forced,
                    sdh=sub_type == "sdh",
                    cc=sub_type == "cc",
                ),
                warn_only=True,
            )

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        amazon_ctx = (track.data or {}).get("amazon")
        if amazon_ctx:
            return self._get_amazon_widevine_license(challenge, amazon_ctx)

        mgmp_ctx = (track.data or {}).get("mgmp") or {}
        license_url = mgmp_ctx.get("license_url")
        auth_token = mgmp_ctx.get("auth_token")

        if not license_url or not auth_token:
            raise ValueError("Track is missing license context (license_url or auth_token).")

        response = self.session.post(
            url=license_url,
            headers={
                "x-keyos-authorization": auth_token,
                "Content-Type": "application/octet-stream",
            },
            data=challenge,
        )
        response.raise_for_status()
        return response.content

    def get_widevine_service_certificate(self, **_: Any) -> Optional[str]:
        return None

    def _parse_title_input(self, value: str) -> dict:
        text = value.strip()

        watch_match = re.match(self.SERIES_WATCH_RE, text)
        if watch_match:
            return {
                "kind": "series",
                "id": watch_match.group("slug"),
                "season": int(watch_match.group("season")),
                "episode": int(watch_match.group("episode")),
            }

        movie_match = re.match(self.MOVIE_URL_RE, text)
        if movie_match:
            return {"kind": "movie", "id": movie_match.group("slug"), "season": None, "episode": None}

        series_match = re.match(self.SERIES_URL_RE, text)
        if series_match:
            return {"kind": "series", "id": series_match.group("slug"), "season": None, "episode": None}

        if text.startswith("movie;"):
            return {"kind": "movie", "id": text, "season": None, "episode": None}
        if text.startswith("series;"):
            return {"kind": "series", "id": text, "season": None, "episode": None}

        return {"kind": None, "id": text, "season": None, "episode": None}

    def _graphql(self, operation: str, variables: dict, query: str) -> dict:
        self._ensure_token_fresh()
        response = self.session.post(
            url=self.config["endpoints"]["graphql"],
            json={
                "operationName": operation,
                "variables": variables,
                "query": query,
                "extensions": {"clientLibrary": {"name": "apollo-kotlin", "version": "4.3.3"}},
            },
            headers={
                "Accept": "multipart/mixed;deferSpec=20220824, application/graphql-response+json, application/json",
                "Content-Type": "application/json",
                "x-session-token": self.session_token,
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise ValueError(f"GraphQL error for {operation}: {data['errors']}")
        return data

    def _graphql_movie(self, title_id: str) -> Optional[dict]:
        data = self._graphql("Movie", {"id": title_id}, queries.MOVIE)
        return (data.get("data") or {}).get("movie")

    def _graphql_series(self, series_id: str) -> Optional[dict]:
        data = self._graphql("Series", {"id": series_id}, queries.SERIES)
        return (data.get("data") or {}).get("series")

    def _graphql_episodes(self, season_id: str) -> list[dict]:
        episodes: list[dict] = []
        after = None
        while True:
            variables = {"seasonId": season_id, "first": 100, "after": after}
            data = self._graphql("Episodes", variables, queries.EPISODES)
            payload = (data.get("data") or {}).get("episodes") or {}
            episodes.extend(payload.get("nodes") or [])
            page_info = payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
        return episodes

    def _graphql_episode(self, series_short_name: str, season_number: int, episode_number: int) -> Optional[dict]:
        variables = {
            "seriesShortName": series_short_name,
            "seasonNumber": int(season_number),
            "episodeNumber": int(episode_number),
        }
        data = self._graphql("Episode", variables, queries.EPISODE)
        return (data.get("data") or {}).get("episode")

    def _get_series_canonical_year(self, seasons_nodes: list[dict]) -> Optional[int]:
        for season in sorted(seasons_nodes, key=lambda x: int((x or {}).get("number") or 10**9)):
            season_id = season.get("id")
            if not season_id:
                continue
            for episode in self._graphql_episodes(season_id):
                raw_year = episode.get("releaseYear")
                if isinstance(raw_year, int) and raw_year > 0:
                    return raw_year
                if isinstance(raw_year, str) and raw_year.isdigit() and int(raw_year) > 0:
                    return int(raw_year)
        return None

    def _graphql_play_flow(self, resource_id: str, context: Optional[str] = None) -> dict:
        """Call the Play GraphQL query matching the AndroidTV apollo-kotlin client."""
        variables = {
            "id": resource_id,
            "context": context or "",
            "behavior": "DEFAULT",
            "supportedActions": ["noop", "continue_play", "play_content", "show_notice", "log_in", "start_billing"],
        }
        data = self._graphql("Play", variables, queries.PLAY)
        return (data.get("data") or {}).get("playFlow")

    def _get_play_flow_content(self, resource_id: str) -> dict:
        """Resolve PlayFlow to actual content, skipping pre-rolls via continuation.

        Pre-rolls are PlayContent responses that lack DRM streams. We chase their
        continuationContext to reach the real content with DASH+Widevine streams.
        """
        play_flow = self._graphql_play_flow(resource_id)
        seen_contexts: set[str] = set()

        for attempt in range(6):
            if not play_flow:
                self.log.warning(f"PlayFlow attempt {attempt + 1}: empty response")
                break

            typename = play_flow.get("__typename", "")
            pf_type = play_flow.get("type", "")

            if typename == "PlayContent" or pf_type == "play_content":
                streams = play_flow.get("streams") or []
                if any(s.get("packagingSystem") == "DASH" and s.get("widevine") for s in streams):
                    return play_flow
                context = play_flow.get("continuationContext")
                if context and context not in seen_contexts:
                    seen_contexts.add(context)
                    play_flow = self._graphql_play_flow(resource_id, context=context)
                    continue
                return play_flow

            context = play_flow.get("continuationContext")
            if not context:
                for action in play_flow.get("actions") or []:
                    if isinstance(action, dict) and action.get("continuationContext"):
                        context = action["continuationContext"]
                        break
            if context and context not in seen_contexts:
                seen_contexts.add(context)
                play_flow = self._graphql_play_flow(resource_id, context=context)
                continue

            break

        if play_flow:
            return play_flow

        raise ValueError(f"PlayFlow did not return playable content for {resource_id}.")

    def _create_web_session(self) -> str:
        """Create a web session on api.mgmplus.com using the same paired GUID."""
        if self.web_session_token and not self._is_token_expired(self.web_session_token):
            return self.web_session_token

        web_config = self.config["web"]
        device_info = dict(web_config["device"])
        device_info["guid"] = self.session_guid

        response = self.session.post(
            url=web_config["endpoints"]["sessions"],
            json={
                "device": device_info,
                "apikey": web_config["apikey"],
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://www.mgmplus.com",
                "Referer": "https://www.mgmplus.com/",
            },
        )
        response.raise_for_status()
        data = response.json()
        self.web_session_token = data["device_session"]["session_token"]
        return self.web_session_token

    def _web_play_flow(self, resource_id: str) -> Optional[dict]:
        """Call PlayFlow via the web GraphQL endpoint to get amazonPlayback data."""
        web_token = self._create_web_session()
        web_config = self.config["web"]

        response = self.session.post(
            url=web_config["endpoints"]["graphql"],
            json={
                "operationName": "PlayFlow",
                "variables": {
                    "id": resource_id,
                    "supportedActions": [
                        "open_url",
                        "show_notice",
                        "start_billing",
                        "play_content",
                        "log_in",
                        "noop",
                        "confirm_provider",
                        "unlinked_provider",
                    ],
                    "streamTypes": [
                        {"encryptionScheme": "CBCS", "packagingSystem": "DASH"},
                        {"encryptionScheme": "CENC", "packagingSystem": "DASH"},
                        {"encryptionScheme": "NONE", "packagingSystem": "HLS"},
                        {"encryptionScheme": "AES_128", "packagingSystem": "HLS"},
                        {"encryptionScheme": "SAMPLE_AES", "packagingSystem": "HLS"},
                    ],
                },
                "query": queries.WEB_PLAY,
            },
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "x-session-token": web_token,
                "Origin": "https://www.mgmplus.com",
                "Referer": "https://www.mgmplus.com/",
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            self.log.warning(f"Web PlayFlow GraphQL errors: {data['errors']}")
            return None
        return (data.get("data") or {}).get("playFlow")

    def _get_amazon_playback(self, resource_id: str) -> Optional[tuple[str, str, str, str, dict, dict]]:
        """Get Amazon DASH playback via web PlayFlow + GetVodPlaybackResources.

        Uses WebPlayer profile (compatible with unauthenticated MGM web session)
        with upgraded codec/resolution/bitrate parameters for better quality.
        Returns (mpd_url, playback_envelope, session_handoff_token, playback_id, web_play_flow, manifest) or None.
        """
        play_flow = self._web_play_flow(resource_id)
        if not play_flow:
            return None

        amazon = play_flow.get("amazonPlayback") or {}
        playback_envelope = amazon.get("playbackEnvelope")
        playback_id = amazon.get("playbackId")
        if not playback_envelope or not playback_id:
            self.log.info("Web PlayFlow returned no amazonPlayback data")
            return None

        amazon_config = self.config["amazon"]

        response = self.session.post(
            url=f"{amazon_config['base']}/playback/prs/GetVodPlaybackResources",
            params={
                "deviceID": self.amazon_device_id,
                "deviceTypeID": amazon_config["device_type"],
                "gascEnabled": "false",
                "marketplaceID": amazon_config["marketplace"],
                "uxLocale": "en_US",
                "firmware": 1,
                "titleId": playback_id,
                "nerid": self._generate_nerid(0),
            },
            headers={
                "Accept": "*/*",
                "Content-Type": "text/plain",
                "x-atv-client-type": "XpPlayer",
                "Origin": "https://www.mgmplus.com",
                "Referer": "https://www.mgmplus.com/",
            },
            json={
                "globalParameters": {
                    "deviceCapabilityFamily": "WebPlayer",
                    "playbackEnvelope": playback_envelope,
                    "capabilityDiscriminators": {
                        "operatingSystem": {"name": "Windows", "version": "10.0"},
                        "middleware": {"name": "Chrome", "version": "145.0.0.0"},
                        "nativeApplication": {"name": "Chrome", "version": "145.0.0.0"},
                        "hfrControlMode": "Legacy",
                        "displayResolution": {"height": 2160, "width": 3840},
                    },
                },
                "auditPingsRequest": {},
                "playbackDataRequest": {},
                "timedTextUrlsRequest": {"supportedTimedTextFormats": ["TTMLv2", "DFXP"]},
                "trickplayUrlsRequest": {},
                "transitionTimecodesRequest": {},
                "vodPlaybackUrlsRequest": {
                    "device": {
                        "hdcpLevel": "2.2",
                        "maxVideoResolution": "2160p",
                        "supportedStreamingTechnologies": ["DASH"],
                        "streamingTechnologies": {
                            "DASH": {
                                "bitrateAdaptations": ["CVBR", "CBR"],
                                "codecs": ["H265", "H264"],
                                "drmKeyScheme": "DualKey",
                                "drmType": "Widevine",
                                "dynamicRangeFormats": ["None"],
                                "edgeDeliveryAuthorizationSchemes": ["PVExchangeV1", "Transparent"],
                                "fragmentRepresentations": ["ByteOffsetRange", "SeparateFile"],
                                "frameRates": ["Standard", "High"],
                                "stitchType": "MultiPeriod",
                                "segmentInfoType": "Base",
                                "timedTextRepresentations": [
                                    "NotInManifestNorStream",
                                    "SeparateStreamInManifest",
                                ],
                                "trickplayRepresentations": ["NotInManifestNorStream"],
                                "variableAspectRatio": "supported",
                            }
                        },
                        "displayWidth": 3840,
                        "displayHeight": 2160,
                    },
                    "playbackCustomizations": {},
                    "playbackSettingsRequest": {
                        "firmware": "UNKNOWN",
                        "playerType": "xp",
                        "responseFormatVersion": "1.0.0",
                        "titleId": playback_id,
                    },
                },
                "vodXrayMetadataRequest": {
                    "xrayDeviceClass": "normal",
                    "xrayPlaybackMode": "playback",
                    "xrayToken": "XRAY_WEB_2023_V2",
                },
            },
        )
        response.raise_for_status()
        result = response.json()

        mpd_url = self._extract_amazon_mpd_url(result)
        if not mpd_url:
            self.log.warning("Amazon GetVodPlaybackResources returned no MPD URL")
            return None

        session_handoff_token = (result.get("sessionization") or {}).get("sessionHandoffToken", "")
        if not session_handoff_token:
            self.log.warning("Amazon response missing sessionHandoffToken")
            return None

        return mpd_url, playback_envelope, session_handoff_token, playback_id, play_flow, result

    @staticmethod
    def _extract_amazon_mpd_url(result: dict) -> Optional[str]:
        """Extract the best DASH MPD URL from Amazon GetVodPlaybackResources response.

        Handles the playlisted format (vodPlaylistedPlaybackUrls) with CDN preference,
        falling back to the legacy format (vodPlaybackUrls) if needed.
        """
        cdn_preference = ["akamai", "cloudfront"]

        # Playlisted format (vodPlaylistedPlaybackUrlsRequest response)
        playlisted = (result.get("vodPlaylistedPlaybackUrls") or {}).get("result") or {}
        playback_urls = playlisted.get("playbackUrls") or {}
        playlist_items = playback_urls.get("intraTitlePlaylist") or []

        if playlist_items:
            main_item = next((p for p in playlist_items if p.get("type") == "Main"), None)
            if not main_item and playlist_items:
                main_item = playlist_items[0]
            if main_item:
                urls = main_item.get("urls") or []
                # Try CDN preference order
                for preferred in cdn_preference:
                    for u in urls:
                        if isinstance(u, dict) and u.get("cdn", "").lower() == preferred and u.get("url"):
                            return MGMP._clean_mpd_url(u["url"])
                # Fallback to first available URL
                for u in urls:
                    if isinstance(u, dict) and u.get("url"):
                        return MGMP._clean_mpd_url(u["url"])

        # Legacy format (vodPlaybackUrlsRequest response)
        vod_urls = (result.get("vodPlaybackUrls") or {}).get("result") or {}
        legacy_urls = vod_urls.get("playbackUrls") or {}
        url_sets = legacy_urls.get("urlSets") or []

        if isinstance(url_sets, list):
            for url_set in url_sets:
                if isinstance(url_set, dict) and url_set.get("url"):
                    return MGMP._clean_mpd_url(url_set["url"])

        if isinstance(url_sets, dict):
            for url_set in url_sets.values():
                if isinstance(url_set, dict):
                    if url_set.get("url"):
                        return MGMP._clean_mpd_url(url_set["url"])
                    for cdn_info in (url_set.get("urls") or {}).values():
                        if isinstance(cdn_info, dict) and cdn_info.get("url"):
                            return MGMP._clean_mpd_url(cdn_info["url"])

        return None

    @staticmethod
    def _clean_mpd_url(mpd_url: str) -> str:
        """Clean up an Amazon MPD manifest URL by stripping custom/dm segments."""
        from urllib.parse import urlparse, urlunparse

        if "custom=true" in mpd_url:
            return mpd_url
        mpd_url = re.sub(r".@[^/]+/|custom=true&", "", mpd_url)
        try:
            parsed_url = urlparse(mpd_url)
            new_path = "/".join(
                segment for segment in parsed_url.path.split("/") if not any(sub in segment for sub in ["$", "dm"])
            )
            return urlunparse(parsed_url._replace(path=new_path))
        except Exception:
            return mpd_url

    def _get_amazon_widevine_license(self, challenge: bytes, amazon_ctx: dict) -> bytes:
        """Get a Widevine license from Amazon's DRM endpoint."""
        amazon_config = self.config["amazon"]
        playback_id = amazon_ctx["playback_id"]

        response = self.session.post(
            url=f"{amazon_config['base']}/playback/drm-vod/GetWidevineLicense",
            params={
                "deviceID": self.amazon_device_id,
                "deviceTypeID": amazon_config["device_type"],
                "gascEnabled": "false",
                "marketplaceID": amazon_config["marketplace"],
                "uxLocale": "en_US",
                "firmware": 1,
                "titleId": playback_id,
                "nerid": self._generate_nerid(0),
            },
            headers={
                "Accept": "*/*",
                "Content-Type": "text/plain",
                "x-atv-client-type": "XpPlayer",
                "Origin": "https://www.mgmplus.com",
                "Referer": "https://www.mgmplus.com/",
            },
            json={
                "includeHdcpTestKey": True,
                "licenseChallenge": base64.b64encode(challenge).decode(),
                "playbackEnvelope": amazon_ctx["playback_envelope"],
                "sessionHandoffToken": amazon_ctx["session_handoff_token"],
            },
        )
        response.raise_for_status()
        result = response.json()
        license_b64 = (result.get("widevineLicense") or {}).get("license")
        if not license_b64:
            raise ValueError(f"Amazon returned no Widevine license: {result}")
        return base64.b64decode(license_b64)

    @staticmethod
    def _generate_nerid(e: int) -> str:
        """Generate a network edge request ID (Amazon format)."""
        base64_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        timestamp = int(time.time() * 1000)
        epoch_chars = []
        for _ in range(7):
            epoch_chars.append(base64_chars[timestamp % 64])
            timestamp //= 64
        base64_epoch = "".join(reversed(epoch_chars))
        random_bytes = os.urandom(15)
        random_chars = [base64_chars[b % 64] for b in random_bytes]
        random_part = "".join(random_chars)
        suffix = f"{e % 100:02d}"
        return base64_epoch + random_part + suffix

    def _select_best_stream(self, streams: list[dict]) -> Optional[dict]:
        """Select the highest resolution DASH stream with Widevine (any encryption scheme)."""
        dash_wv = [
            s for s in streams if s.get("packagingSystem") == "DASH" and s.get("playlistUrl") and s.get("widevine")
        ]
        if not dash_wv:
            return None
        return max(dash_wv, key=lambda s: (s.get("videoQuality") or {}).get("height", 0))

    @staticmethod
    @click.command(name="MGMP", short_help="https://www.mgmplus.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return MGMP(ctx, **kwargs)


__all__ = ("MGMP",)
