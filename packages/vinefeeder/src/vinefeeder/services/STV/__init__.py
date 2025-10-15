from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import extract_script_with_id_json, parse_json, split_options
from rich.console import Console
import jmespath
import re

console = Console()


class StvLoader(BaseLoader):
    options = ""

    def __init__(self):
        headers = {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            "Origin": "https://player.stv.tv",
            "Referer": "https://player.stv.tv/",
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
            StvLoader.options = opts
        self.options_list = split_options(StvLoader.options)
        # direct download

        if "http" in search_term and inx == 1:
            try:
                self.options_list = split_options(StvLoader.options)
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "STV", search_term]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "STV", search_term]
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
            # example:
            # https://player.stv.tv/summary/joan
            # need a search keyword(s) from url
            # split and select series name
            if not 'https' in search_term:
                return self.fetch_videos(search_term)
            # need a search keyword(s) from category url
            # split and select series name
            else:
                search_term = search_term.split("/")[-1]
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
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Origin": "https://player.stv.tv",
            "Referer": "https://player.stv.tv/",
            "Host": "search-api.swiftype.com",
            "Access-Control-Request-Method": "POST",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
        }
        url = "https://search-api.swiftype.com/api/v1/public/engines/search.json"

        response = self.get_options(url, headers=headers)

        xdata = response["x-request-id"]
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Origin": "https://player.stv.tv",
            "Referer": "https://player.stv.tv/",
            "Host": "search-api.swiftype.com",
            "Access-Control-Request-Method": "POST",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
            "x-request-id": f"{xdata}",
        }
        json = {
            "engine_key": "S1jgssBHdk8ZtMWngK_y",
            "per_page": 100,
            "page": 1,
            "fetch_fields": {"page": ["title", "body", "resultDescriptionTx", "url"]},
            "highlight_fields": {"page": {"title": {"size": 100, "fallback": True}}},
            "search_fields": {"page": ["title^3", "body", "category", "sections"]},
            "q": search_term,
            "spelling": "strict",
        }

        response = self.post_data(url, headers=headers, json=json)
        parsed_data = response.json()  #
        mydata = parsed_data["records"]["page"]

        for item in mydata:
            title = item["title"]
            episode = {
                "title": item["title"],
                "url": item["url"],
                "synopsis": item["resultDescriptionTx"],
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
        Or if given a direct url, fetch it and process for greedy download.
        The function will prepare the series data for episode selection and display the final episode list.
        It will return the URLs for the selected episodes.
        """
        if "https" in selected:  # direct url provided so skip url preparation
            url = selected
        else:
            url = self.get_selected_url(selected)
        try:
            myhtml = self.get_data(url=url)
        except Exception:
            print(f"No valid data at {url} found.\n Exiting")
            return
        parsed_data = extract_script_with_id_json(myhtml, "__NEXT_DATA__", 0)
        self.clear_series_data()  # Clear existing series data

        '''console.print_json(data=parsed_data) # for debugging
        f = open("1stv.json",'w')
        f.write(json.dumps(parsed_data))
        f.close()'''

        series_data = parsed_data["props"]["pageProps"]["data"]["programmeHeader"][
            "name"
        ]
        tabs = len(parsed_data["props"]["pageProps"]["data"]["tabs"])
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0',
            'Accept': '*/*',
            'Accept-Language': 'en-GB,en;q=0.5',
            # 'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Referer': 'https://player.stv.tv/',
            'Stv-Drm': 'true',
            'Stv-PremierSportsAware': 'true',
            'Origin': 'https://player.stv.tv',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'Priority': 'u=4',
            'Cache-Control': 'max-age=0',
        }
        # STV mostly provde one series' worth of episodes in one request
        # tabs are populated with series_guid to allow for multiple series calls
        # some tabs are not series and only have a programme.guid for example:-
        # props►pageProps►data►tabs►0►params►query►programme.guid
        # test here


        try:
            PROGGUID = parsed_data["props"]["pageProps"]["data"]["programmeData"]["guid"]
        except Exception:
            PROGGUID = None

        if PROGGUID:
        
            for item in parsed_data["props"]["pageProps"]["data"]["tabs"][0]["data"]:
                
                try:
                    series_no = parsed_data["props"]["pageProps"]["data"]["tabs"][0]["title"]
                    if "Episode" in series_no:
                        series_no = 100
                    else:
                        series_no = int(series_no.split(" ")[1])
                    title = item["title"]
                    url = f"https://player.stv.tv{item['link']}"  # https://player.stv.tv/episode/4nlk/loose-women
                    synopsis = item["summary"]

                    episode = {
                        "series_no": series_no,
                        "title": title.replace(
                            ", ", "-"
                        ),  # data with comma messes up later when selecting url
                        "url": url,
                        "synopsis": synopsis,
                    }
                    self.add_episode_remove_duplicates(series_data, episode)                    

                except KeyError as e:
                    print(f"Error: {e}")

        if tabs > 1:
            for index in range(1, tabs):
                # last few tabs may not contain series so check
                if (
                    "Autoplay"
                    in parsed_data["props"]["pageProps"]["data"]["tabs"][index]["title"]
                    or "Trailer"
                    in parsed_data["props"]["pageProps"]["data"]["tabs"][index]["title"]
                ):
                    break

                series_guid = parsed_data["props"]["pageProps"]["data"]["tabs"][index][
                    "params"
                ]["query"]["series.guid"]

                response = self.get_data(
                    f"https://player.api.stv.tv/v1/episodes?series.guid={series_guid}&limit=100&groupToken=0071",
                    headers=headers,
                )
                next_parsed_data = parse_json(response)

                '''console.print_json(data=next_parsed_data)
                f = open("2stv.json",'w')
                f.write(json.dumps(next_parsed_data))
                f.close()'''

                for item in next_parsed_data["results"]:
                    try:
                        series_no = item["playerSeries"]["name"]
                        title = item["title"]
                        url = item["_permalink"]
                        synopsis = item["summary"]

                        episode = {
                            "series_no": int(series_no.replace("Series ", "")),
                            "title": title,
                            "url": url,  #
                            "synopsis": synopsis,
                        }

                        self.add_episode_remove_duplicates(series_data, episode)

                    except KeyError as e:
                        print(f"Error: {e}")

        self.prepare_series_for_episode_selection(
            series_data
        )  # creates list of series;
        selected_final_episodes = self.display_final_episode_list(
            self.final_episode_data
        )
        self.options_list = split_options(self.options)
        for item in selected_final_episodes:
            url = item.split(",")[2].lstrip()
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "STV", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "STV", url]
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is the package installed correctly?",
                )
        return None

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
            init_data = extract_script_with_id_json(req, "__NEXT_DATA__", 0)

            """console.print_json(data=init_data)
            f = open("cat_stv.json",'w')
            f.write(json.dumps(init_data))
            f.close()"""
            # Extract brand items

            myjson = jmespath.search("props.pageProps.data.assets", init_data)

            # jmespath is an efficient json parser that searches complex json
            # and, in this case, produces a simple dict from which
            # res(ults) are more easily extracted.
            # STV specific

            res = jmespath.search(
                """
                [].{
                    label: title,
                    overlaytext: description,
                    href: link
                    }
                    """,
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
            url = f"https://player.stv.tv{res[int(ind)]['href']}"
            # url may be for series or single Film
            url = url.encode("utf-8", "ignore").decode().strip()  # if has spaces!

            # STV is unusual in that some urls from Documentary or Films are not valid
            # for download but only for browsing.
            # Check if url has an identifier,
            # if not, get it by calling receive() with index = 3 (greedy search)

            test_id = url.split("/")[4]  # look for identifier ~4hgf
            if re.match(r"[0-9]+", test_id) and len(test_id) == 4:
                pass
            else:
                return self.receive(3, url)
            # if film/doc then process url and download

            # process short-cut download or do greedy search on url
            return self.process_received_url_from_category(url)

        else:
            print("No video selected.")
            return
    