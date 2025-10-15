# TVNZ __init__.py
from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import split_options, list_prettify
from rich.console import Console
from beaupy import select_multiple

console = Console()

# TV Shows https://www.tvnz.co.nz/shows/boiling-point/episodes/s1-e1 or
# Movies https://www.tvnz.co.nz/shows/legally-blonde/movie/s1-e1 or
# Sport https://www.tvnz.co.nz/sport/football/uefa-euro/spain-v-france-semi-finals-highlights


class TvnzLoader(BaseLoader):
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

        headers = {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            #'Origin': 'https://www.tvnz.co.nz',
            #'Referer': 'https://www.tvnz.co.nz',
        }
        super().__init__(headers)
        self.showType = None

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
            TvnzLoader.options = opts
        self.options_list = split_options(TvnzLoader.options)
        # direct download

        if "http" in search_term and inx == 1:
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "TVNZ", search_term]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "TVNZ", search_term]
                self.runsubprocess(command)
                return
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

        #  POP-UP MENU alternates:
        elif inx == 0:
            # from greedy-search OR selecting Browse-category
            # need a search keyword(s) from url
            # split and select series name
            if not 'https' in search_term:
                return self.fetch_videos(search_term)
            # need a search keyword(s) from category url
            # split and select series name
            else:
                search_term = search_term.split("/")[4]

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
        """Fetch videos from TVNZ using a search term.
        Here the first search for series titles matches all or part of search_term.
        The function will prepare the series data, matching the search term for display.
        """
        # returns json as type String
        url = f"https://apis-public-prod.tech.tvnz.co.nz/api/v1/web/play/search?q={search_term}"
        try:
            html = self.get_data(url)
            if "No Matches" in html:
                print("Nothing found for that search; try again.")
                return
            else:
                parsed_data = self.parse_data(html)  # to json
        except Exception:
            print(f"No valid data returned for {url}")
            return
        # console.print_json(data=parsed_data)
        """f = open('tvnz.json', 'w')
        f.write(json.dumps(parsed_data))  # parsed_data)
        f.close()"""
        if parsed_data and "results" in parsed_data:
            for item in parsed_data["results"]:
                series_name = item.get("title", "Unknown Series")
                url = "https://apis-edge-prod.tech.tvnz.co.nz" + item.get(
                    "page", {}
                ).get("href", "")
                episode = {
                    "type": item.get("type"),  # 'type'
                    "title": item.get("title", "Unknown Title"),
                    "url": url,
                    "synopsis": item.get("synopsis", "No synopsis available."),
                }
                self.add_episode(series_name, episode)
        else:
            print(f"No valid data returned for {url}")
            return None
        selected_series = self.display_series_list()
        if selected_series:
            return self.second_fetch(
                selected_series
            )  # Return URLs for selected episodes
        return None

    def second_fetch(self, selected):
        # TVNZ video type: sportVideo, showVideo or Movie
        # makes this extractor unusual and should not be used
        # to model other services
        """
        Given a selected series name, fetch its HTML and extract its episodes.
        Or if given a direct url, fetch it and process for greedy download.
        The function will prepare the series data for episode selection and display the final episode list.
        It will return the URLs for the selected episodes.
        """
        if "https" in selected:  # direct url provided skip url preparation
            url = selected
        else:
            # initial pass to find type
            series_name = (
                selected.lower().replace(" ", "-").replace(":", "").replace(",", "")
            )
            episodes = self.get_series_data()
            episode_test = episodes[selected][0]
            type = episode_test.get("type")
            beaupylist = []
            # if sport video / news video - assume no episodes
            # use data from first fetch directly
            if type == "sportVideo" or type == "newsVideo":
                for item in episodes[selected]:  # existing data
                    url = "https://www.tvnz.co.nz/" + item.get("url").split("/page/")[1]
                    beaupylist.append(
                        [
                            item.get("title"),
                            url,
                            item.get("synopsis", "No synopsis available."),
                        ]
                    )

                selected = select_multiple(
                    beaupylist,
                    preprocessor=lambda val: list_prettify(val),
                    minimal_count=1,
                    cursor_style="pink1",
                    pagination=True,
                    page_size=8,
                )

                for item in selected:
                    url = item[1]
                    if self.options_list[0] == "":
                        command = ['uv', 'run', 'envied', "dl", "TVNZ", url]
                    else:
                        command = ['uv', 'run', 'envied', "dl", *self.options_list, "TVNZ", url]
                    self.runsubprocess(command)
                return None

            elif type == "show" or type == "showVideo":
                url = f"https://apis-public-prod.tech.tvnz.co.nz/api/v1/web/play/page/shows/{series_name}/episodes"
                try:
                    html = self.get_data(url)
                    parsed_data = self.parse_data(html)
                except Exception:
                    try:
                        # direct download as seems only one episode
                        # https://www.tvnz.co.nz/shows/circle-of-friends/movie/s1-e1
                        url = f"https://www.tvnz.co.nz/shows/{series_name}/movie/s1-e1"
                        if self.options_list[0] == "":
                            command = ['uv', 'run', 'envied', "dl", "TVNZ", url]
                        else:
                            command = ['uv', 'run', 'envied', "dl", *self.options_list, "TVNZ", url]
                        self.runsubprocess(command)
                        return

                    except Exception as e:
                        print(
                            "Error downloading video:",
                            e,
                            "Is self.DOWNLOAD_ORCHESTRATOR installed correctly via 'pip install self.DOWNLOAD_ORCHESTRATOR?",
                        )
                        return
                try:
                    href_list = []
                    # iterate over all seasons and capture url for each
                    for item in parsed_data["layout"]["slots"]["main"]["modules"][0][
                        "lists"
                    ]:
                        href_list.append(item["href"])
                except Exception:
                    # object►layout►slots►main►modules►0►mobiledoc►sections►0►2►0►3
                    try:
                        message = parsed_data["layout"]["slots"]["main"]["modules"][0][
                            "mobiledoc"
                        ]["sections"][0][2][0][3]
                        print(
                            "There is no content for this series. TVNZ says ..."
                            + message
                        )
                    except Exception:
                        print(f"No valid data returned for {url}")
                    return
        # with season url, iterate over each season and capture episodes
        try:
            for url in href_list:  #  for all seasons
                url = "https://apis-edge-prod.tech.tvnz.co.nz" + url
                myhtml = self.get_data(url=url)
                parsed_data = self.parse_data(myhtml)
                if parsed_data and "_embedded" in parsed_data:
                    try:
                        for item_key, item in parsed_data["_embedded"].items():
                            series_no = item.get("seasonNumber", "100")
                            myurl = "https://www.tvnz.co.nz" + item.get("page", {}).get(
                                "url", ""
                            )
                            episode_no = item.get("episodeNumber", None)
                            synopsis = item.get("synopsis", "No synopsis available")
                            episode = {
                                "series_no": series_no,
                                "title": episode_no,
                                "url": myurl,
                                "synopsis": synopsis,
                            }
                            self.add_episode(series_name, episode)
                    except Exception:
                        pass  # disregard any episode that does not fit the scheme

                else:
                    print(f"No valid data at {url} found.\n Exiting")
                    return
        except Exception:
            print(f"No valid data returned for {url}")
            return

        self.options_list = split_options(self.options)
        # direct download single items
        if self.get_number_of_episodes(series_name) == 1:
            item = self.get_series(series_name)[0]
            # check if has https://www.tvnz.co.nz

            if "https://www.tvnz.co.nz" in item["url"]:
                url = item["url"]
            else:
                url = f'https://www.tvnz.co.nz{item["url"]}'
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "TVNZ", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "TVNZ", url]
                self.runsubprocess(command)
                return None
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is self.DOWNLOAD_ORCHESTRATOR installed correctly via 'pip install self.DOWNLOAD_ORCHESTRATOR?",
                )
                return
        # else present list of series and display for multiple selection
        self.prepare_series_for_episode_selection(
            series_name
        )  # creates list of series; allows user selection of wanted series prepares an episode list over chosen series
        self.final_episode_data = self.sort_episodes(self.get_final_episode_list())
        selected_final_episodes = self.display_final_episode_list(
            self.final_episode_data
        )

        for item in selected_final_episodes:
            url = item.split(",")[2].lstrip()

            if url == "None":
                print(f"No valid URL for {item.split(',')[1]}")
                continue

            if self.options_list[0] == "":
                command = ['uv', 'run', 'envied', "dl", "TVNZ", url]
            else:
                command = ['uv', 'run', 'envied', "dl", *self.options_list, "TVNZ", url]
            self.runsubprocess(command)

        return

    def fetch_videos_by_category(self, browse_url):
        """
        Fetches videos from a category (VERY TVNZ specific).
        Args:
            browse_url (str): URL of the category page.
        Returns:
            None
        """

        try:
            req = self.get_data(browse_url, headers=self.headers)

            # Parse the json returned
            parsed_data = self.parse_data(req)

            # for development only
            """console.print_json(data=parsed_data)
            f = open("cat_tvnz.json",'w')
            f.write(json.dumps(parsed_data))
            f.close()"""

            i = 1
            beaupylist = []
            linkList = []

            for item_key, item in parsed_data["_embedded"].items():
                try:
                    if item.get("type") == "category":
                        continue
                    elif item.get("type") == "showVideo":
                        myurl = "https://www.tvnz.co.nz" + item.get("page", {}).get(
                            "url", ""
                        )
                        showType = item.get("showType", "unknown")
                    elif item.get("type") == "show":
                        showType = item.get("showType", "unknown")
                        myurl = "https://www.tvnz.co.nz" + item.get(
                            "watchAction", {}
                        ).get("link", "")
                    elif item.get("type") == "sportVideo":
                        showType = item.get("showType", "unknown")
                        myurl = "https://www.tvnz.co.nz" + item.get("page", {}).get(
                            "url", ""
                        )
                    title = item.get("title", "Unknown Title")
                    # we are adding an episode to the series data using title as series name
                    # There will be some episodes with the same series name.
                    # in the context of category browsing TVNZ allows duplicates.
                    # Below we only take the fist item with a series name
                    # and add that to our beaupylist for display. Thus duplicates are not added.
                    episode = {
                        "index": i,
                        "title": item.get("title", "Unknown Title"),
                        "synopsis": item.get("synopsis", "No synopsis available"),
                    }
                    self.add_episode(title, episode)
                    # linked list allows keeping the url away from beaupy list
                    # we pull the url from selected by referencing the index  in linkList
                    linkList.append([myurl, showType])
                    i += 1
                except Exception:
                    continue
        except Exception as e:
            print(f"Error fetching category data: {e}")
            return

        data = self.get_series_data()
        # unique to TVNZ
        # get first item in each list to create a beaupylist
        # without duplicates
        for key, items in data.items():
            if items:  # Check if the list is not empty
                first_item = items[
                    0
                ]  # Get the first dictionary, any more may be duplicates
                index = first_item["index"]
                title = first_item["title"]
                synopsis = first_item["synopsis"]
                beaupylist.append([index, title, synopsis])

        found = self.list_display_beaupylist(beaupylist)
        self.clear_series_data()
        if found:
            ind = found[0]
            url, showType = linkList[int(ind) - 1]
            if showType == "Movie" or showType == "NonEpisodic":
                # direct download
                return self.receive(inx=1, search_term=url)
            else:
                # greedy search
                search_term = url.split("/")[4].replace("-", " ")
                return self.receive(inx=3, search_term=search_term)
        else:
            print("No video selected.")
            return
        

