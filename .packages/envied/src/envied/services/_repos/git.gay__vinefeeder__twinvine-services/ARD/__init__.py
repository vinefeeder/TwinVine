from __future__ import annotations

from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union
from functools import partial
from pathlib import Path
import sys
import re

import click
import webvtt
import requests
from click import Context
from bs4 import BeautifulSoup

from envied.core.credential import Credential
from envied.core.service import Service
from envied.core.titles import Movie, Movies, Episode, Series
from envied.core.tracks import Track, Chapter, Tracks, Video, Subtitle
from envied.core.manifests.hls import HLS
from envied.core.manifests.dash import DASH


class ARD(Service):
	"""
	Service code for ARD Mediathek (https://www.ardmediathek.de)

	\b
	Version: 1.0.0
	Author: lambda
	Authorization: None
	Robustness:
		Unencrypted: 2160p, AAC2.0
	"""

	GEOFENCE = ("de",)
	TITLE_RE = r"^(https://www\.ardmediathek\.de/(?P<item_type>serie|video)/.+/)(?P<item_id>[a-zA-Z0-9]{10,})(/[0-9]{1,3})?$"
	EPISODE_NAME_RE = r"^(Folge [0-9]+:)?(?P<name>[^\(]+) \(S(?P<season>[0-9]+)/E(?P<episode>[0-9]+)\)$"

	@staticmethod
	@click.command(name="ARD", short_help="https://www.ardmediathek.de", help=__doc__)
	@click.argument("title", type=str)
	@click.pass_context
	def cli(ctx: Context, **kwargs: Any) -> ARD:
		return ARD(ctx, **kwargs)

	def __init__(self, ctx: Context, title: str):
		self.title = title
		super().__init__(ctx)

	def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
		pass

	def get_titles(self) -> Union[Movies, Series]:
		match = re.match(self.TITLE_RE, self.title)
		if not match:
			return

		item_id = match.group("item_id")
		if match.group("item_type") == "video":
			return self.load_player(item_id)

		r = self.session.get(self.config["endpoints"]["grouping"].format(item_id=item_id))
		item = r.json()

		for widget in item["widgets"]:
			if widget["type"] == "gridlist" and widget.get("compilationType") == "itemsOfShow":
				episodes = Series()
				for teaser in widget["teasers"]:
					if teaser["coreAssetType"] != "EPISODE":
						continue

					if 'Hörfassung' in teaser['longTitle']:
						continue

					episodes += self.load_player(teaser["id"])
				return episodes

	def get_tracks(self, title: Union[Episode, Movie]) -> Tracks:
		if title.data["blockedByFsk"]:
			self.log.error(
				"This content is age-restricted and not currently available. "
				"Try again after 10pm German time")
			sys.exit(0)

		media_collection = title.data["mediaCollection"]["embedded"]
		tracks = Tracks()
		for stream_collection in media_collection["streams"]:
			if stream_collection["kind"] != "main":
				continue

			for stream in stream_collection["media"]:
				if stream["mimeType"] == "application/vnd.apple.mpegurl":
					tracks += Tracks(HLS.from_url(stream["url"]).to_tracks(stream["audios"][0]["languageCode"]))
					break

		# Fetch tracks from HBBTV endpoint to check for potential H.265/2160p DASH
		r = self.session.get(self.config["endpoints"]["hbbtv"].format(item_id=title.id))
		hbbtv = r.json()
		for stream in hbbtv["video"]["streams"]:
			for media in stream["media"]:
				if media["mimeType"] == "application/dash+xml" and media["audios"][0]["kind"] == "standard":
					tracks += Tracks(DASH.from_url(media["url"]).to_tracks(media["audios"][0]["languageCode"]))
					break

		# for stream in title.data["video"]["streams"]:
		# 	for media in stream["media"]:
		# 		if media["mimeType"] != "video/mp4" or media["audios"][0]["kind"] != "standard":
		# 			continue

		# 		tracks += Video(
		# 			codec=Video.Codec.AVC, # Should check media["videoCodec"]
		# 			range_=Video.Range.SDR, # Should check media["isHighDynamicRange"]
		# 			width=media["maxHResolutionPx"],
		# 			height=media["maxVResolutionPx"],
		# 			url=media["url"],
		# 			language=media["audios"][0]["languageCode"],
		# 			fps=50,
		# 		)

		for sub in media_collection["subtitles"]:
			for source in sub["sources"]:
				if source["kind"] == "ebutt":
					tracks.add(Subtitle(
						codec=Subtitle.Codec.TimedTextMarkupLang,
						language=sub["languageCode"],
						url=source["url"]
					))

		return tracks

	def get_chapters(self, title: Union[Episode, Movie]) -> list[Chapter]:
		return []

	def load_player(self, item_id):
		r = self.session.get(self.config["endpoints"]["item"].format(item_id=item_id))
		item = r.json()

		for widget in item["widgets"]:
			if widget["type"] != "player_ondemand":
				continue

			common_data = {
				"id_": item_id,
				"data": widget,
				"service": self.__class__,
				"language": "de",
				"year": widget["broadcastedOn"][0:4],
			}

			if widget["show"]["coreAssetType"] == "SINGLE" or not widget["show"].get("availableSeasons"):
				return Movies([Movie(
					name=widget["title"],
					**common_data
				)])
			else:
				match = re.match(self.EPISODE_NAME_RE, widget["title"])
				if not match:
					name = widget["title"]
					season = 0
					episode = 0
				else:
					name = match.group("name")
					season = match.group("season") or 0
					episode = match.group("episode") or 0

				return Series([Episode(
					name=name,
					title=widget["show"]["title"],
					#season=widget["show"]["availableSeasons"][0],
					season=season,
					number=episode,
					**common_data
				)])

