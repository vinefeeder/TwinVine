from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import rinse, split_options, extract_script_with_id_json
from rich.console import Console
from unidecode import unidecode
import re
import json
from httpx import Client
import unicodedata
from urllib.parse import unquote

client = Client()



#console = Console()

class RteLoader(BaseLoader):
    options = ""

    def __init__(self):
        headers = {
            "Accept": "*/*",
            "user-agent": "Dalvik/2.9.8 (Linux; U; Android 9.9.2; ALE-L94 Build/NJHGGF)",
            "Origin": "https://plex.tv",
            "Referer": "https://plex.tv/",
        }
        super().__init__(headers)

    def receive(
        self, inx: None, search_term: None, category=None, hlg_status=False, opts=None
    ):
        """
        First fetch for series titles matching all or part of search_term.

        Uses inx, an int variable to switch:-
            0 for greedy search using url
            1 for direct url download
            2 for category browse
            3 for search with keyword
        If search_url_entry is left blank vinefeeder generates a
        a menu with 3 options:
            - Greedy Search by URL
            - Browse by Category
            - Search by Keyword
        results are forwarded to receive().
        If inx == 1, do direct download using url.
        If inx == 3, do keyword search.
        If inx == 0, fetch videos using a greedy search or browse-category.
        If inx == 2, fetch videos from a category url.
        If an unknown error occurs, exit with code 0.
        """

        if opts:
            RteLoader.options = opts
        self.options_list = split_options(RteLoader.options)
        #  RTE with aodd accented chars
        search_term = normalize_url(search_term)
        # direct download
        if "http" in search_term and inx == 1:
            
            self.options_list = split_options(self.options)
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "RTE", search_term]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "RTE", search_term]
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is the package for vinefeeder/envied installed correctly ?",
                )
            return
        
        # keyword search
        elif inx == 3:
            print(f"Searching for {search_term}")
            return self.fetch_videos(search_term)

        # ALTERNATIVES BELOW FROM POP-UP MENU
        elif inx == 0:
            # from greedy-search OR selecting Browse-category
            # example: https://www.channel4.com/programmes/the-great-british-bake-off/on-demand/75228-001

            # need a search keyword(s) from url
            # split and select series name
            search_term = search_term.split("/")[4].replace("-", " ")
            # fetch_videos_by_category search_term may have other params to remove
            if "?" in search_term:
                search_term = search_term.split("?")[0].replace("-", " ")
            return self.fetch_videos(search_term)

        elif "http" in search_term and inx == 2:
            self.category = category
            self.fetch_videos_by_category(
                search_term
            )  # search_term here holds a URL!!!

        else:
            print(f"Unknown error when searching for {search_term}")

        return
    
    def fetch_videos(self, search_term):
        params = {
            "byProgramType": "Series|Movie",
            "q": f"title:({search_term})",
            "range": "0-40",
            "schema": "2.15",
            "sort": "rte$rank|desc",
            "gzip": "true",
            "omitInvalidFields": "true",
        }
        feed = "https://feed.entertainment.tv.theplatform.eu"
        results = json.loads(client.get(f"{feed}/f/1uC-gC/rte-prd-prd-search", params=params).content)["entries"]

        for result in results:
            link = "https://www.rte.ie/player/{}/{}/{}"
            series = result.get("plprogram$programType").lower() == "series"
            _id = result.get("guid") if series else result.get("id").split("/")[-1]
            _title = result.get("title") if series else result.get("plprogram$longTitle")
            _type = result.get("plprogram$programType")

            title = _title.format(_type, _title, _id).lower()
            title = re.sub(r"\W+", "-", title)
            title = re.sub(r"^-|-$", "", title)

            description=result.get("plprogram$shortDescription"),

            episode = {
                "title":_title,
                "url": link.format(_type, title, _id),
                #"synpopsis": description[0],
                "type": _type,
            }

            self.add_episode(title, episode)
                # List series with search_term using beaupy
        selected_series = self.display_series_list()

        if selected_series:
            # one series selected
            return self.second_fetch(selected_series)
        return None
    
    def second_fetch(self, selected):
        
        episode = self.get_series(selected)
        url = normalize_url(episode[0]['url'])
        guid = url.split('/')[-1]
        type = episode[0]['type']
        if not type == 'series':
            # direct single download
            self.options_list = split_options(self.options)
            
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "RTE", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "RTE", url]    
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is devine/envied installed correctly via 'pip install <program name>?",
                )
            return None
        else:
            self.clear_series_data()  # code reuse
            
            resp = client.get(f"https://www.rte.ie/mpx/1uC-gC/rte-prd-prd-all-movies-series?byGuid={guid}" )
            data = json.loads(resp.text)
            serid = data["entries"][0]["id"].split('/')[-1]
            resp = client.get(f"https://www.rte.ie/mpx/1uC-gC/rte-prd-prd-all-programs?bySeriesId={serid}" )
            data = json.loads(resp.text)["entries"]
            #exit(0)
        # https://www.rte.ie/player/the-late-late-show/SI0000001694?epguid=IH10008885-25-0019
        # https://www.rte.ie/player/series/the-late-late-show/SI0000001694?epguid=IH10008885-25-0019
        for result in data:
            link = "https://www.rte.ie/player/series/{}/{}?epguid={}"
            series = result.get("plprogram$programType").lower() == "series"
            _id = result.get("guid") #if series else result.get("id").split("/")[-1]
            _title = result.get("title") if series else result.get("plprogram$longTitle")
            _type = result.get("plprogram$programType")
            season_no = result.get("plprogram$tvSeasonNumber") or '00'
            episode_no = result.get("plprogram$tvSeasonEpisodeNumber") or '00'
            serid = result.get('plprogram$seriesId').split('/')[-1]
            title = _title.format(_type, _title, _id).lower()
            title = re.sub(r"\W+", "-", title)
            title = re.sub(r"^-|-$", "", title)

            description=result.get("plprogram$shortDescription"),

            episode = {
                "series_no": season_no,
                "title": f"{episode_no} {_title.replace(',','')}",
                "url": link.format( title, serid, _id),
                "synopsis": description[0],
                "type": _type,
                "guid": guid
            }

            self.add_episode(title, episode)
        self.prepare_series_for_episode_selection(selected)  # creates list of series; allows user selection of wanted series prepares an episode list over chosen series
        selected_final_episodes = self.display_final_episode_list(
            self.final_episode_data
        )

        for vid in selected_final_episodes:
            ep = self.get_final_episode_list()
            url = vid.split(',')[2] 
            url = unidecode(url.replace(' ',''))  # unidecode: Irish use accents!

            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "RTE", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "RTE", url]    
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is devine/envied installed correctly via 'pip install <program name>?",
                )
            return None
        

    def fetch_videos_by_category(self, browse_url):

        print("Search by category is not implemented for this service.")

def normalize_url(url_string):
    # remove accented and url quoted chats from string
    url = url_string
    decoded_url = unquote(url)
    # Normalize and remove non-ASCII characters
    normalized_url = unicodedata.normalize('NFKD', decoded_url).encode('ASCII', 'ignore').decode()

    return(normalized_url)