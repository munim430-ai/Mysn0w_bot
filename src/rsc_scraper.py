"""
Module 1: RSC Factory List Scraper
Scrapes https://rsc-bd.org/factories/ with pagination support.
"""

import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)

# Common Bangladesh districts for parsing
DISTRICTS = [
    "Dhaka", "Gazipur", "Narayanganj", "Chittagong", "Khulna",
    "Rajshahi", "Sylhet", "Barisal", "Rangpur", "Mymensingh",
    "Comilla", "Tangail", "Kushtia", "Jessore", "Dinajpur",
    "Faridpur", "Bogra", "Pabna", "Rangamati", "Bandarban",
    "Narsingdi", "Manikganj", "Munshiganj", "Gopalganj",
    "Shariatpur", "Madaripur", "Rajbari", "Kishoreganj",
    "Netrokona", "Jamalpur", "Sherpur", "Naogaon", "Natore",
    "Nawabganj", "Sirajganj", "Joypurhat", "Gaibandha",
    "Kurigram", "Lalmonirhat", "Nilphamari", "Panchagarh",
    "Thakurgaon", "Habiganj", "Moulvibazar", "Sunamganj",
    "Brahmanbaria", "Chandpur", "Feni", "Khagrachhari",
    "Lakshmipur", "Noakhali", "Bagerhat", "Chuadanga",
    "Jhenaidah", "Magura", "Meherpur", "Narail", "Satkhira",
]


