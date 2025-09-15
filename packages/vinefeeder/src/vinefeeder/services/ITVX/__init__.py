from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import rinse, split_options, extract_script_with_id_json
from rich.console import Console
import jmespath
import re


console = Console()


class ItvxLoader(BaseLoader):
    options = ""

    def __init__(self):
        headers = {
            "Accept": "*/*",
            "user-agent": "Dalvik/2.9.8 (Linux; U; Android 9.9.2; ALE-L94 Build/NJHGGF)",
            "Origin": "https://www.itv.com",
            "Referer": "https://www.itv.com/",
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
            ItvxLoader.options = opts
        self.options_list = split_options(ItvxLoader.options)
        # direct download
        if "http" in search_term and inx == 1:
            self.options_list = split_options(self.options)
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "ITVX", search_term]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "ITVX", search_term]
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
        """Fetch videos from ITV using a search term."""
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Host": "textsearch.prd.oasvc.itv.com",
            "Origin": "https://www.itv.com",
            "Referer": "https://www.itv.com/",
            "user-agent": "Dalvik/2.9.8 (Linux; U; Android 9.9.2; ALE-L94 Build/NJHGGF)",
        }
        url = f"https://textsearch.prd.oasvc.itv.com/search?broadcaster=itv&channelType=simulcast&featureSet=clearkey,outband-webvtt,hls,aes,playready,widevine,fairplay,bbts,progressive,hd,rtmpe&platform=dotcom&pretx=true&query={search_term}&size=24"
        html = self.get_data(url, headers)
        parsed_data = self.parse_data(html)

        # select only from FREE tier
        res = jmespath.search(
            """
        results[?data.tier=='FREE'].{
        api1: data.legacyId.officialFormat,
        title: data.[programmeTitle, filmTitle, specialTitle],
        synopsis: data.synopsis
        } """,
            parsed_data,
        )

        for item in res:
            api1 = item["api1"].replace("/", "a")
            title = next((value for value in item["title"] if value is not None), "")
            title = title.replace(" ", "-")
            url = f"https://www.itv.com/watch/{title}/{api1}"

            episode = {
                "title": item.get("title", "Unknown Title"),
                "url": url,
                "synopsis": item.get("synopsis", "No synopsis available."),
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
        if "https" in selected:  # direct url provided skip url preparation
            url = selected
        else:
            url = self.get_selected_url(selected)
        try:
            headers = {
                "authority": "www.itv.com",
                "user-agent": "Dalvik/2.9.8 (Linux; U; Android 9.9.2; ALE-L94 Build/NJHGGF)",
            }
            myhtml = self.get_data(url=url, headers=headers)
        except Exception:
            print(f"No valid data at {url} found.\n Exiting")
            return
        parsed_data = extract_script_with_id_json(myhtml, "__NEXT_DATA__", 0)
        self.clear_series_data()  # Clear existing series data

        """
        # debug
        console.print_json(data=parsed_data)
        f = open('itv.json', 'w')
        f.write(myhtml)
        f.close()"""

        url_data = jmespath.search(
            """
        query.{
        programmeSlug: programmeSlug
        programmeId: programmeId                                               
        }""",
            parsed_data,
        )
        programmeSlug = url_data["programmeSlug"]
        programmeId = url_data["programmeId"]

        mytitles = jmespath.search("props.pageProps", parsed_data)
        res = jmespath.search(
            """
        seriesList[].titles[].{
        episode: episode
        eptitle: episodeTitle
        series: series            
        magniurl: playlistUrl
        description: description
        episodeId: episodeId
        letterA: encodedEpisodeId.letterA
        contentInfo: contentInfo
        channel: channel
  
        } """,
            mytitles,
        )

        for item in res:
            try:
                # ITVX sometimes has a word in place of a series number, correct for this.
                series_no = 100  # set default
                if item["series"]:  # check it is a digir
                    try:
                        series_no = int(item["series"])  # Try to cast to int
                    except ValueError:
                        pass  # Leave series_no as 100 if casting fails

                episode = {
                    "series_no": series_no,
                    "title": f"{item['episode'] or ''}:{item['eptitle'] or ''}",
                    "url": f"https://www.itv.com/watch/{programmeSlug}/{programmeId}/{item['letterA']}",
                    "synopsis": rinse(item["description"]) or None,
                }
            except KeyError:
                continue  # Skip any episode that doesn't have the required information

            self.add_episode(programmeSlug, episode)

        self.prepare_series_for_episode_selection(
            programmeSlug
        )  # creates list of series; allows user selection of wanted series prepares an episode list over chosen series
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
                    command = ['uv', 'run', 'envied', "dl", "ITVX", url]
                else:
                    command = ['uv','run', 'envied', "dl", *self.options_list, "ITVX", url]
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
        Fetches videos from a category (ITVX specific).
        Args:
            browse_url (str): URL of the category page.
        Returns:
            None
        """
        beaupylist = []  # hold beaupy data for display and programme selection
        headers = {
            "host": "www.itv.com",
            "user-agent": "Dalvik/2.9.8 (Linux; U; Android 9.9.2; ALE-L94 Build/NJHGGF)",
        }
        try:
            myhtml = self.get_data(browse_url, headers=headers)

            parsed_data = extract_script_with_id_json(myhtml, "__NEXT_DATA__", 0)

            # jmespath is an efficient json parser that searches complex json
            # and, in this case, produces a simple dict from which
            # res(ults) are more easily extracted.
            # ITVX specific
            mytitles = jmespath.search("props.pageProps.collection.shows", parsed_data)
            res = jmespath.search(
                """
                [].{
                    title: titleSlug,
                    programmeId: encodedProgrammeId.letterA,
                    episodeId: encodedEpisodeId.letterA,
                    synopsis: description
                }
            """,
                mytitles,
            )

            # Add the URL to each entry in res
            # ITV does not have a url in json data thus need to build.
            for entry in res:
                entry["url"] = (
                    f"https://www.itv.com/watch/{entry['title']}/{entry['programmeId']}/{entry['episodeId']}"
                )

            # Build the beaupylist for display
            for i, item in enumerate(res):
                title = (item["title"].replace("-", " ")).title()
                url = item["url"]
                synopsis = item["synopsis"]
                beaupylist.append(
                    f"{i} {title.replace('_',' ')}\n\t{synopsis}"
                )  # \t used to split text later

        except Exception as e:
            print(f"Error fetching category data: {e}")
            return

        # call function in BaseLoader
        found = self.display_beaupylist(beaupylist)

        if found:
            ind = found.split(" ")[0]
            url = res[int(ind)]["url"]
            # url may be for series or single Film
            url = url.encode("utf-8", "ignore").decode().strip()  # has spaces!

            # process short-cut download or do greedy search on url
            return self.process_received_url_from_category(url)

        else:
            print("No video selected.")
            return
        
    