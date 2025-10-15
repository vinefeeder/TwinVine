from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import split_options
import jmespath
from rich.console import Console

console = Console()


class ULoader(BaseLoader):
    options = ""

    def __init__(self):
        headers = {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            "Origin": "https:///u.co.uk",
            "Referer": "https:///u.co.uk/",
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
            ULoader.options = opts
        self.options_list = split_options(ULoader.options)

        # direct download
        if "http" in search_term and inx == 1:
            # options_list = split_options(ULoader.options)
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "U", search_term]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "U", search_term]
                self.runsubprocess(command)  # url
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is self.DOWNLOAD_ORCHESTRATOR installed correctly via 'pip install self.DOWNLOAD_ORCHESTRATOR?",
                )

            return

        # keyword search
        elif inx == 3:
            print(f"Searching for {search_term}")
            return self.fetch_videos(search_term)

        # ALTERNATIVES BELOW FROM POP-UP MENU
        elif inx == 0:
            # from greedy-search OR selecting Browse-category
            # example  https://u.co.uk/shows/worlds-most-dangerous-roads/series-6/episode-1/6363704256112
            if not 'https' in search_term:
                return self.fetch_videos(search_term)
            # need a search keyword(s) from category url
            # split and select series name
            else:
                # need a search keyword(s) from url
                # split and select series name
                search_term = search_term.split("/")[4].replace("-", " ")
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
        headers = {
            "user-agent": "Dalvik/2.9.8 (Linux; U; Android 9.9.2; ALE-L94 Build/NJHGGF)",
            "Host": "vschedules.uktv.co.uk",
            "Origin": "https://uktv.co.uk",
            "Referer": "https://uktv.co.uk/",
        }
        url = f"https://vschedules.uktv.co.uk/vod/search/?q={search_term}"

        html = self.get_data(url, headers)
        parsed_data = self.parse_data(html)  # to json ()

        # select only from FREE tier
        res = jmespath.search(
            """
            [].{
            title: name,
            slug: slug,
            synopsis: synopsis
            type: type
            } """,
            parsed_data,
        )

        for item in res:
            if item["type"] == "COLLECTION":
                continue  # Skip to the next iteration if type is COLLECTION
            title = item["title"]
            slug = item["slug"]
            synopsis = item["synopsis"]
            url = f"https://vschedules.uktv.co.uk/vod/brand/?slug={slug}"

            episode = {
                "title": item.get("title", "Unknown Title"),
                "url": url,
                "synopsis": synopsis,
            }
            self.add_episode(title, episode)

        # List series with search_term using beaupy
        selected_series = self.display_series_list()

        if selected_series:
            return self.second_fetch(selected_series)
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
            myhtml = self.get_data(url)
        except Exception:
            print(f"No valid data at {url} found.\n Exiting")
            return
        parsed_data = self.parse_data(myhtml)

        series_ids = [series["id"] for series in parsed_data["series"]]
        if not series_ids:
            print(f"No series data found at {url}.\n Exiting")
            return

        # Go through each episode in the selected series
        for series_id in series_ids:
            html = self.get_data(
                f"https://vschedules.uktv.co.uk/vod/series/?id={series_id}"
            )
            parsed_data = self.parse_data(html)

            try:
                for item in parsed_data["episodes"]:
                    # series_id = item['series_id']
                    ep_number = item["episode_number"]
                    ser_number = item.get("series_number", "100")
                    video_id = item["video_id"]
                    brand_slug = item["brand_slug"]
                    url = f"https://u.co.uk/shows/{brand_slug}/series-{ser_number}/episode-{ep_number}/{video_id}"
                    synopsis = item["synopsis"]

                    episode = {
                        "series_no": ser_number,
                        "title": ep_number,
                        "url": url,
                        "synopsis": synopsis,
                    }
                    self.add_episode(brand_slug, episode)

            except KeyError:
                continue  # Skip any episode that doesn't have the required information

        self.prepare_series_for_episode_selection(
            brand_slug
        )  # creates list of series; allows user selection of wanted series prepares an episode list over chosen series
        selected_final_episodes = self.display_final_episode_list(
            self.final_episode_data
        )
        self.options_list = split_options(self.options)
        for item in selected_final_episodes:
            url = item.split(",")[2].lstrip()
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "U", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "U", url]
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is the package installed correctly?",
                )

        return None

    def fetch_videos_by_category(self, browse_url):
        print(
            "Method not implemented for fetching videos by category for this channel."
        )
        return None
    

