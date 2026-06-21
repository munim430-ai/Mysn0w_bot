"""
Module 4: Brand Supplier Scraper
Scrapes public supplier lists from major apparel brands.
"""

import csv
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)


class BrandScraper:
    """Scrapes supplier lists from brand websites."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.brands = self.config["brands"]
        self.output_dir = Path(self.config["paths"]["raw_data"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output_dir / "brand_suppliers.csv"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.config["scraping"]["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        self.delay = self.config["scraping"]["delay_between_requests"]
        self.max_retries = self.config["scraping"]["max_retries"]
        self.timeout = self.config["scraping"]["timeout"]

        self.supplier_count = 0
        self.error_count = 0
        self.parse_failures = []

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
                logger.warning(f"Retry {retries+1} for {url} after {wait}s: {e}")
                time.sleep(wait)
                return self._fetch_page(url, retries + 1)
            self.parse_failures.append(f"Failed to fetch {url}: {e}")
            logger.error(f"Max retries for {url}: {e}")
            return ""

    def _extract_suppliers_from_html(self, html: str, brand_name: str, base_url: str) -> list:
        """Extract supplier data from HTML using multiple strategies."""
        suppliers = []
        soup = BeautifulSoup(html, "html.parser")

        # Strategy 1: Look for structured data (JSON-LD)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    for item in data:
                        supplier = self._parse_jsonld_item(item, brand_name)
                        if supplier:
                            suppliers.append(supplier)
                elif isinstance(data, dict):
                    supplier = self._parse_jsonld_item(data, brand_name)
                    if supplier:
                        suppliers.append(supplier)
            except (json.JSONDecodeError, TypeError):
                pass

        if suppliers:
            return suppliers

        # Strategy 2: HTML tables
        tables = soup.find_all("table")
        for table in tables:
            rows = table.select("tbody tr") if table.select("tbody") else table.find_all("tr")
            for row in rows[1:]:  # Skip header
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    name = cells[0].get_text(strip=True)
                    address = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                    country = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                    product = cells[3].get_text(strip=True) if len(cells) > 3 else ""

                    if name and len(name) > 2:
                        suppliers.append({
                            "brand_name": brand_name,
                            "factory_name": name,
                            "address": address,
                            "country": country,
                            "product_category": product,
                            "source_url": base_url,
                            "scraped_at": datetime.now().isoformat(),
                        })

        if suppliers:
            return suppliers

        # Strategy 3: Structured divs / lists
        container = soup.select_one(".supplier-list, .factory-list, .manufacturing-list, [class*='supplier']")
        if container:
            items = container.select(".supplier-item, .factory-item, .item, li, .row")
            for item in items:
                name_el = item.select_one(".name, .factory-name, .supplier-name, h3, h4, strong")
                addr_el = item.select_one(".address, .location, .country")
                prod_el = item.select_one(".product, .category, .type")

                name = name_el.get_text(strip=True) if name_el else item.get_text(strip=True)[:100]
                address = addr_el.get_text(strip=True) if addr_el else ""
                product = prod_el.get_text(strip=True) if prod_el else ""

                # Extract country from address
                country = self._extract_country(address)

                if name and len(name) > 2:
                    suppliers.append({
                        "brand_name": brand_name,
                        "factory_name": name,
                        "address": address,
                        "country": country,
                        "product_category": product,
                        "source_url": base_url,
                        "scraped_at": datetime.now().isoformat(),
                    })

        # Strategy 4: Generic fallback - any element with factory-like content
        if not suppliers:
            for el in soup.find_all(["div", "li", "tr"]):
                text = el.get_text(strip=True)
                if any(k in text.lower() for k in ["garment", "factory", "manufacturing", "textile", "bd", "bangladesh"]):
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    if lines and len(lines[0]) > 2:
                        suppliers.append({
                            "brand_name": brand_name,
                            "factory_name": lines[0][:200],
                            "address": "; ".join(lines[1:3]) if len(lines) > 1 else "",
                            "country": self._extract_country(text),
                            "product_category": "",
                            "source_url": base_url,
                            "scraped_at": datetime.now().isoformat(),
                        })
                if len(suppliers) > 500:  # Safety limit
                    break

        return suppliers

    def _parse_jsonld_item(self, item: dict, brand_name: str) -> dict:
        """Parse a JSON-LD structured data item."""
        if not isinstance(item, dict):
            return None

        item_type = item.get("@type", "").lower()
        if "organization" in item_type or "place" in item_type or "localbusiness" in item_type:
            name = item.get("name", "")
            address_obj = item.get("address", {})
            if isinstance(address_obj, dict):
                address = address_obj.get("streetAddress", "")
                country = address_obj.get("addressCountry", "")
            else:
                address = str(address_obj)
                country = ""

            if name:
                return {
                    "brand_name": brand_name,
                    "factory_name": name,
                    "address": address,
                    "country": country,
                    "product_category": item.get("description", ""),
                    "source_url": "",
                    "scraped_at": datetime.now().isoformat(),
                }
        return None

    def _extract_country(self, address: str) -> str:
        """Extract country from address string."""
        if not address:
            return ""
        # Check for Bangladesh
        if any(k in address.lower() for k in ["bangladesh", " dhaka", "gazipur", "narayanganj", "chittagong"]):
            return "Bangladesh"
        # Common countries
        countries = ["China", "India", "Vietnam", "Turkey", "Pakistan", "Cambodia", "Myanmar", "Indonesia", "Sri Lanka"]
        for c in countries:
            if c.lower() in address.lower():
                return c
        return ""

    def _handle_csv_download(self, url: str, brand_name: str) -> list:
        """Try to download and parse CSV supplier lists."""
        suppliers = []
        csv_url = None

        # Common CSV URL patterns
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        potential_urls = [
            urljoin(base, "/suppliers.csv"),
            urljoin(base, "/factories.csv"),
            urljoin(base, "/api/suppliers"),
        ]

        for csv_url in potential_urls:
            try:
                time.sleep(self.delay)
                resp = self.session.get(csv_url, timeout=self.timeout)
                if resp.status_code == 200 and "csv" in resp.headers.get("Content-Type", "").lower():
                    lines = resp.text.strip().split("\n")
                    reader = csv.DictReader(lines)
                    for row in reader:
                        name = row.get("Factory", row.get("Name", row.get("factory", row.get("name", ""))))
                        if name:
                            suppliers.append({
                                "brand_name": brand_name,
                                "factory_name": name,
                                "address": row.get("Address", row.get("address", "")),
                                "country": row.get("Country", row.get("country", "")),
                                "product_category": row.get("Product", row.get("product", "")),
                                "source_url": csv_url,
                                "scraped_at": datetime.now().isoformat(),
                            })
                    if suppliers:
                        break
            except Exception:
                continue

        return suppliers

    def _scrape_with_playwright(self, url: str, brand_name: str) -> list:
        """Use Playwright for JS-heavy sites (interactive maps, etc.)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("Playwright not available")
            return []

        suppliers = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers({
                    "User-Agent": self.config["scraping"]["user_agent"],
                })

                logger.info(f"Playwright navigating: {url}")
                page.goto(url, wait_until="networkidle", timeout=30000)

                # Wait for dynamic content
                time.sleep(3)

                # Try to interact with filters if present
                try:
                    # Look for Bangladesh filter
                    bangladesh_btn = page.locator("text=Bangladesh, [data-country='BD'], [value='Bangladesh']").first
                    if bangladesh_btn.is_visible():
                        bangladesh_btn.click()
                        time.sleep(2)
                except:
                    pass

                # Handle map-based sites (Nike, etc.)
                try:
                    markers = page.locator("[class*='marker'], [class*='pin'], [class*='factory']").all()
                    for marker in markers[:100]:  # Limit
                        try:
                            marker.click()
                            time.sleep(0.5)
                            popup_html = page.content()
                            popup_suppliers = self._extract_suppliers_from_html(popup_html, brand_name, url)
                            suppliers.extend(popup_suppliers)
                        except:
                            continue
                except:
                    pass

                if not suppliers:
                    html = page.content()
                    suppliers = self._extract_suppliers_from_html(html, brand_name, url)

                browser.close()

        except Exception as e:
            logger.error(f"Playwright error for {brand_name}: {e}")
            self.parse_failures.append(f"{brand_name} Playwright: {e}")

        return suppliers

    def run(self, uploader: GitHubUploader = None) -> dict:
        """Execute brand scraper for all configured brands."""
        logger.info(f"Starting brand scraper for {len(self.brands)} brands")
        all_suppliers = []

        for brand in self.brands:
            brand_name = brand["name"]
            url = brand["url"]
            logger.info(f"Scraping {brand_name}: {url}")

            # Try static HTML first
            html = self._fetch_page(url)
            suppliers = []

            if html:
                suppliers = self._extract_suppliers_from_html(html, brand_name, url)

            # Try CSV download
            if not suppliers:
                suppliers = self._handle_csv_download(url, brand_name)

            # Fallback to Playwright for JS-heavy sites
            if not suppliers:
                logger.info(f"Trying Playwright for {brand_name}...")
                suppliers = self._scrape_with_playwright(url, brand_name)

            if suppliers:
                all_suppliers.extend(suppliers)
                self.supplier_count += len(suppliers)
                logger.info(f"  -> {len(suppliers)} suppliers from {brand_name}")
            else:
                self.error_count += 1
                self.parse_failures.append(f"{brand_name}: No suppliers extracted")
                logger.warning(f"  -> No suppliers from {brand_name} (parse failure logged)")

        # Write output
        if all_suppliers:
            self._write_csv(all_suppliers)
            logger.info(f"Saved {len(all_suppliers)} suppliers to {self.output_file}")

            if uploader:
                uploader.upload_file(
                    str(self.output_file),
                    "data/raw/brand_suppliers.csv",
                    "brand_scraper",
                )
        else:
            logger.warning("No suppliers extracted from any brand")

        return self._get_stats()

    def _write_csv(self, suppliers: list):
        """Write suppliers to CSV."""
        fieldnames = [
            "brand_name", "factory_name", "address", "country",
            "product_category", "source_url", "scraped_at",
        ]
        with open(self.output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for s in suppliers:
                row = {k: s.get(k, "") for k in fieldnames}
                writer.writerow(row)

    def _get_stats(self) -> dict:
        return {
            "module": "brand_scraper",
            "suppliers_extracted": self.supplier_count,
            "brands_failed": self.error_count,
            "parse_failures": self.parse_failures,
            "output_file": str(self.output_file) if self.output_file.exists() else None,
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("brand_scraper")
    scraper = BrandScraper()
    stats = scraper.run()
    print(stats)
