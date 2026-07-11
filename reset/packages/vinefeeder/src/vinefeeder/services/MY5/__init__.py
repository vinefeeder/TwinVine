from vinefeeder.base_loader import BaseLoader
import jmespath
from rich.console import Console
from vinefeeder.parsing_utils import parse_json, split_options
import re

console = Console()


class My5Loader(BaseLoader):
    options = ""

    def __init__(self):
        headers = {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            "Origin": "https://www.channel5.com",
            "Referer": "https://www.channel5.com/",
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
            My5Loader.options = opts
        self.options_list = split_options(My5Loader.options)

        # direct download

        if "http" in search_term and inx == 1:
            # options_list = split_options(self.options)
            if self.options_list[0] == "":
                command = ['uv', 'run', 'envied', "dl", "MY5", search_term]
            else:
                command = ['uv', 'run', 'envied', "dl", *self.options_list, "MY5", search_term]
            self.runsubprocess(command)

            return

        # keyword search
        elif inx == 3:
            print(f"Searching for {search_term}")
            return self.fetch_videos(search_term)

        # ALTERNATIVES BELOW FROM POP-UP MENU
        elif inx == 0:
            # from greedy-search OR selecting Browse-category
            if not 'https' in search_term:
                return self.fetch_videos(search_term)
            # example: https://www.channel5.com/the-teacher/season-2/episode-2
            # need a search keyword(s) from url
            # split and select series name
            
            if "show" in search_term:
                match = re.search(r"\/show\/([a-z-]+)", search_term)

                if match:
                    search_term = str(match.group(1))
                    search_term = search_term.split("-")[:-1]
                    search_term = str(" ".join(search_term)).replace("-", " ")

            else:
                search_term = search_term.split("/")[3].replace("-", " ")
            # fetch_videos_by_category search_term may have other params to remove
            if "/" in search_term:
                search_term = search_term.split("/")[0].replace("-", " ")
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
        """Fetch videos from ITV using a search term."""
        url = f"https://corona.channel5.com/shows/search.json?platform=my5desktop&friendly=1&query={search_term}"
        response = self.get_data(url, headers=self.headers)
        parsed_data = parse_json(response)

        if parsed_data and "shows" in parsed_data:
            for item in parsed_data["shows"]:
                series_name = item.get("title", "Unknown Series")
                episode = {
                    "title": item.get("title", "Unknown Title"),
                    "slug": f"{item.get('f_name', '')}",
                    "synopsis": f"{item.get('s_desc', '')}",
                    "url": f"https://corona.channel5.com/shows/{item.get('f_name', '')}/seasons.json?platform=my5desktop&friendly=1",
                }
                self.add_episode(series_name, episode)

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
        if "https" in selected:  # direct url: not sure how this may happen with My5??
            url = selected
            self.options_list = split_options(self.options)
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "MY5", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "MY5", url]
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is self.DOWNLOAD_ORCHESTRATOR installed correctly via 'pip install self.DOWNLOAD_ORCHESTRATOR?",
                )
            return
        else:
            url = self.get_selected_url(selected)
        try:
            myhtml = self.get_data(url=url)
        except Exception:
            print(f"No valid data at {url} found.\n Exiting")
            return

        parsed_data = parse_json(myhtml)
        self.clear_series_data()  # Clear existing series data

        # Extract the episodes from the parsed data of the selected series
        brndslug = url.split("/")[4]
        if parsed_data and "seasons" in parsed_data:
            for item in parsed_data["seasons"]:
                try:
                    # no season means single episode
                    # check and call self.receive for single episode
                    if item["seasonNumber"] is None:
                        url = f"https://www.channel5.com/show/{brndslug}"
                        self.receive(1, url)
                        return
                    else:
                        url = f"https://corona.channel5.com/shows/{brndslug}/seasons/{item['seasonNumber']}/episodes.json?platform=my5desktop&friendly=1&linear=true"
                    url = url.encode("utf-8", "ignore").decode().strip()
                    myhtml = self.get_data(url=url)
                    parsed_data = parse_json(myhtml)
                    if len(parsed_data["episodes"]) == 0:
                        continue

                    series_name = parsed_data["episodes"][0]["sh_f_name"]
                    for item in parsed_data["episodes"]:
                        episode = {
                            "series_no": f"{item['sea_num'] or '100'}",
                            "title": f"{item['ep_num'] or ''}:{item['title'] or ''}",
                            "url": f"https://www.channel5.com/{item['sh_f_name']}/{item['sea_f_name']}/{item['f_name']}",
                            "synopsis": item["s_desc"] or None,
                        }
                        self.add_episode(series_name, episode)
                except KeyError:
                    continue  # Skip any episode that doesn't have the required information

        self.prepare_series_for_episode_selection(
            series_name
        )  # creates list of series; allows user selection of wanted series prepares an episode list over chosen series
        selected_final_episodes = self.display_final_episode_list(
            self.final_episode_data
        )

        # download

        self.options_list = split_options(self.options)
        for item in selected_final_episodes:
            url = item.split(",")[2].lstrip()
            if url == "None":
                print(f"No valid URL for {item.split(',')[1]}")
                continue
            if 'http' not in url:
                url_pattern = r'https?://[^\s,]+'
                match = re.search(url_pattern, item)
                if match:
                    url = match.group()

            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "MY5", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "MY5", url]
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is the package installed correctly?",
                )
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

            # Parse the json data
            init_data = parse_json(req)
            # Extract brand items
            myjson = init_data["shows"]

            # jmespath is an efficient json parser that searches complex json
            # and, in this case, produces a simple dict from which
            # res(ults) are more easily extracted.
            # C4 specific
            res = jmespath.search(
                """
            [*].{
                href: f_name,
                label: title,
                overlaytext: s_desc            
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
            url = f"https://www.channel5.com/show/{res[int(ind)]['href']}"
            # url may be for series or single Film
            url = url.encode("utf-8", "ignore").decode().strip()  # has spaces!

            # process short-cut download or do greedy search on url
            return self.process_received_url_from_category(url)

        else:
            print("No video selected.")
            return

