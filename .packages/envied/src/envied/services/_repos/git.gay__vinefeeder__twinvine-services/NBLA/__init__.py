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
from envied.core.tracks import Track, Chapter, Tracks, Subtitle
from envied.core.manifests.hls import HLS


class NebulaSubtitle(Subtitle):
	STYLE_RE = re.compile('::cue\\(v\\[voice="(.+)"\\]\\) { color: ([^;]+); (.*)}')
	RGB_RE = re.compile("rgb\\((.+), ?(.+), ?(.+)\\)")

	def download(
		self,
		session: requests.Session,
		prepare_drm: partial,
		max_workers: Optional[int] = None,
		progress: Optional[partial] = None
	):
		# Track.download chooses file extension based on class name so use
		# this hack to keep it happy
		self.__class__.__name__ = "Subtitle"

		# Skip Subtitle.download and use Track.download directly. The pycaption
		# calls in Subtitle.download are not needed here and mangle the WebVTT
		# styling Nebula uses
		Track.download(self, session, prepare_drm, max_workers, progress)

	def convert(self, codec: Subtitle.Codec) -> Path:
		if codec != Subtitle.Codec.SubRip:
			return super().convert(codec)

		output_path = self.path.with_suffix(f".{codec.value.lower()}")
		vtt = webvtt.read(self.path)

		styles = dict()
		for group in vtt.styles:
			for style in group.text.splitlines():
				if match := self.STYLE_RE.match(style):
					name, color, extra = match.groups()

					if "rgb" in color:
						r, g, b = self.RGB_RE.match(color).groups()
						color = "#{0:02x}{1:02x}{2:02x}".format(int(r), int(g), int(b))

					bold = "bold" in extra
					styles[name.lower()] = {"color": color, "bold": bold}

		count = 1
		new_subs = []
		for caption in vtt:
			soup = BeautifulSoup(caption.raw_text, features="html.parser")

			for tag in soup.find_all("v"):
				name = " ".join(tag.attrs.keys())

				# Work around a few broken "Abolish Everything" subtitles
				if ((name == "spectator" and "spectator" not in styles) or
					(name == "spectators" and "spectators" not in styles)):
					name = "audience"

				style = styles[name]
				tag.name = "font"
				tag.attrs = {"color": style["color"]}

				if style["bold"]:
					tag.wrap(soup.new_tag("b"))

			text = str(soup)
			new_subs.append(f"{count}")
			new_subs.append(f"{caption.start} --> {caption.end}")
			new_subs.append(f"{text}\n")
			count += 1

		output_path.write_text("\n".join(new_subs), encoding="utf8")

		self.path = output_path
		self.codec = codec

		if callable(self.OnConverted):
			self.OnConverted(codec)

		return output_path


class NBLA(Service):
	"""
	Service code for Nebula (https://nebula.tv)

	\b
	Version: 1.0.0
	Author: lambda
	Authorization: Credentials
	Robustness:
		Unencrypted: 2160p, AAC2.0
	"""

	VIDEO_RE = r"https?://(?:www\.)?nebula\.tv/videos/(?P<slug>.+)"
	CHANNEL_RE = r"^https?://(?:www\.)?nebula\.tv/(?P<slug>.+)"

	@staticmethod
	@click.command(name="NBLA", short_help="https://nebula.tv", help=__doc__)
	@click.argument("title", type=str)
	@click.pass_context
	def cli(ctx: Context, **kwargs: Any) -> NBLA:
		return NBLA(ctx, **kwargs)

	def __init__(self, ctx: Context, title: str):
		self.title = title
		super().__init__(ctx)

	def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
		cache = self.cache.get(f"key_{credential.sha1}")
		if not cache or cache.expired:
			self.log.info("Key is missing or expired, logging in...")

			data = {
				"email": credential.username,
				"password": credential.password,
			}
			r = self.session.post(self.config["endpoints"]["login"], json=data)
			r.raise_for_status()

			key = r.json().get("key")
			cache.set(key)
		else:
			key = cache.data

		r = self.session.post(self.config["endpoints"]["authorization"], headers={"Authorization": f"Token {key}"})
		r.raise_for_status()

		self.jwt = r.json()["token"]
		self.session.headers.update({"Authorization": f"Bearer {self.jwt}"})

	def get_titles(self) -> Union[Movies, Series]:
		if video_match := re.match(self.VIDEO_RE, self.title):
			r = self.session.get(self.config["endpoints"]["video"].format(slug=video_match.group("slug")))
			video = r.json()

			# Simplest scenario: This is a video on a non-episodic channel, return it as movie
			if video["channel_type"] != "episodic":
				return Movies([
					Movie(
						id_=video["id"],
						service=self.__class__,
						name=video["title"],
						year=video["published_at"][0:4],
						language="en"
					)
				])

			# For episodic videos, things are trickier: There is no way to get the season
			# and episode number from the video endpoint, so we instead have to iterate
			# through all seasons and filter for the video id.
			return self.get_content(video["channel_slug"], video_id_filter=video["id"])

		# If the link did not match the video regex, try using it as slug for the content
		# API to fetch a whole channel/season
		elif channel_match := re.match(self.CHANNEL_RE, self.title):
			return self.get_content(channel_match.group("slug"))

	def get_tracks(self, title: Union[Episode, Movie]) -> Tracks:
		r = self.session.get(self.config["endpoints"]["manifest"].format(video_id=title.id, jwt=self.jwt), allow_redirects=False)
		manifest_url = r.headers["Location"]
		tracks = HLS.from_url(manifest_url).to_tracks(title.language)

		subs = []
		for subtitle in tracks.subtitles:
			subs.append(NebulaSubtitle(
				id_=subtitle.id,
				url=subtitle.url,
				language=subtitle.language,
				is_original_lang=subtitle.is_original_lang,
				descriptor=subtitle.descriptor,
				name=subtitle.name,
				codec=subtitle.codec,
				forced=subtitle.forced,
				sdh=subtitle.sdh,
			))

		tracks.subtitles = subs
		return tracks

	def get_chapters(self, title: Union[Episode, Movie]) -> list[Chapter]:
		return []


	def search(self) -> Generator[SearchResult, None, None]:
		pass
		#self.title
		r = self.session.get(self.config["endpoints"]["search"], params=params)
		r.raise_for_status()

