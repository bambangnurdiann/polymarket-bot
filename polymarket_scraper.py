import logging
import os
import re
import time
from datetime import datetime
from typing import Optional, Dict, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


# ───────────────── CONFIG ─────────────────

POLYMARKET_BASE_URL = "https://polymarket.com/event"
WINDOW_DURATION = 300
SCRAPER_CACHE_TTL = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


# ───────────────── CACHE ─────────────────

class Cache:
    def __init__(self):
        self.data: Dict[str, Tuple[float, float]] = {}

    def get(self, coin):
        v = self.data.get(coin)
        if not v:
            return None
        price, ts = v
        if time.time() - ts > SCRAPER_CACHE_TTL:
            return None
        return price

    def set(self, coin, price):
        self.data[coin] = (price, time.time())


cache = Cache()


# ───────────────── HELPERS ─────────────────

def build_urls(coin):
    now = time.time()
    coin = coin.lower()

    urls = []
    for offset in [0, -14400, -18000]:
        base = int((now + offset) // WINDOW_DURATION) * WINDOW_DURATION
        for d in [0, 300, -300]:
            urls.append(f"{POLYMARKET_BASE_URL}/{coin}-updown-5m-{base+d}")

    return list(dict.fromkeys(urls))


def parse_price(text):
    m = re.search(r"\$([0-9,]+(?:\.\d+)?)", text)
    if m:
        val = float(m.group(1).replace(",", ""))
        if 5000 < val < 500000:
            return val
    return None


# ───────────────── SCRAPER ─────────────────

class PolymarketScraper:

    def __init__(self, headless=False):
        self.headless = headless
        self.driver = None

    def init_driver(self):
        opts = Options()

        if self.headless:
            opts.add_argument("--headless=new")

        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")

        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts
        )

    def navigate(self, coin):
        urls = build_urls(coin)

        for url in urls:
            try:
                logger.info(f"[TRY] {url}")
                self.driver.get(url)
                time.sleep(2)

                html = self.driver.page_source.lower()
                current = self.driver.current_url.lower()

                if coin.lower() in current and "price to beat" in html:
                    logger.info("[OK] MARKET FOUND")
                    return True

            except Exception:
                continue

        self.screenshot("NAV_FAIL", coin)
        return False

    def extract(self):
        try:
            elements = self.driver.find_elements("xpath", "//*[contains(text(),'Price to Beat')]")

            for el in elements:
                try:
                    parent = el.find_element("xpath", "./..")
                    text = parent.text

                    price = parse_price(text)
                    if price:
                        logger.info(f"[FOUND] {price}")
                        return price
                except:
                    pass

            # fallback regex
            html = self.driver.page_source
            m = re.search(r'Price to Beat[^$]{0,50}\$([0-9,]+(?:\.\d+)?)', html)
            if m:
                val = float(m.group(1).replace(",", ""))
                logger.info(f"[FOUND REGEX] {val}")
                return val

        except Exception as e:
            logger.error(e)

        return None

    def get_price(self, coin="BTC"):

        cached = cache.get(coin)
        if cached:
            logger.info(f"[CACHE] {cached}")
            return cached

        if not self.driver:
            self.init_driver()

        if not self.navigate(coin):
            return None

        price = self.extract()

        if price:
            cache.set(coin, price)
            logger.info(f"[SAVE] {price}")
            return price

        self.screenshot("EXTRACT_FAIL", coin)
        return None

    def screenshot(self, tag, coin):
        try:
            os.makedirs("logs/screenshots", exist_ok=True)
            path = f"logs/screenshots/{tag}_{coin}_{int(time.time())}.png"
            self.driver.save_screenshot(path)
            logger.info(f"[SHOT] {path}")
        except:
            pass

    def close(self):
        if self.driver:
            self.driver.quit()


# ───────────────── RUNNER ─────────────────

if __name__ == "__main__":
    scraper = PolymarketScraper(headless=False)

    print("\n=== TEST BTC ===\n")

    price = scraper.get_price("BTC")

    if price:
        print(f"\n✅ RESULT: {price}")
    else:
        print("\n❌ FAILED")

    input("\nPress Enter to exit...")
    scraper.close()