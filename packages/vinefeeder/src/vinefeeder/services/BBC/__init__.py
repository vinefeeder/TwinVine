# BBC  __init__.py
"""The BBC does not follow the same structures as most other srvices
The BBC does not use  script = window.__PARAMS__ = ..."""

from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import extract_params_json, parse_json, split, split_options
from rich.console import Console
import jmespath
from scrapy.selector import Selector

console = Console()

"""
Note: The BBC is outrageously awkward. Do not use this as a template for other services!
"""

## BBC iPlayer sets a limit of 200 episodes per page
## This is a global variable to keep track of the page number
## if the number of episodes exceeds 200 set page to 2
## and increment by 1 for each subsequent page
PAGE = 1

class BbcLoader(BaseLoader):
    HLG = None
    options = None

    def __init__(self):
        # self.HLG = None
        self.AVAILABLE_HLG = False
        self.uhd_list = []

        headers = {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko",
            "Origin": "https://www.bbc.com",
            "Referer": "https://www.bbc.com/",
        }
        super().__init__(headers)

    # Check if UHD content is available BBC specific
    def check_uhd(self):
        """
        Fetch the list of UHD content from the BBC iPlayer website.
        Returns:
            list: A list of URLs with UHD content.
        """

        uhd_url = "https://www.bbc.co.uk/iplayer/help/questions/programme-availability/uhd-content"
        html = self.get_data(uhd_url)
        sel = Selector(text=html)
        uhd_list = sel.xpath("(//ul)[9]//a/text()").getall()
        #url_list = sel.xpath("(//ul)[9]//a/@href").getall()

        return uhd_list


    def receive(
        self, inx: None, search_term: None, category=None, hlg_status=False, opts=None
    ):
        if hlg_status:
            BbcLoader.HLG = hlg_status
        if opts:
            BbcLoader.options = opts
        self.options_list = split_options(BbcLoader.options)
        # prepare in UHD list
        self.uhd_list = self.check_uhd()
        # standardize for comparison later
        results = []
        for item in self.uhd_list:
            item = item.title()
            results.append(item)
        self.uhd_list = results

        

        # standardize
        if "https" in search_term:
            pass
        else:
            search_term = search_term.strip().lower().title().replace('  ',' ')


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
        # TWO ALTERNATES FROM GUI-TEXT ENTRY; POULATED WITH KEYWORD OR URL
        # direct download from url
        if "http" in search_term and inx == 1:
            # 'https://www.bbc.co.uk/iplayer/episode/m0021yyt'
            # https://www.bbc.co.uk/iplayer/episode/m0023h9f/asia-series-1-1-beneath-the-waves
            series_name = split(search_term, "/", 6)[1].split("-series")[0]
            if series_name:
                for hlg_item in self.uhd_list:
                    hlg_item = hlg_item.lower().replace(" ", "-")
                    if series_name in hlg_item:
                        self.AVAILABLE_HLG = True
                        break
                search_term = split(search_term, "?", 1)[0].replace(
                    "episodes", "episode"
                )
            else:
                self.AVAILABLE_HLG = False

            self.options_list = split_options(BbcLoader.options)

            if BbcLoader.HLG and self.AVAILABLE_HLG:
                try:
                    command = (
                        [
                            'uv', 'run', 'envied',
                            "dl",
                            *self.options_list,
                            "--range",
                            "HLG",
                            "iP",
                            search_term,
                        ]
                        if  self.options_list
                        else ['uv', 'run', 'envied', "dl", "--range", "HLG", "iP", search_term]
                    )
                    self.runsubprocess(command)
                except Exception as e:
                    print(
                        "Error downloading video:",
                        e,
                        "Is self.DOWNLOAD_ORCHESTRATOR installed correctly via 'pip install self.DOWNLOAD_ORCHESTRATOR?",
                    )

            else:
                try:
                    command = (
                        ['uv', 'run', 'envied', "dl", *self.options_list, "iP", search_term]
                        if self.options_list
                        else ['uv', 'run', 'envied', "dl", "iP", search_term]
                    )
                    self.runsubprocess(command)
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
            for hlg_item in self.uhd_list:

                if search_term in hlg_item:
                    self.AVAILABLE_HLG = True
                    break
            return self.fetch_videos(search_term)

        # ALTERNATIVES BELOW FROM POP-UP MENU
        # from greedy-search OR selecting Browse-category
        elif inx == 0 and "https" in search_term:
            if "episode" in search_term:
                # https://www.bbc.co.uk/iplayer/episodes/p09twdp8/showtrial?seriesId=m0023h9h
                search_term = split(search_term, "/", 6)[1]  # search_term
                # fetch_videos_by_category search_term may have other params to remove
                if "?" in search_term:
                    search_term = search_term.split("?")[0].replace("-", " ")
                    return self.fetch_videos(search_term)
                elif "series" in search_term:
                    search_term = (
                        search_term.split("series")[0].replace("-", " ").strip()
                    )
                    return self.fetch_videos(search_term)

            else:
                print(
                    f"No search term found  in {search_term}\nTry again with a series name in the url."
                )
                return

        elif "http" in search_term and inx == 2:
            self.category = category
            self.fetch_videos_by_category(
                search_term
            )  # search_term here holds a URL!!!

        else:
            print(f"Unknown error when searching for {search_term}")

        return

    def fetch_videos(self, search_term):
        """Fetch videos from BBC using a search term."""

        url = "https://ibl.api.bbc.co.uk/ibl/v1/new-search"
        # url = f"https://search.api.bbci.co.uk/formula/iplayer-ibl-root?q={search_term}&apikey=D2FgtcTxGqqIgLsfBWTJdrQh2tVdeaAp&seqId=0582e0f0-b911-11ee-806c-11c6c885ab56"
        params = {
            "q": search_term,
            "rights": "web",
            #'mixin': 'live'
        }
        try:
            html = self.get_data(url, self.headers, params)
            if "new_search" not in html:
                print("Nothing found for that search; try again.")
                return
            else:
                parsed_data = parse_json(html)  # to json
        except Exception:
            print(f"No valid data returned for {url}")
            return

        if parsed_data and "new_search" in parsed_data:
            for item in parsed_data["new_search"]["results"]:
                series_name = item.get(
                    "title", "Unknown Title"
                )  # BBC has a 'title' key for series names

                episode = {
                    "title": item.get("title", "Unknown Title"),
                    "url": f"{item.get('id', '')}",
                    "synopsis": item.get("synopsis", "No synopsis available."),
                }
                self.add_episode(series_name, episode)
        else:
            print(f"No data found for {search_term}")
        # List series and episodes using beaupy

        selected_series = self.display_series_list()
        if selected_series:
            return self.second_fetch(
                selected_series
            )  # Return URLs for selected episodes
        return None

    def second_fetch(self, selected_series):
        # 'selected' is search term
        """
        Given a selected series name, fetch its HTML and extract its episodes.
        need programme Id for url lookup
        Or if given a direct url, fetch it and process for greedy download.
        The function will prepare the series data for episode selection and display the final episode list.
        It will return the URLs for the selected episodes.
        """

        selected = selected_series

        #  option for direct url
        if "https" in selected:  # direct url provided skip url preparation
            url = selected  # 'https://www.bbc.co.uk/iplayer/episodes/m000mfhl'
            # url = f"https://www.bbc.co.uk/iplayer/episode/{url.split('/')[-1]}"
        # option for search
        else:
            url = self.get_selected_url(
                selected
            )  # Here url is hhtps://www.bbc.co.uk/iplayer/episodes/m000mfhl
            url = url.split("/")[-1]
            # thanks to kenyard for help with url preparation

            # https://www.bbc.co.uk/iplayer/episodes/b007y6k8/silent-witness           
            myurl = f"https://ibl.api.bbci.co.uk/ibl/v1/episodes/{url}?rights=mobile&availability=available&api_key=D2FgtcTxGqqIgLsfBWTJdrQh2tVdeaAp"
            myhtml = self.get_data(url=myurl, headers=self.headers)
            parsed_data = parse_json(myhtml)
            SINGLE = True
            if parsed_data["episodes"] == []:
                SINGLE = False
                url = f"https://ibl.api.bbci.co.uk/ibl/v1/programmes/{url}/episodes?rights=mobile&availability=available&page={PAGE}&per_page=200&api_key=D2FgtcTxGqqIgLsfBWTJdrQh2tVdeaAp"
                myhtml = self.get_data(url=url, headers=self.headers)
                parsed_data = parse_json(myhtml)

        # testing
        #file = open("init_data.json", "w")
        #file.write(json.dumps(parsed_data))
        #file.close()
        #console.print_json(data=parsed_data)

        self.clear_series_data()  # Clear existing series data
        self.options_list = split_options(self.options)

        # Extract the episodes from the parsed data of the selected series
        if parsed_data and "programme_episodes" in parsed_data and not SINGLE:
            try:
                series_name = (
                    parsed_data["programme_episodes"]["programme"]["title"]
                    or "unknownTitle"
                )
            except KeyError:
                print(f"No data found for {url}")
                return
            except Exception:
                print(f"No episodes currently available for {url}")
                print("Try again but be sure to use the series utmost top level url!")
                return

            # Go through each episode in the selected series and add it to the series data
            episodes = parsed_data.get("programme_episodes").get("elements")
            for item in episodes:
                try:
                    series_no = item["subtitle"].split(":")[0].split(" ")[1]
                    if int(series_no):
                        pass
                    else:
                        continue
                    episode = {
                        "series_no": series_no,
                        # 'title' is episode number here, some services use descriptive text
                        "title": item["subtitle"].split(":")[
                            1
                        ],  # .split(' ')[-1] or '01',
                        "url": "https://www.bbc.co.uk/iplayer/episode/" + item["id"],
                        "synopsis": item["synopses"]["small"] or None,
                    }
                except Exception:
                    try:
                        episode = {
                            "series_no": 100,  # special or one-off'
                            # 'title' is episode number here, some services use descriptive text
                            "title": item["subtitle"],  # could be date
                            "url": "https://www.bbc.co.uk/iplayer/episode/"
                            + item["id"],
                            "synopsis": item["synopses"]["small"] or None,
                        }
                    except KeyError as e:
                        print(f"Error: {e}")
                        continue  # Skip any episode that doesn't have the required information
                self.add_episode(series_name, episode)

        # Single episode  expedite download
        else:
            series_name = parsed_data["episodes"][0]["title"]
            url = f"https://www.bbc.co.uk/iplayer/episode/{parsed_data['episodes'][0]['id']}/{parsed_data['episodes'][0]['title']}"
            # check for UHD content
            for hlg_item in self.uhd_list:
                if series_name.lower() in hlg_item:
                    self.AVAILABLE_HLG = True
                    break
            # self.options_list = split_options(self.options)
            try:
                if BbcLoader.HLG and self.AVAILABLE_HLG:
                    command = (
                        [
                            'uv', 'run', 'envied',
                            "dl",
                            *self.options_list,
                            "--range",
                            "HLG",
                            "iP",
                            url.replace(" ", "-"),
                        ]
                    )
                else:
                    command = (['uv', 'run', 'envied', "dl", *self.options_list, "iP", url.replace(" ", "-")])

                self.runsubprocess(command)
            
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is self.DOWNLOAD_ORCHESTRATOR installed correctly via 'pip install self.DOWNLOAD_ORCHESTRATOR?",
                )
            return

        self.prepare_series_for_episode_selection(
            series_name
        )  # creates list of series; allows user selection of wanted series prepares an episode list over chosen series
        selected_final_episodes = self.display_final_episode_list(
            self.final_episode_data
        )

        # specific to BBC
        # self.options_list = split_options(self.options)
        for item in selected_final_episodes:
            # check for UHD content
            for hlg_item in self.uhd_list:
                if series_name.lower() in hlg_item:
                    self.AVAILABLE_HLG = True
                    break
            for part in item.split(","):
                if "https" in part:
                    url = part.strip()
                    break

            if BbcLoader.HLG and self.AVAILABLE_HLG:
                command = (
                    ['uv', 'run', 'envied', "dl", *self.options_list, "--range", "HLG", "iP", url.replace(" ", "-")]
                )
            else:
                command = (['uv', 'run', 'envied', "dl", *self.options_list, "iP", url.replace(" ", "-")])
            self.runsubprocess(command)
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

        req = self.client.get(browse_url, headers=self.headers)
        init_data = extract_params_json(req.content.decode(), "__IPLAYER_REDUX_STATE__")

        """
        # for bug fixing
        console.print_json(data=init_data)
        file = open("init_data.json", "w")
        file.write(json.dumps(init_data))
        file.close()
        """

        myjson = init_data
        # console.print_json(data=myjson)
        category = myjson["bundles"][0]["id"]

        res = jmespath.search(
            """
            bundles[*].entities[*].episode[].{
                href: id,
                label: title.default,
                overlaytext: synopsis.small
            }
            """,
            myjson,
        )
        # console.print_json(data=res)

        seen_titles = set()  # To track seen titles
        beaupylist = list()

        # Build the beaupylist for display but check for duplicates
        for i, item in enumerate(res):
            label = item["label"]  # Title of the programme
            overlaytext = item["overlaytext"]  # Synopsis of the programme
            formatted_item = f"{i} {label}\n\t{overlaytext}"

            if (
                label not in seen_titles
            ):  # Check if the title has already been processed
                seen_titles.add(label)
                beaupylist.append(formatted_item)

        found = self.display_beaupylist(beaupylist)
        if found:
            if category not in [
                "films",
                "featured-category-films",
                "featured-films",
                "film",
                "movie",
            ]:
                # extract search_term for greedy search
                search_term = found.split("\n\t")[0][2:]
                return self.process_received_url_from_category(search_term)
            else:
                ind = found.split(" ")[0]
                url = res[int(ind)]["href"]
                # url may be for series or single Film
                url = f"https://www.bbc.co.uk/iplayer/episode/{url}"
                #url = url.encode("utf-8", "ignore").decode().strip()  # has spaces!
                

                # process short-cut download or do greedy search on url
                return self.process_received_url_from_category(url)

        else:
            print("No video selected.")
            return
        
    