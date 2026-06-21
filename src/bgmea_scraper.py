"""
Module 5: BGMEA Directory Scraper
Scrapes bgmea.com.bd member directory with anti-scraping handling.
"""

import csv
import logging
import re
import time
import urllib3
from datetime import datetime
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)


class BGMEAScraper:
    """Scrapes BGMEA member factory directory."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.base_url = self.config["bgmea"]["base_url"]
        self.member_path = self.config["bgmea"]["member_list_path"]
        self.output_dir = Path(self.config["paths"]["raw_data"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output_dir / "bgmea_factories.csv"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.config["scraping"]["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
        })
        self.delay = self.config["scraping"]["delay_between_requests"]
        self.max_retries = self.config["scraping"]["max_retries"]
        self.timeout = self.config["scraping"]["timeout"]

        self.factory_count = 0
        self.error_count = 0
        self.anti_scraping_triggered = False
        self.errors = []

    def _fetch_page(self, url: str, retries: int = 0) -> str:
        """Fetch page with retry and anti-scraping evasion."""
        try:
            time.sleep(self.delay)
            resp = self.session.get(url, timeout=self.timeout, verify=False)

            # Check for anti-scraping measures
            if resp.status_code == 403:
                logger.warning("403 Forbidden - possible anti-scraping triggered")
                self.anti_scraping_triggered = True
                return ""
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 120))
                logger.warning(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                if retries < self.max_retries:
                    return self._fetch_page(url, retries + 1)
                return ""

            # Check for Cloudflare/captcha
            if "cf-browser-verification" in resp.text or "challenge-platform" in resp.text:
                logger.warning("Cloudflare challenge detected")
                self.anti_scraping_triggered = True
                return ""
            if "captcha" in resp.text.lower():
                logger.warning("CAPTCHA detected")
                self.anti_scraping_triggered = True
                return ""

            resp.raise_for_status()
            return resp.text

        except requests.RequestException as e:
            if retries < self.max_retries:
                wait = 2 ** retries
                logger.warning(f"Retry {retries+1} for {url} after {wait}s: {e}")
                time.sleep(wait)
                return self._fetch_page(url, retries + 1)
            self.errors.append(f"{url}: {e}")
            return ""

    def _extract_factories_from_html(self, html: str) -> list:
        """Extract factory data from BGMEA HTML."""
        factories = []
        soup = BeautifulSoup(html, "html.parser")

        # Try table-based layout
        rows = soup.select("table tbody tr, .member-row, .directory-item")
        if not rows:
            # Try card-based layout
            rows = soup.select(".member-card, .factory-card, .company-item, .listing-item")

        for row in rows:
            factory = self._parse_factory_row(row)
            if factory and factory.get("factory_name"):
                factories.append(factory)

        return factories

    def _parse_factory_row(self, row) -> dict:
        """Parse a single factory entry."""
        factory = {
            "factory_name": "",
            "address": "",
            "phone": "",
            "email": "",
            "website": "",
            "contact_person": "",
            "member_type": "",
            "source_url": self.base_url,
            "scraped_at": datetime.now().isoformat(),
        }

        # Table cells
        cells = row.select("td")
        if len(cells) >= 2:
            factory["factory_name"] = cells[0].get_text(strip=True)
            factory["address"] = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            factory["phone"] = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            factory["email"] = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            factory["member_type"] = cells[4].get_text(strip=True) if len(cells) > 4 else ""

            # Look for website link
            for link in row.find_all("a", href=True):
                href = link["href"]
                if href.startswith("http"):
                    if "mailto:" in href:
                        factory["email"] = href.replace("mailto:", "")
                    elif "tel:" in href:
                        factory["phone"] = href.replace("tel:", "")
                    else:
                        factory["website"] = href

        # Div-based layout
        if not factory["factory_name"]:
            name_el = row.select_one(".company-name, .factory-name, .name, h3, h4, .title, a")
            if name_el:
                factory["factory_name"] = name_el.get_text(strip=True)

            addr_el = row.select_one(".address, .location, [class*='address']")
            if addr_el:
                factory["address"] = addr_el.get_text(strip=True)

            phone_el = row.select_one(".phone, .tel, [class*='phone']")
            if phone_el:
                factory["phone"] = phone_el.get_text(strip=True)

            email_el = row.select_one(".email, [href^='mailto']")
            if email_el:
                if email_el.name == "a" and email_el.get("href", "").startswith("mailto:"):
                    factory["email"] = email_el["href"].replace("mailto:", "")
                else:
                    factory["email"] = email_el.get_text(strip=True)

            contact_el = row.select_one(".contact, .contact-person, .person")
            if contact_el:
                factory["contact_person"] = contact_el.get_text(strip=True)

            type_el = row.select_one(".member-type, .type, .category")
            if type_el:
                factory["member_type"] = type_el.get_text(strip=True)

        # Extract email with regex if not found
        if not factory["email"]:
            text = row.get_text()
            email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
            if email_match:
                factory["email"] = email_match.group(0)

        # Extract phone with regex
        if not factory["phone"]:
            text = row.get_text()
            phone_match = re.search(r"[\d+\-()\s]{7,}", text)
            if phone_match:
                factory["phone"] = phone_match.group(0).strip()

        return factory

    def _scrape_with_playwright(self) -> list:
        """Use Playwright for JS-rendered or protected content."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("Playwright not available")
            return []

        factories = []
        url = f"{self.base_url}{self.member_path}"

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers({
                    "User-Agent": self.config["scraping"]["user_agent"],
                })

                logger.info(f"Playwright navigating: {url}")
                page.goto(url, wait_until="networkidle", timeout=30000)

                # Handle possible login modal
                try:
                    close_btn = page.locator(".close, .modal-close, [aria-label='Close']").first
                    if close_btn.is_visible():
                        close_btn.click()
                        time.sleep(1)
                except:
                    pass

                # Scroll through infinite scroll if present
                last_height = page.evaluate("document.body.scrollHeight")
                for _ in range(30):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2)
                    new_height = page.evaluate("document.body.scrollHeight")
                    if new_height == last_height:
                        break
                    last_height = new_height

                html = page.content()
                browser.close()

                factories = self._extract_factories_from_html(html)

        except Exception as e:
            logger.error(f"Playwright error: {e}")
            self.errors.append(f"Playwright: {e}")

        return factories

    def run(self, uploader: GitHubUploader = None) -> dict:
        """Execute BGMEA scraper with fallback strategies."""
        logger.info(f"Starting BGMEA scraper: {self.base_url}")

        url = f"{self.base_url}{self.member_path}"
        factories = []

        # Strategy 1: Static request
        html = self._fetch_page(url)
        if html:
            factories = self._extract_factories_from_html(html)
            logger.info(f"Static scrape: {len(factories)} factories")

        # Strategy 2: Playwright for JS-rendered content
        if not factories and not self.anti_scraping_triggered:
            logger.info("Trying Playwright...")
            factories = self._scrape_with_playwright()
            logger.info(f"Playwright: {len(factories)} factories")

        # Strategy 3: If blocked, create fallback parser stub
        if not factories:
            logger.warning("BGMEA scraping blocked or failed. Creating fallback parser.")
            self.anti_scraping_triggered = True
            self._create_fallback_template()

        # Write output
        if factories:
            self.factory_count = len(factories)
            self._write_csv(factories)
            logger.info(f"Saved {len(factories)} factories to {self.output_file}")

            if uploader:
                uploader.upload_file(
                    str(self.output_file),
                    "data/raw/bgmea_factories.csv",
                    "bgmea_scraper",
                )
        else:
            logger.warning("No factories extracted from BGMEA")

        return self._get_stats()

    def _create_fallback_template(self):
        """Create a manual import template when scraping fails."""
        template_file = self.output_dir / "bgmea_manual_template.csv"
        fieldnames = [
            "factory_name", "address", "phone", "email", "website",
            "contact_person", "member_type", "source_url", "scraped_at",
        ]
        with open(template_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({
                "factory_name": "EXAMPLE FACTORY LTD",
                "address": "123 Factory Road, Gazipur, Dhaka, Bangladesh",
                "phone": "+880-2-1234567",
                "email": "info@example.com",
                "website": "www.example.com",
                "contact_person": "Mr. Example",
                "member_type": "Ordinary Member",
                "source_url": "",
                "scraped_at": datetime.now().isoformat(),
            })
        logger.info(f"Created manual import template: {template_file}")
        logger.info("Download BGMEA member list manually and replace this file")

    def _write_csv(self, factories: list):
        """Write factories to CSV."""
        fieldnames = [
            "factory_name", "address", "phone", "email", "website",
            "contact_person", "member_type", "source_url", "scraped_at",
        ]
        with open(self.output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for factory in factories:
                row = {k: factory.get(k, "") for k in fieldnames}
                writer.writerow(row)

    def _get_stats(self) -> dict:
        return {
            "module": "bgmea_scraper",
            "factories_extracted": self.factory_count,
            "anti_scraping_triggered": self.anti_scraping_triggered,
            "errors": self.errors[:10],
            "output_file": str(self.output_file) if self.output_file.exists() else None,
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("bgmea_scraper")
    scraper = BGMEAScraper()
    stats = scraper.run()
    print(stats)