class RSCScraper:
    """Scrapes the RSC Bangladesh factory list."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.base_url = self.config["rsc"]["base_url"]
        self.output_dir = Path(self.config["paths"]["raw_data"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output_dir / "rsc_factories.csv"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.config["scraping"]["user_agent"],
        })
        self.delay = self.config["scraping"]["delay_between_requests"]
        self.max_retries = self.config["scraping"]["max_retries"]
        self.timeout = self.config["scraping"]["timeout"]

        self.success_count = 0
        self.failure_count = 0
        self.errors = []

    def _fetch_page(self, url: str, retries: int = 0) -> str:
        """Fetch page with retry logic."""
        try:
            time.sleep(self.delay)
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            if retries < self.max_retries:
                wait = 2 ** retries
                logger.warning(f"Retry {retries+1}/{self.max_retries} for {url} after {wait}s: {e}")
                time.sleep(wait)
                return self._fetch_page(url, retries + 1)
            self.errors.append(f"Failed to fetch {url}: {e}")
            logger.error(f"Max retries exceeded for {url}: {e}")
            return ""

    def _parse_district(self, address: str) -> str:
        """Extract district from address string."""
        if not address:
            return ""
        for district in DISTRICTS:
            if district.lower() in address.lower():
                return district
        # Try regex for "District: XXX" pattern
        match = re.search(r"[Dd]istrict[:\s]+([A-Za-z]+)", address)
        if match:
            return match.group(1)
        return ""

    def _extract_factories_from_html(self, html: str) -> list:
        """Parse factory data from HTML."""
        factories = []
        soup = BeautifulSoup(html, "html.parser")

        # Try multiple selector patterns
        rows = soup.select("table tbody tr")
        if not rows:
            rows = soup.select(".factory-row, .factory-item, [data-factory]")
        if not rows:
            # Generic fallback - look for structured divs
            rows = soup.select("div[class*='factory'], div[class*='item']")

        logger.info(f"Found {len(rows)} potential factory rows")

        for row in rows:
            try:
                factory = self._parse_factory_row(row)
                if factory and factory.get("name"):
                    factories.append(factory)
                    self.success_count += 1
                else:
                    self.failure_count += 1
            except Exception as e:
                self.failure_count += 1
                self.errors.append(f"Parse error: {e}")

        return factories

    def _parse_factory_row(self, row) -> dict:
        """Parse a single factory row/element."""
        factory = {
            "rsc_id": "",
            "name": "",
            "address": "",
            "district": "",
            "remediation_status": "",
            "cap_progress_percent": "",
            "safety_training_status": "",
            "cap_download_url": "",
            "workers_count": "",
            "scraped_at": datetime.now().isoformat(),
        }

        # Try table cells
        cells = row.select("td")
        if len(cells) >= 2:
            factory["name"] = cells[0].get_text(strip=True)
            factory["address"] = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            if len(cells) > 2:
                factory["remediation_status"] = cells[2].get_text(strip=True)
            if len(cells) > 3:
                progress_text = cells[3].get_text(strip=True)
                progress_match = re.search(r"(\d+)", progress_text)
                if progress_match:
                    factory["cap_progress_percent"] = progress_match.group(1)

        # Try div-based layout
        if not factory["name"]:
            name_el = row.select_one(".factory-name, .name, h3, h4, .title")
            if name_el:
                factory["name"] = name_el.get_text(strip=True)

            addr_el = row.select_one(".address, .location, [class*='address']")
            if addr_el:
                factory["address"] = addr_el.get_text(strip=True)

            status_el = row.select_one(".status, .remediation, [class*='status']")
            if status_el:
                factory["remediation_status"] = status_el.get_text(strip=True)

        # Look for links
        for link in row.find_all("a", href=True):
            href = link["href"]
            if "cap" in href.lower() or "download" in href.lower():
                factory["cap_download_url"] = href if href.startswith("http") else f"https://rsc-bd.org{href}"
            if href.startswith("http") and "rsc-bd.org" in href:
                # Extract ID from URL
                id_match = re.search(r"/(\d+)/?$", href)
                if id_match and not factory["rsc_id"]:
                    factory["rsc_id"] = id_match.group(1)

        # Parse district from address
        factory["district"] = self._parse_district(factory["address"])

        # Extract workers count if present
        workers_match = re.search(r"(\d+)\s*(?:workers?|employees?)", row.get_text(), re.I)
        if workers_match:
            factory["workers_count"] = workers_match.group(1)

        return factory

    def _detect_pagination(self, html: str) -> list:
        """Detect pagination and return list of page URLs."""
        soup = BeautifulSoup(html, "html.parser")
        pages = [self.base_url]

        # Look for pagination links
        pagination = soup.select(".pagination a, .page-link, [class*='page']")
        seen = set()

        for link in pagination:
            href = link.get("href", "")
            if href and href not in seen:
                seen.add(href)
                full_url = href if href.startswith("http") else f"https://rsc-bd.org{href}"
                if full_url not in pages:
                    pages.append(full_url)

        # Check for infinite scroll / JS-rendered content
        if not pagination and "load more" in html.lower():
            logger.info("Possible infinite scroll detected - consider using Playwright")

        return pages

    def run(self, uploader: GitHubUploader = None) -> dict:
        """Execute the scraper and return statistics."""
        logger.info(f"Starting RSC scraper: {self.base_url}")

        # Fetch main page
        html = self._fetch_page(self.base_url)
        if not html:
            return self._get_stats()

        # Detect pagination
        page_urls = self._detect_pagination(html)
        logger.info(f"Detected {len(page_urls)} page(s) to scrape")

        all_factories = []
        for url in page_urls:
            if url != self.base_url:
                html = self._fetch_page(url)
            if html:
                factories = self._extract_factories_from_html(html)
                all_factories.extend(factories)
                logger.info(f"Extracted {len(factories)} factories from {url}")

        # Handle JS-rendered sites with Playwright fallback
        if not all_factories:
            logger.info("No factories found with static parsing, trying Playwright...")
            all_factories = self._scrape_with_playwright()

        # Write CSV
        if all_factories:
            self._write_csv(all_factories)
            logger.info(f"Saved {len(all_factories)} factories to {self.output_file}")

            # Upload to GitHub
            if uploader:
                uploader.upload_file(
                    str(self.output_file),
                    "data/raw/rsc_factories.csv",
                    "rsc_scraper",
                )
        else:
            logger.warning("No factories extracted - site structure may have changed")

        return self._get_stats()

    def _scrape_with_playwright(self) -> list:
        """Fallback: Use Playwright for JS-rendered content."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("Playwright not installed, cannot handle JS-rendered content")
            return []

        factories = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers({
                    "User-Agent": self.config["scraping"]["user_agent"],
                })

                logger.info(f"Navigating with Playwright: {self.base_url}")
                page.goto(self.base_url, wait_until="networkidle", timeout=30000)

                # Handle infinite scroll
                last_height = page.evaluate("document.body.scrollHeight")
                scroll_attempts = 0
                max_scrolls = 50

                while scroll_attempts < max_scrolls:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2)
                    new_height = page.evaluate("document.body.scrollHeight")
                    if new_height == last_height:
                        break
                    last_height = new_height
                    scroll_attempts += 1

                # Also check for "Load More" buttons
                for _ in range(20):
                    try:
                        load_more = page.locator("text=Load More, text=Show More, text=View More").first
                        if load_more.is_visible():
                            load_more.click()
                            time.sleep(2)
                        else:
                            break
                    except:
                        break

                html = page.content()
                browser.close()

                factories = self._extract_factories_from_html(html)
                logger.info(f"Playwright extracted {len(factories)} factories")

        except Exception as e:
            logger.error(f"Playwright error: {e}")
            self.errors.append(f"Playwright: {e}")

        return factories

    def _write_csv(self, factories: list):
        """Write factories to CSV."""
        if not factories:
            return
        fieldnames = [
            "rsc_id", "name", "address", "district", "remediation_status",
            "cap_progress_percent", "safety_training_status", "cap_download_url",
            "workers_count", "scraped_at",
        ]
        with open(self.output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for factory in factories:
                row = {k: factory.get(k, "") for k in fieldnames}
                writer.writerow(row)

    def _get_stats(self) -> dict:
        """Return scrape statistics."""
        return {
            "module": "rsc_scraper",
            "success": self.success_count,
            "failed": self.failure_count,
            "errors": self.errors[:10],  # Limit errors
            "output_file": str(self.output_file) if self.output_file.exists() else None,
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("rsc_scraper")
    scraper = RSCScraper()
    stats = scraper.run()
    print(stats)
