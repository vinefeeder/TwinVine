from __future__ import annotations

from http.cookiejar import MozillaCookieJar
from typing import Any, Optional, Union
import sys
import re

import click
from click import Context

from envied.core.credential import Credential
from envied.core.service import Service
from envied.core.titles import Movie, Movies, Episode, Series
from envied.core.tracks import Track, Chapter, Tracks, Video, Subtitle


class ZDF(Service):
	"""
	Service code for ZDF.de (https://www.zdf.de)

	\b
	Version: 1.0.0
	Author: lambda
	Authorization: None
	Robustness:
		Unencrypted: 2160p HLG, AAC2.0
	"""

	GEOFENCE = ("de",)
	VIDEO_RE = r"^https://www\.zdf\.de/(play|video)/(?P<content_type>.+)/(?P<series_slug>.+)/(?P<item_slug>[^\?]+)(\?.+)?$"
	SERIES_RE = r"^https://www.zdf.de/serien/(?P<slug>[^\?]+)(\?.+)?$"
	VIDEO_CODEC_MAP = {
		"video/mp4": Video.Codec.AVC,
		"video/webm": Video.Codec.VP9
	}

	@staticmethod
	@click.command(name="ZDF", short_help="https://www.zdf.de", help=__doc__)
	@click.argument("title", type=str)
	@click.pass_context
	def cli(ctx: Context, **kwargs: Any) -> ZDF:
		return ZDF(ctx, **kwargs)

	def __init__(self, ctx: Context, title: str):
		self.title = title
		super().__init__(ctx)

	def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
		# This seems to be more or less static, but it's easy enough to fetch every time
		r = self.session.get("http://hbbtv.zdf.de/zdfm3/index.php")
		match = re.match(r'.+GLOBALS\.apikey += +"(?P<header>[^"\n]+).+";', r.text, re.DOTALL)
		self.session.headers.update({"Api-Auth": match.group('header')})

	def get_titles(self) -> Union[Movies, Series]:
		if match := re.match(self.SERIES_RE, self.title):
			return self.handle_series_page(match.group('slug'))

		if match := re.match(self.VIDEO_RE, self.title):
			r = self.session.post(self.config["endpoints"]["graphql"], json={
				"operationName": "VideoByCanonical",
				"query": self.config["queries"]["VideoByCanonical"],
				"variables": {"canonical": match.group('item_slug'), "first": 1},
			}, headers={"content-type": "application/json"})

			video = r.json()["data"]["videoByCanonical"]
			return self.parse_video_data(video)

	def get_tracks(self, title: Union[Episode, Movie]) -> Tracks:
		tracks = Tracks()
		for node in title.data["nodes"]:
			if node["vodMediaType"] != "DEFAULT":
				continue

			for player_type in self.config["meta"]["player_types"]:
				ptmd_url = (self.config["endpoints"]["ptmd_base"] +
					node["ptmdTemplate"].format(playerId=player_type))

				r = self.session.get(ptmd_url)
				ptmd = r.json()

				for pl in ptmd["priorityList"]:
					for media_format in pl["formitaeten"]:
						if "restriction_useragent" in media_format["facets"] or media_format["mimeType"] not in self.VIDEO_CODEC_MAP.keys():
							continue

						if 'hdr_hlg' in media_format["facets"]:
							video_range = Video.Range.HLG
							video_codec = Video.Codec.HEVC
						else:
							video_range = Video.Range.SDR
							video_codec = self.VIDEO_CODEC_MAP[media_format["mimeType"]]

						for quality in media_format["qualities"]:
							for track in quality["audio"]["tracks"]:
								if track["class"] not in ("main", "ot"):
									continue

								track_id = f'{video_codec}-{track["language"]}-{quality["highestVerticalResolution"]}'
								if tracks.exists(by_id=track_id):
									continue

								tracks.add(Video(
									id_=track_id,
									codec=video_codec,
									range_=video_range,
									width=quality["highestVerticalResolution"] // 9 * 16,
									height=quality["highestVerticalResolution"],
									url=track["uri"],
									language=track["language"],
									fps=50,
								))

				for subs in ptmd["captions"]:
					if subs["format"] == "ebu-tt-d-basic-de":
						track_id = f'subs-{subs["language"]}-{subs["class"]}'
						if tracks.exists(by_id=track_id):
							continue

						tracks.add(Subtitle(
							id_=track_id,
							codec=Subtitle.Codec.TimedTextMarkupLang,
							language=subs["language"],
							sdh=subs["class"] == "hoh",
							url=subs["uri"]
						))

		return tracks

	def get_chapters(self, title: Union[Episode, Movie]) -> list[Chapter]:
		for node in title.data["nodes"]:
			si = node.get("skipIntro")

			if si and node["vodMediaType"] == "DEFAULT":
				if si["startIntroTimeOffset"] and si["stopIntroTimeOffset"]:
					intro_start = float(si["startIntroTimeOffset"])
					intro_stop = float(si["stopIntroTimeOffset"])
					chapters = []

					if intro_start != 0:
						chapters.append(Chapter(timestamp=0))

					return chapters + [
						Chapter(timestamp=intro_start),
						Chapter(timestamp=intro_stop),
					]
				break
		return []

	def parse_video_data(self, video):
		common_data = {
			"id_": video["id"],
			"service": self.__class__,
			"year": video["editorialDate"][0:4],
			"data": video["currentMedia"],
		}

		meta = video["structuralMetadata"]
		if "publicationFormInfo" in meta and meta["publicationFormInfo"]["original"] == "Film":
			return Movies([Movie(
				name=video["title"],
				**common_data
			)])
		else:
			name = video["title"]
			series_title = video["smartCollection"].get("title", "DUMMY")

			# Ignore fake episode names like "Episode 123" or "Series Name (1/8)"
			if re.match(fr"^(Folge \d+|{series_title} \(\d+/\d+\))$", name):
				name = None

			return Series([Episode(
				**common_data,
				name=name,
				title=series_title,
				season=video["episodeInfo"]["seasonNumber"],
				number=video["episodeInfo"]["episodeNumber"],
			)])

	def handle_series_page(self, slug):
		extensions = {
			"persistedQuery": {
				"version": 1,
				"sha256Hash": "9412a0f4ac55dc37d46975d461ec64bfd14380d815df843a1492348f77b5c99a"
			}
		}

		variables = {
			"seasonIndex": 0,
			"episodesPageSize": 24,
			"canonical": slug,
			"sortBy": [
				{
					"field": "EDITORIAL_DATE",
					"direction": "ASC"
				}
			]
		}

		r = self.session.get(self.config["endpoints"]["graphql"], params={
			"extensions": json.dumps(extensions),
			"variables": json.dumps(variables)
		}, headers={"content-type": "application/json"})

		data = r.json()["data"]["smartCollectionByCanonical"]
		if not data:
			return

		series = Series()
		for season in data["seasons"]["nodes"]:
			for video in season["episodes"]["nodes"]:
				series += self.parse_video_data(video)

		return series
