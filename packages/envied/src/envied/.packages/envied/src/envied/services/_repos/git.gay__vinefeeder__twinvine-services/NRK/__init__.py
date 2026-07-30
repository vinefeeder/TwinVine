from __future__ import annotations

from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union
from functools import partial
from pathlib import Path
import json
import re

import click
import isodate
from click import Context

from envied.core.credential import Credential
from envied.core.service import Service
from envied.core.titles import Movie, Movies, Episode, Series
from envied.core.tracks import Track, Chapter, Tracks, Video, Audio, Subtitle
from envied.core.manifests.hls import HLS
from envied.core.manifests.dash import DASH
from rich.console import Console


class NRK(Service):
	"""
	Service code for NRK TV (https://tv.nrk.no)

	\b
	Version: 1.0.0
	Author: lambda
	Authorization: None
	Robustness:
		Unencrypted: 1080p, DD5.1
	"""

	GEOFENCE = ("no",)
	TITLE_RE = r"^https://tv.nrk.no/serie/fengselseksperimentet/sesong/1/episode/(?P<content_id>.+)$"

	@staticmethod
	@click.command(name="NRK", short_help="https://tv.nrk.no", help=__doc__)
	@click.argument("title", type=str)
	@click.pass_context
	def cli(ctx: Context, **kwargs: Any) -> NRK:
		return NRK(ctx, **kwargs)

	def __init__(self, ctx: Context, title: str):
		self.title = title
		super().__init__(ctx)

	def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
		pass

	def get_titles(self) -> Union[Movies, Series]:
		match = re.match(self.TITLE_RE, self.title)
		if  match:
			content_id = match.group("content_id")
			EPISODE = True
			MOVIE = False
		else:
			content_id = self.title.split('/')[-1] 
			MOVIE = True
			EPISODE = False

		r = self.session.get(self.config["endpoints"]["content"].format(content_id=content_id))
		item = r.json()
		# development only
		#console = Console()
		#console.print_json(data=item)
		if EPISODE:
			episode, name = item["programInformation"]["titles"]["title"].split(". ", maxsplit=1)
			return Series([Episode(
				id_=content_id,
				service=self.__class__,
				language="nb",
				year=item["moreInformation"]["productionYear"],
				title=item["_links"]["seriesPage"]["title"],
				name=name,
				season=item["_links"]["season"]["name"],
				number=episode,
			)])
		if MOVIE:
			name = item["programInformation"]["titles"]["title"]
			return Movies([Movie(
				id_ = content_id,
				service=self.__class__, 
				name = name,

				year = item["moreInformation"]["productionYear"],
				language="nb",
				data = None,
				description = None,)])
		


	def get_tracks(self, title: Union[Episode, Movie]) -> Tracks:
		r = self.session.get(self.config["endpoints"]["manifest"].format(content_id=title.id))
		manifest = r.json()
		tracks = Tracks()

		for asset in manifest["playable"]["assets"]:
			if asset["format"] == "HLS":
				tracks += Tracks(HLS.from_url(asset["url"], session=self.session).to_tracks("nb"))


		for sub in manifest["playable"]["subtitles"]:
			tracks.add(Subtitle(
				codec=Subtitle.Codec.WebVTT,
				language=sub["language"],
				url=sub["webVtt"],
				sdh=sub["type"] == "ttv",
			))


		for track in tracks:
			track.needs_proxy = True

#		if isinstance(track, Audio) and track.channels == 6.0:
#			track.channels = 5.1

		return tracks

	def get_chapters(self, title: Union[Episode, Movie]) -> list[Chapter]:
		r = self.session.get(self.config["endpoints"]["metadata"].format(content_id=title.id))
		sdi = r.json()["skipDialogInfo"]

		chapters = []
		if sdi["endIntroInSeconds"]:
			if sdi["startIntroInSeconds"]:
				chapters.append(Chapter(timestamp=0))

			chapters |= [
				Chapter(timestamp=sdi["startIntroInSeconds"], name="Intro"),
				Chapter(timestamp=sdi["endIntroInSeconds"])
			]

		if sdi["startCreditsInSeconds"]:
			if not chapters:
				chapters.append(Chapter(timestamp=0))

			credits = isodate.parse_duration(sdi["startCredits"])
			chapters.append(Chapter(credits.total_seconds(), name="Credits"))

		return chapters
