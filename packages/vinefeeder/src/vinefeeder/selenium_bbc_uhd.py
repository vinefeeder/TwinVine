from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import time


def fetch_uhd_links():
    url = "https://www.bbc.co.uk/iplayer/help/questions/programme-availability/uhd-content"

    chrome_options = Options()
    chrome_options.add_argument("--headless")  # Run in headless mode
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1920,1080")

    # Optional: Adjust path to chromedriver if not system-wide
    driver = webdriver.Chrome(options=chrome_options)

    driver.get(url)
    time.sleep(5)  # Wait for dynamic content to load

    links = driver.find_elements(By.XPATH, "//a[contains(@href, '/iplayer/episodes/')]")

    uhd_links = set()
    for link in links:
        href = link.get_attribute("href")
        if href:
            uhd_links.add(href)

    driver.quit()

    return sorted(uhd_links)


if __name__ == "__main__":
    links = fetch_uhd_links()
    for link in links:
        print(link)