#            for result in results["results"]:
#                yield SearchResult(
#                    id_=result["brand"].get("websafeTitle"),
#                    title=result["brand"].get("title"),
#                    description=result["brand"].get("description"),
#                    label=result.get("label"),
#                    url=result["brand"].get("href"),
#                )

	### Service specific functions
	def season_to_episodes(self, channel, season, video_id_filter):
		try:
			season_number = int(season["label"])
		except ValueError:
			# Some shows such have some non-integer season numbers (Such as
			# Jet Lag: The Game season 13.5). These are generally listed as specials
			# (Season 0) on TMDB, so treat them the same way.
			#
			# Specials episode numbers will then likely be off, use caution and
			# check TMDB for manual corrections.
			season_number = 0
			self.log.warn(f"Could not extract season information, guessing season {season_number}")

		for episode_number, episode in enumerate(season["episodes"], start=1):
			if not episode["video"] or (video_id_filter and video_id_filter != episode["video"]["id"]):
				continue

			yield Episode(
				id_=episode["video"]["id"],
				service=self.__class__,
				title=channel["title"],
				name=episode["title"],
				language="en",
				year=episode["video"]["published_at"][0:4],
				season=season_number,
				number=episode_number,
			)



	def get_content(self, slug, video_id_filter=None):
		r = self.session.get(self.config["endpoints"]["content"].format(slug=slug))
		content = r.json()

		if content["type"] == "season":
			r = self.session.get(self.config["endpoints"]["content"].format(slug=content["video_channel_slug"]))
			channel = r.json()
			return Series(self.season_to_episodes(channel, content, video_id_filter))
		elif content["type"] == "video_channel" and content["channel_type"] == "episodic":
			episodes = []
			for season_data in content["episodic"]["seasons"]:
				# We could also use the generic content endpoint to retrieve
				# seasons, but this is how the nebula web app does it.
				r = self.session.get(self.config["endpoints"]["season"].format(id=season_data["id"]))
				episodes.extend(self.season_to_episodes(content, r.json(), video_id_filter))

			return Series(episodes)
		elif content["type"] == "video_channel":
			self.log.error("Non-episodic channel URL passed. Treating it as a show with a single season. If you want to download non-episodic content as a movie, pass the direct video URL instead.")
			r = self.session.get(self.config["endpoints"]["video_channel_episodes"].format(id=content["id"]))
			episodes = r.json()['results']

			# Non-episodic channel names tend to have a format of "Creator Name — Show Name"
			if " — " in content["title"]:
				show_title = content["title"].split(" — ", maxsplit=1)[1]
			else:
				show_title = content["title"]

			season = []
			episode_number = 0
			for episode in episodes:
				if 'trailer' in episode['title'].lower():
					continue

				episode_number += 1
				season.append(Episode(
					id_=episode["id"],
					service=self.__class__,
					title=show_title,
					name=episode["title"],
					language="en",
					year=episode["published_at"][0:4],
					season=1,
					number=episode_number,
				))

			return Series(season)
		else:
			self.log.error("Unsupported content type")
			sys.exit(1)
