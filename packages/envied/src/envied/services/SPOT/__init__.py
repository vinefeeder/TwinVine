import base64
import re
from http.cookiejar import CookieJar
from typing import Optional, Union
import click
import requests
from envied.core.service import Service
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.titles import Song, Album, Title_T
from envied.core.tracks import Audio, Tracks
from envied.core.drm import DRM_T, Widevine
from envied.utils import base62
from pywidevine.pssh import PSSH

class SPOT(Service):
    """
    Service code for Spotify
    Written by ToonsHub

    Reference: https://github.com/glomatico/spotify-aac-downloader

    Authorization: Cookies (Free - 128kbps and Premium - 256kbps)
    Security: AAC@L3
    """

    # Static method, this method belongs to the class
    @staticmethod

    # The command name, must much the service tag (and by extension the service folder)
    @click.command(name="SPOT", short_help="https://open.spotify.com", help=__doc__)

    # Using track/playlist/album/artist page URL
    @click.argument("title", type=str)

    # Pass the context back to the CLI with arguments
    @click.pass_context
    def cli(ctx, **kwargs):
        return SPOT(ctx, **kwargs)

    # Accept the CLI arguments by overriding the constructor (The __init__() method)
    def __init__(self, ctx, title):

        # Pass the title argument to self so it's accessable across all methods
        self.title = title
        self.is_premium = False
       
        super().__init__(ctx)


    # Defining an authinticate function
    def authenticate(self, cookies: Optional[CookieJar], credential: Optional[Credential] = None):

        super().authenticate(cookies, credential)

        # Check for cookies
        if not cookies:
            raise Exception("Cookies are required for performing this action.")

        # Authenticate using Cookies
        self.session.headers.update(
            {
                'accept': 'application/json',
                'accept-language': 'en',
                "app-platform": "WebPlayer",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
            }
        )
        self.session.cookies.update(cookies)
        home_page = self.session.get("https://open.spotify.com/").content
        token = re.search(r'accessToken":"(.*?)"', home_page).group(1)
        self.is_premium = re.search(r'isPremium":(.*?),', home_page).group(1) == 'true'
        self.session.headers.update(
            {
                "authorization": f"Bearer {token}",
            }
        )

    # Function to determine the type of collection
    def getCollectionTypeAndId(self):

        _type = self.title.split("open.spotify.com/")[1].split("/")[0]
        _id = self.title.split(_type + "/")[1].split("?")[0]
        return _type, _id

    # Defining a function to return titles
    def get_titles(self):

        songs = []
        _type, _id = self.getCollectionTypeAndId()
            
        if _type == 'album':
            album = self.session.get(self.config['endpoints']['albums'].format(id=_id)).json()
            album_next_url = album["tracks"]["next"]
            while album_next_url is not None:
                album_next = self.session.get(album_next_url).json()
                album["tracks"]["items"].extend(album_next["items"])
                album_next_url = album_next["next"]

            # Get the episode metadata by iterating through each season id
            for song in album["tracks"]["items"]:

                # Set a class for each song
                song_class = Song(
                    id_=song["id"], 
                    name=song["name"], 
                    artist=", ".join([ artist["name"] for artist in song["artists"] ]), 
                    album=album["name"], 
                    track=song["track_number"], 
                    disc=song["disc_number"], 
                    year=int(album["release_date"][:4].strip()),
                    service=self.__class__
                )

                # Append it to the list
                songs.append(song_class)
        
        elif _type == "playlist":
            playlist = self.session.get(
                self.config['endpoints']['playlists'].format(id=_id)
            ).json()
            playlist_next_url = playlist["tracks"]["next"]
            while playlist_next_url is not None:
                playlist_next = self.session.get(playlist_next_url).json()
                playlist["tracks"]["items"].extend(playlist_next["items"])
                playlist_next_url = playlist_next["next"]

            # Get the episode metadata by iterating through each season id
            for song in playlist["tracks"]["items"]:

                song = song["track"]
                # Set a class for each song
                song_class = Song(
                    id_=song["id"], 
                    name=song["name"], 
                    artist=", ".join([ artist["name"] for artist in song["artists"] ]), 
                    album=song["album"]["name"], 
                    track=song["track_number"], 
                    disc=song["disc_number"], 
                    year=int(song["album"]["release_date"][:4].strip()),
                    service=self.__class__
                )

                # Append it to the list
                songs.append(song_class)
        
        elif _type == "artist":
            playlist = self.session.get(
                self.config['endpoints']['artists'].format(id=_id)
            ).json()

            # Get the episode metadata by iterating through each season id
            for song in playlist["tracks"]:

                # Set a class for each song
                song_class = Song(
                    id_=song["id"], 
                    name=song["name"], 
                    artist=", ".join([ artist["name"] for artist in song["artists"] ]), 
                    album=song["album"]["name"], 
                    track=song["track_number"], 
                    disc=song["disc_number"], 
                    year=int(song["album"]["release_date"][:4].strip()),
                    service=self.__class__
                )

                # Append it to the list
                songs.append(song_class)
        
        elif _type == "track":
            song = self.session.get(
                self.config['endpoints']['tracks'].format(id=_id)
            ).json()

            # Set a class for each song
            song_class = Song(
                id_=song["id"], 
                name=song["name"], 
                artist=", ".join([ artist["name"] for artist in song["artists"] ]), 
                album=song["album"]["name"], 
                track=song["track_number"], 
                disc=song["disc_number"], 
                year=int(song["album"]["release_date"][:4].strip()),
                service=self.__class__
            )

            # Append it to the list
            songs.append(song_class)
        
        return Album(songs)
    
    # Get DRM
    def get_spotify_drm(self) -> DRM_T:
        pssh = requests.get(
            self.config['endpoints']['pssh'].format(file_id=self.file_id)
        ).json()["pssh"]
        return Widevine(
                pssh=PSSH(pssh)
            )

    # Defining a function to get tracks
    def get_tracks(self, title: Title_T) -> Tracks:

        self.audio_quality = "MP4_256_DUAL" if self.is_premium else "MP4_128_DUAL"

        # Get FileID
        gid = hex(base62.decode(title.id, base62.CHARSET_INVERTED))[2:].zfill(32)
        metadata = self.session.get(
            self.config['endpoints']['metadata'].format(gid=gid)
        ).json()
        audio_files = metadata.get("file")
        if audio_files is None:
            if metadata.get("alternative") is not None:
                audio_files = metadata["alternative"][0]["file"]
            else:
                return None
        self.file_id = next(
            i["file_id"] for i in audio_files if i["format"] == self.audio_quality
        )

        # Get stream URL
        stream_url = self.session.get(
            self.config['endpoints']['stream'].format(file_id=self.file_id)
        ).json()["cdnurl"][0] # Can change index to get different server

        # Get & Set DRM
        drm = [self.get_spotify_drm()]

        # Set the tracks
        tracks = Tracks()
        tracks.add(Audio(
            url=stream_url,
            drm=drm,
            codec=Audio.Codec.AAC,
            language=metadata.get("language_of_performance", ["en"])[0],
            bitrate=256000 if self.is_premium else 128000,
            channels=2
        ))

        # Return the tracks
        return tracks

    # Defining a function to get chapters
    def get_chapters(self, title):
        return []

    # Defining a function to get widevine license keys
    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:

        # Send the post request to the license server
        license_raw = self.session.post(
            self.config['endpoints']['license'],
            data=challenge
        )

        # Return the license
        return base64.b64encode(license_raw.content).decode()
