## Plex service for twinvine/vinefeeder
from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import rinse, split_options, extract_script_with_id_json
from rich.console import Console
import jmespath
import uuid

#console = Console()


class PlexLoader(BaseLoader):
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
            PlexLoader.options = opts
        self.options_list = split_options(PlexLoader.options)
        # direct download
        if "http" in search_term and inx == 1:
            self.options_list = split_options(self.options)
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "PLEX", search_term]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "PLEX", search_term]
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
            if not 'https' in search_term:
                return self.fetch_videos(search_term)
            # need a search keyword(s) from category url
            # split and select series name
            else:
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
        URL = ("https://discover.provider.plex.tv/library/search/"
       "?searchProviders=discover,plexAVOD,plexFAST"
       "&includeGroups=1"
       "&searchTypes=all%2Clivetv%2Cmovies%2Ctv%2Cpeople"
       "&includeMetadata=1"
       "&filterPeople=1"
       "&limit=10"
       f"&query={search_term}")

        # Helpful (and expected!!) Plex headers + ask for JSON
        HEADERS = {
            "Accept": "application/json",
            "X-Plex-Product": "Plex Mediaverse",
            "X-Plex-Version": "1.0",
            "X-Plex-Client-Identifier": str(uuid.uuid4()),
            # If you have a Plex token, add it (some endpoints need it):
            # "X-Plex-Token": "<YOUR_TOKEN>"
        }
        jsn = self.fetch_and_parse(URL, HEADERS)
        #console.print_json(data=jsn)
        '''f = open("/home/angela/plex.json", "w")
        f.write(json.dumps(jsn))
        f.close()'''
        
        res = jmespath.search(
            """
            MediaContainer.SearchResults[?id=='plex'] | [0].SearchResult[].{
            slug:  Metadata.slug,
            title: Metadata.title,
            type:  Metadata.type
            }
            """,
            jsn,
        )
       

        for item in res:
            slug = item["slug"]
            title = item["title"]
            type = item["type"] 
            url = f"https://watch.plex.tv/{type}/{slug}"

            episode = {
                "title": item.get("title", "Unknown Title"),
                "url": url,
                "synopsis": item.get("type"),
            }
            self.add_episode(title, episode)

        # List series with search_term using beaupy
        selected_series = self.display_series_list()

        if selected_series:
            # one series selected
            return self.second_fetch(selected_series)
        return None
    
    def second_fetch(self, selected):
        """
        Given a selected series name, fetch its HTML and extract its episodes.
        Or if given a movie url, fetch it and process for download.
        The function will prepare the series data for episode selection and display the final episode list.
        It will return the URLs for the selected episodes.
        """
        episode = self.get_series(selected)
        url = episode[0]['url']
        if 'movie' in url:
            # direct single download
            self.options_list = split_options(self.options)
            
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "PLEX", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "PLEX", url]    
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is devine/envied installed correctly via 'pip install <program name>?",
                )
            return None

        else:
            try:
                headers = {
                    "referer": "https://www.plex.tv",
                    "user-agent": "Dalvik/2.9.8 (Linux; U; Android 9.9.2; ALE-L94 Build/NJHGGF)",
                }
                myhtml = self.get_data(url=url, headers=headers)
                import re
                from lxml import html

                doc = html.fromstring(myhtml)

                aria_labels = doc.xpath("//div[@data-id='seasons']//a[contains(@aria-label,'Season ')]/@aria-label")
                titles = doc.xpath("//div[@data-id='seasons']//*[@title[starts-with(.,'Season ')]]/@title")

                parts = []
                for lbl in aria_labels:
                    if "•" in lbl:
                        parts.append(lbl.split("•", 1)[1].strip())
                parts.extend(titles)

                def season_num(s: str, default=10**9):
                    m = re.search(r'\d+', s)
                    return int(m.group()) if m else default

                seasons = sorted({p.strip().lower() for p in parts}, key=season_num)
                self.clear_series_data()  #  safely remove any leftovers ready for building 
                for item in seasons:
                    url = f"https://watch.plex.tv/show/{selected.lower().replace(' ','-')}/season/{item.split(' ')[1]}"            
                    html_bytes = self.get_data(url=url, headers=headers)
                    doc = html.fromstring(html_bytes)
                    for fig in doc.xpath("//div[@data-id='episodes']//figure"):
                        hrefs = fig.xpath(".//a[contains(@href,'/season/') and contains(@href,'/episode/')]/@href")
                        if not hrefs:
                            continue
                        relative_link = hrefs[0].split("?", 1)[0]
                        m = re.search(r"/season/(\d+)/episode/(\d+)", relative_link)
                        if not m:
                            continue
                        season_no, episode_no = map(int, m.groups())
                        title = fig.xpath("normalize-space(.//figcaption/span[1]/@title | .//figcaption/span[1]/text())")
                        synopsis = fig.xpath("normalize-space(.//figcaption/span[2]/@title | .//figcaption/span[2]/text())")
                        episode = {
                            "series_no": season_no,
                            "title": f"{episode_no} {title}",
                            "url": f"https://watch.plex.tv{relative_link}",
                            "synopsis": rinse(synopsis) or None,
                        }
                        self.add_episode(selected, episode)
                    
                
            except Exception as e:
                print(f"No valid data at {url} found.\n Exiting  {e}")
                return


        self.prepare_series_for_episode_selection(selected)  # creates list of series; allows user selection of wanted series prepares an episode list over chosen series
        selected_final_episodes = self.display_final_episode_list(
            self.final_episode_data
        )
        self.options_list = split_options(self.options)
        for item in selected_final_episodes:
            url = item.split(",")[2].lstrip()
            if 'http' not in url:
                url_pattern = r'https?://[^\s,]+'
                match = re.search(url_pattern, item)
                if match:
                    url = match.group()
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "PLEX", url]
                else:
                    command = ['uv','run', 'envied', "dl", *self.options_list, "PLEX", url]
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is package/envied installed correctly?",
                )

        return None

    def fetch_videos_by_category(self, browse_url):
        """
        Fetches videos from a category Plex
        Args:
            browse_url (str): URL of the category page.
        Returns:
            None
        """
        category = browse_url.split('/')[-1]
        browse_url = f"https://luma.plex.tv/api/screen/on-demand/category/{category}"
        beaupylist = []  # hold beaupy data for display and programme selection
        headers = {
            "Accept": "application/json",
            "X-Plex-Product": "Plex Mediaverse",
            "X-Plex-Version": "1.0",
            "X-Plex-Client-Identifier": str(uuid.uuid4()),
        }
        try:
            myjson = self.parse_data(self.get_data(browse_url))
            res = jmespath.search(
            """
            ui.list.content[].{
                label: label,
                link: link.url,
                duration: link.previewData.facts[1].accessibilityLabel,
                pay: link.external
            }
            """,
            myjson,
            )
       
            for item in res:
                title = item["label"]
                duration= item["duration"] 
                url = f"https://watch.plex.tv{item.get("link")}"

                episode = {
                    "title": title,
                    "url": url,
                    "synopsis": duration,
                }
                self.add_episode(title, episode)


            # Build the beaupylist for display
            for i, item in enumerate(res):
                title = (item["label"].replace("-", " ")).title()
                url = f"https://watch.plex.tv{item["link"]}"
                synopsis = item["duration"]
                beaupylist.append(
                    f"{i} {title.replace('_',' ')}\n\t{synopsis}"
                )  # \t used to split text later

        except Exception as e:
            print(f"Error fetching category data: {e}")
            return

        # super
        found = self.display_beaupylist(beaupylist)

        if found:
            ind = found.split(" ")[0]
            url = f"https://watch.plex.tv{res[int(ind)]["link"]}"
            # url may be for series or single Film
            url = url.encode("utf-8", "ignore").decode().strip()  # has spaces!
            # process short-cut download or do greedy search on url
            if 'movie' in url:
                self.options_list = split_options(self.options)
                try:
                    if self.options_list[0] == "":
                        command = ['uv', 'run', 'envied', "dl", "PLEX", url]
                    else:
                        command = ['uv', 'run', 'envied', "dl", *self.options_list, "PLEX", url]
                    self.runsubprocess(command)
                except Exception as e:
                    print(
                        "Error downloading video:",
                        e,
                        "Is the package for vinefeeder/envied installed correctly ?",
                    )
                return
            category = "show"
            return self.process_received_url_from_category(url, category=category)

        else:
            print("No video selected.")
            return