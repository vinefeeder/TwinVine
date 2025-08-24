from playwright.sync_api import sync_playwright

def fetch_uhd_links():
    url = "https://www.bbc.co.uk/iplayer/help/questions/programme-availability/uhd-content"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)
        page.wait_for_timeout(5000)  # Wait for content to load

        links = page.locator("a").all()
        uhd_links = []
        for link in links:
            href = link.get_attribute('href')
            if href and '/iplayer/episodes/' in href:
                uhd_links.append(href)

        browser.close()
        return list(set(uhd_links))

print(fetch_uhd_links())
