from vinefeeder.base_loader import BaseLoader
from vinefeeder.parsing_utils import split_options
from rich.console import Console
import json
from beaupy import select_multiple

console = Console()


def prettify(my_list):
    """
    Formats a list of three elements into a styled string using rich text formatting.

    Parameters:
    my_list (list): A list containing three elements to be formatted.

    Returns:
    str: A formatted string where the first two elements are displayed in green
         and the third element is displayed in cyan on a new line with indentation.
    """
    my_beaupystring = []
    one = my_list[0]
    two = my_list[1]
    three = my_list[2]

    my_beaupystring = f"[green]{one}, {two}[/green][cyan]\n\t{three}[/cyan]"
    return my_beaupystring

class TptvLoader(BaseLoader):
    options = ""

    def __init__(self):
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0',
            'Accept': '*/*',
            'Accept-Language': 'en-GB,en;q=0.5',
            'api-key': 'zq5pyPd0RTbNg3Fyj52PrkKL9c2Af38HHh4itgZTKDaCzjAyhd',
            'Referer': 'https://tptvencore.co.uk/',
            'tenant': 'encore',
            'Content-Type': 'application/json',
            'Origin': 'https://tptvencore.co.uk',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'cross-site',
            'Priority': 'u=0',
            }
        super().__init__(headers)
        payload = {}
        r = self.post_data("https://prod.suggestedtv.com/api/client/v1/session", json=payload, headers=headers)
        if r.status_code != 200:
            raise ConnectionError   
        else:
            self.session_id = r.json()['id']
      

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
            TptvLoader.options = opts
        self.options_list = split_options(TptvLoader.options)
        # direct download

        if "http" in search_term and inx == 1:
            try:
                self.options_list = split_options(TptvLoader.options)
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "TPTV", search_term]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "TPTV", search_term]
                self.runsubprocess(command)  # url
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    f"Is {self.DOWNLOAD_ORCHESTRATOR} installed correctly via 'pip install {self.DOWNLOAD_ORCHESTRATOR})?",
                )

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
        
        suggested_url = f"https://prod.suggestedtv.com/api/client/v2/search/{search_term}"



        suggested_headers = {
        'Accept': '*/*',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
        'Connection': 'keep-alive',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Origin': 'https://tptvencore.co.uk',
        'Referer': 'https://tptvencore.co.uk/',
        }

        get_headers = suggested_headers.copy()
        get_headers['Access-Control-Request-Headers'] = 'session,tenant'
        get_headers['Access-Control-Request-Method'] = 'GET'   
        get_headers['session'] = self.session_id
        get_headers['tenant'] = 'encore'                                                                         
        response = self.get_data(suggested_url, headers=get_headers)
        myjson = json.loads(response)
        
        myitems = []       
        for item in myjson['data']:
            if  item.startswith('collection_'):
                id = item.replace('collection_','')
                collection_url = f"https://prod.suggestedtv.com/api/client/v1/collection/by-reference/{id}?extend=label"
                response = self.get_data(collection_url, headers=get_headers)
                if response:
                    data = json.loads(response)
                    for item in data['children']:
                        myitems.append(item['id'].replace('product_',''))
            elif item.startswith('product_'):
                myitems.append(item.replace('product_',''))
        mystring = ",".join(myitems)
            
        continued_search_url = "https://prod.suggestedtv.com/api/client/v1/product?ids=" + mystring + "&extend=label"
        
        response = self.get_data(continued_search_url, headers=get_headers)
        data = json.loads(response)
        try:
            for item in data['data']:
                vid_id = item['reference']
                title = item['name']
                synopsis = item['description'].replace('\n', ' ')
                if len(synopsis) > 300:
                    synopsis = f"{synopsis[:300]}  ...snip"
                episode = {
                    "title": title,
                    "url": vid_id,
                    "synopsis": synopsis,
                }
                self.add_episode(title,episode)
        except Exception as e:
            if len(self.series_data) > 0:
                pass
            else:
                print("Nothing found. Try another search term.")
            return
        eps = self.get_series()
        beaupylist = []
        for entry in eps.values():
            if entry and isinstance(entry[0], dict):  # guard clause for safety
                beaupylist.append([entry[0]['title'], entry[0]['synopsis'], entry[0]['url']])
        selected = select_multiple(beaupylist, preprocessor=lambda val: prettify(val),  page_size=6, pagination=True)  

        
        if selected:
            return self.second_fetch(selected)
        return None


    def second_fetch(self, selected):
   
        self.options_list = split_options(self.options)
        for item in selected:
            url = item[2]
            url = f"https://tptvencore.co.uk/product/{url}"
            #print(url)
            try:
                if self.options_list[0] == "":
                    command = ['uv', 'run', 'envied', "dl", "TPTV", url]
                else:
                    command = ['uv', 'run', 'envied', "dl", *self.options_list, "TPTV", url]
                self.runsubprocess(command)
            except Exception as e:
                print(
                    "Error downloading video:",
                    e,
                    "Is devine/envied installed correctly via 'pip install <program name>?",
                )
        return None

    def fetch_videos_by_category(self, browse_url):
        """
        Fetches videos from a category.
        Args:
            browse_url (str): URL of the category page.
        Returns:
            None
        """
        print("Category fetch not Implemented")
        return None
    
