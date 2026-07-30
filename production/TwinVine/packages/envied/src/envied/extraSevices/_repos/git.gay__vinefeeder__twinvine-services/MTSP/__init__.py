from __future__ import annotations

from http.cookiejar import MozillaCookieJar
from typing import Any, Optional
import re

import click
from click import Context
from bs4 import BeautifulSoup

from envied.core.credential import Credential
from envied.core.service import Service
from envied.core.titles import Movie, Movies
from envied.core.tracks import Chapter, Tracks
from envied.core.manifests.dash import DASH


class MTSP(Service):
	TITLE_RE = r"^(?:https?://(?:www\.)?magentasport\.de/event/[^/]+)?/[0-9]+/(?P<video_id>[0-9]+)"

	@staticmethod
	@click.command(name="MTSP", short_help="https://magentasport.de", help=__doc__)
	@click.argument("title", type=str)
	@click.pass_context
	def cli(ctx: Context, **kwargs: Any) -> MTSP:
		return MTSP(ctx, **kwargs)

	def __init__(self, ctx: Context, title: str):
		self.title = title
		super().__init__(ctx)

	def authenticate(self, cookies: Optional[MozillaCookieJar] = None, credential: Optional[Credential] = None) -> None:
		cache = self.cache.get(f"session_{credential.sha1}")
		if cache and not cache.expired:
			self.session.cookies.update({
				"session": cache.data,
				"entitled": "1",
			})
			return

		self.log.info("No cached session cookie, logging in...")
		r = self.session.get(self.config["endpoints"]["login_form"])
		r.raise_for_status()

		tid, xsrf_name, xsrf_value = self.get_login_tid_xsrf(r.text)

		data = {
			"tid": tid,
			xsrf_name: xsrf_value,
			"pkc": "",
			"webauthn_supported": "false",
			"pw_usr": credential.username
		}
		r = self.session.post(self.config["endpoints"]["login_post"], data=data)
		r.raise_for_status()

		tid, xsrf_name, xsrf_value = self.get_login_tid_xsrf(r.text)

		data = {
			"tid": tid,
			xsrf_name: xsrf_value,
			"hidden_usr": credential.username,
			"pw_pwd": credential.password,
			"persist_session_displayed": "1",
			"persist_session": "on"
		}
		r = self.session.post(self.config["endpoints"]["login_post"], data=data)
		r.raise_for_status()

		session = self.session.cookies.get_dict().get('session')
		cache.set(session)

	def get_titles(self) -> Movies:
		video_id = re.match(self.TITLE_RE, self.title).group("video_id")
		r = self.session.get(self.config["endpoints"]["video_config"].format(video_id=video_id))
		config = r.json()

		return Movies([Movie(
			id_=video_id,
			service=self.__class__,
			name=config["title"],
			language="de",
			data=config,
		)])

	def get_tracks(self, title: Movie) -> Tracks:
		r = self.session.post(title.data['streamAccess'])
		access = r.json()
		tracks = DASH.from_url(access["data"]["stream"]["dash"]).to_tracks(title.language)
		return tracks

	def get_chapters(self, title: Movie) -> list[Chapter]:
		return [
		]

	def get_login_tid_xsrf(self, html):
		soup = BeautifulSoup(html, "html.parser")
		form = soup.find("form", id="login")
		xsrf = form.find("input", {"name": re.compile("^xsrf_")})
		tid = form.find("input", {"name": "tid"})
		return tid.get("value"), xsrf.get('name'), xsrf.get("value")
