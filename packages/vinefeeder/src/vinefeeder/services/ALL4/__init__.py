# channel 4 __init__.py
from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import extract_params_json, split_options
from rich.console import Console
import sys
import jmespath

console = Console()


class All4Loader(BaseLoader):
    # global options
    options = ""

    def __init__(self):
        """
        Initialize the All4Loader class with the provided headers.

        Parameters:
            None

        Attributes:
            options (str): Global options; later taken from service config.yaml
            headers (dict): Global headers; may be overridden
        """
        # self.options = ''

        headers = {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            "Origin": "https://www.channel4.com",
            "Referer": "https://www.channel4.com/",
        }
        super().__init__(headers)


    # entry point from Vinefeeder
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

        # re-entry here for second time loses options settings
        # so reset
        if opts:
            All4Loader.options = opts
        self.options_list = split_options(All4Loader.options)
        # direct download
        if "http" in search_term and inx == 1:
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "ALL4", search_term]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "ALL4", search_term]
                self.runsubprocess(command)
                return
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    f"Is {self.DOWNLOAD_ORCHESTRATOR} installed correctly via 'pip install {self.DOWNLOAD_ORCHESTRATOR}?",
                )
                return

        # keyword search
        elif inx == 3:
            print(f"Searching for {search_term}")
            return self.fetch_videos(search_term)

        #  POP-UP MENU alternates:
        elif inx == 0:
            # from greedy-search OR selecting Browse-category
            # example: https://www.channel4.com/programmes/the-great-british-bake-off/on-demand/75228-001

            # need a search keyword(s) from url
            # split and select series name
            search_term = search_term.split("/")[4]
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
        """Fetch videos from Channel 4 using a search term.
        Here the first search for series titles matches all or part of search_term.
        The function will prepare the series data, matching the search term for display.
        """
        # returns json as type String
        url = f"https://all4nav.channel4.com/v1/api/search?q={search_term}&limit=100"
        try:
            html = self.get_data(url)
            if "No Matches" in html:
                print("Nothing found for that search; try again.")
                sys.exit(0)
            else:
                parsed_data = self.parse_data(html)  # to json
        except Exception:
            print(f"No valid data returned for {url}")

            return

        # Assuming that parsed_data has a 'results' key containing video data
        if parsed_data and "results" in parsed_data:
            for item in parsed_data["results"]:
                series_name = item.get("brand", {}).get(
                    "websafeTitle", "Unknown Series"
                )
                episode = {
                    "title": item.get("brand", {}).get("title", "Unknown Title"),
                    "url": f"{item.get('brand', {}).get('href', '')}",
                    "synopsis": item.get("brand", {}).get(
                        "description", "No synopsis available."
                    ),
                }
                self.add_episode(series_name, episode)
        else:
            print(f"No valid data returned for {url}")
            return None
        # List series and episodes using beaupy
        selected_series = self.display_series_list()
        if selected_series:
            return self.second_fetch(
                selected_series
            )  # Return URLs for selected episodes
        return None

    def second_fetch(self, selected):
        """
        Given a selected series name, fetch its HTML and extract its episodes.
        Or if given a direct url, fetch it and process for greedy download.
        The function will prepare the series data for episode selection and display the final episode list.
        It will return the URLs for the selected episodes.
        """
        if "https" in selected:  # direct url provided skip url preparation
            url = selected
        else:
            url = self.get_selected_url(selected)
        try:
            myhtml = self.get_data(url=url)
        except Exception as e:
            print(f"[ALL4] Error occurred: {e}\nIs this title live on All4?\nThey list near-future titles (not yet downloadable). \n Exiting")
            return

        parsed_data = extract_params_json(myhtml)
        self.clear_series_data()  # Clear existing series data

        # Extract the episodes from the parsed data of the selected series
        if parsed_data and "initialData" in parsed_data:
            series_name = (
                parsed_data["initialData"]["brand"]["websafeTitle"] or "unknownTitle"
            )

            # Go through each episode in the selected series
            episodes = parsed_data["initialData"]["brand"].get("episodes", [])
            if not episodes:
                print(
                    f"No episodes found for {series_name}\nThis is quite common with Channel 4. Exiting."
                )
                return
            for item in episodes:
                try:
                    episode = {
                        "series_no": item.get(
                            "seriesNumber", "00"
                        ),  # number exists or '00'
                        "title": item.get("title", "Title unknown"),
                        "url": item.get("hrefLink"),
                        "synopsis": item["summary"] or None,
                    }
                except KeyError:
                    continue  # Skip any episode that doesn't have the required information
                self.add_episode(series_name, episode)

        self.options_list = split_options(self.options)

        if self.get_number_of_episodes(series_name) == 1:
            item = self.get_series(series_name)[0]
            try:
                url = "https://www.channel4.com" + item["url"]
            except Exception as e:
                print(f"[ALL4] Error occurred:\nIs this title live on All4?\nThey list near-future titles (not yet downloadable). \n\nExiting!")
                exit(1)
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "ALL4", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "ALL4", url]
                self.runsubprocess(command)
                return None
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    f"Is {self.DOWNLOAD_ORCHESTRATOR} installed correctly via 'pip install {self.DOWNLOAD_ORCHESTRATOR}?",
                )
                return

        self.prepare_series_for_episode_selection(
            series_name
        )  # creates list of series; allows user selection of wanted series prepares an episode list over chosen series
        selected_final_episodes = self.display_final_episode_list(
            self.final_episode_data
        )

        # specific to ALL4
        for item in selected_final_episodes:
            url = item.split(",")[2].lstrip()
            if url == "None":
                print(f"No valid URL for {item.split(',')[1]}")
                continue
            url = "https://www.channel4.com" + url
            try:

                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "ALL4", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "ALL4", url]
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is the package installed correctly? Try uv clean\nuv lock\n uv sysnc\n?",
                )
                return

        return

    def fetch_videos_by_category(self, browse_url):
        """
        Fetches videos from a category (Channel 4 specific).
        Args:
            browse_url (str): URL of the category page.
        Returns:
            None
        """
        beaupylist = []
        try:
            req = self.get_data(browse_url, headers=self.headers)

            # Parse the __PARAMS__ data
            init_data = extract_params_json(req, "__PARAMS__")
            # Extract brand items
            myjson = init_data["initialData"]["brands"]["items"]

            # jmespath is an efficient json parser that searches complex json
            # and, in this case, produces a simple dict from which
            # res(ults) are more easily extracted.
            # C4 specific
            res = jmespath.search(
                """
            [*].{
                href: hrefLink,
                label: labelText,
                overlaytext: overlayText            
            } """,
                myjson,
            )

            # Build the beaupylist for display
            for i, item in enumerate(res):
                label = item["label"]
                overlaytext = item["overlaytext"]
                beaupylist.append(
                    f"{i} {label}\n\t{overlaytext}"
                )  # \t used to split text later

        except Exception as e:
            print(f"Error fetching category data: {e}")
            return

        # call function in BaseLoader
        found = self.display_beaupylist(beaupylist)

        if found:
            ind = found.split(" ")[0]
            url = res[int(ind)]["href"]
            # url may be for series or single Film
            url = url.encode("utf-8", "ignore").decode().strip()  # has spaces!

            # process short-cut download or do greedy search on url
            return self.process_received_url_from_category(url)

        else:
            print("No video selected.")
            return
        
    

