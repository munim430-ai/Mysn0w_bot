"""
Module 1: RSC Factory List Scraper

Scrapes https://rsc-bd.org/factories/ — a JS-rendered WordPress page that
loads factory cards into #factory-data via the FFC API.

Key logic:
- Each factory card has 4 inspection-report icons: Fire, Structural, Electrical, Boiler
- A real PDF link  → href starts with "https://"
- A missing report → href="javascript:void(0)"
- boiler_missing = True  means the factory is a sales lead for boiler upgrades
"""

import csv
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)

FIELDNAMES = [
    "factory_name",
    "remediation_status",
    "safety_training",
    "workers_count",
    "progress_rate_pct",
    "fire_pdf_url",
    "structural_pdf_url",
    "electrical_pdf_url",
    "boiler_pdf_url",
    "boiler_missing",
    "cap_url",
    "scraped_at",
]


def _parse_card(card) -> dict:
    """Extract all fields from a single .card div."""
    record = {f: "" for f in FIELDNAMES}
    record["scraped_at"] = datetime.now().isoformat()

    # Factory name
    h5 = card.select_one("h5.card-title")
    record["factory_name"] = h5.get_text(strip=True) if h5 else ""

    # Remediation status and safety training — look for <p> with label text
    for p in card.select("p"):
        txt = p.get_text(" ", strip=True)
        strong = p.find("strong")
        value = strong.get_text(strip=True) if strong else ""
        if "Remediation Status" in txt:
            record["remediation_status"] = value
        elif "Safety Training" in txt:
            record["safety_training"] = value

    # Workers and progress — the two fw-bold <p> tags in the right column
    for p in card.select("p.fw-bold"):
        txt = p.get_text(strip=True).lstrip(": ").strip()
        if "%" in txt:
            record["progress_rate_pct"] = txt.replace("%", "").strip()
        elif txt.isdigit():
            record["workers_count"] = txt

    # Inspection report PDFs — 4 icons in .d-flex.flex-row
    icon_row = card.select_one("div.d-flex.flex-row.gap-2")
    if icon_row:
        for a in icon_row.select("a"):
            href = a.get("href", "")
            img = a.find("img")
            title = img.get("title", "") if img else ""
            pdf_url = href if href.startswith("http") else None

            if "Fire" in title:
                record["fire_pdf_url"] = pdf_url or ""
            elif "Structural" in title:
                record["structural_pdf_url"] = pdf_url or ""
            elif "Electrical" in title:
                record["electrical_pdf_url"] = pdf_url or ""
            elif "Boiler" in title:
                record["boiler_pdf_url"] = pdf_url or ""
                record["boiler_missing"] = "True" if pdf_url is None else "False"

    # CAP download link
    cap = card.find("a", string=lambda s: s and "CAP" in s)
    record["cap_url"] = cap["href"] if cap else ""

    return record


def _parse_page_html(html: str) -> list[dict]:
    """Parse all factory cards from a page's HTML snippet."""
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("#factory-data .card")
    return [_parse_card(c) for c in cards if c.select_one("h5.card-title")]


class RSCScraper:
    """Scrapes the RSC Bangladesh factory list via Playwright."""

    URL = "https://rsc-bd.org/factories/"

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.output_dir = Path(self.config["paths"]["raw_data"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output_dir / "rsc_factories.csv"
        self.leads_file = self.output_dir / "boiler_leads.csv"

    def run(self, uploader: "GitHubUploader | None" = None) -> dict:
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"

        factories = self._scrape_with_playwright()

        stats = {
            "module": "rsc_scraper",
            "total": len(factories),
            "boiler_missing": sum(1 for f in factories if f.get("boiler_missing") == "True"),
            "boiler_present": sum(1 for f in factories if f.get("boiler_missing") == "False"),
            "output_file": None,
            "leads_file": None,
        }

        if not factories:
            logger.warning("No factories scraped — check site availability")
            return stats

        self._write_csv(self.output_file, factories, FIELDNAMES)
        logger.info(f"Saved {len(factories)} factories → {self.output_file}")

        leads = [f for f in factories if f.get("boiler_missing") == "True"]
        self._write_csv(self.leads_file, leads, FIELDNAMES)
        logger.info(f"Saved {len(leads)} boiler leads → {self.leads_file}")

        stats["output_file"] = str(self.output_file)
        stats["leads_file"] = str(self.leads_file)

        if uploader:
            uploader.upload_file(str(self.output_file), "data/raw/rsc_factories.csv", "rsc_scraper")
            uploader.upload_file(str(self.leads_file), "data/raw/boiler_leads.csv", "rsc_scraper")

        return stats

    def _scrape_with_playwright(self) -> list[dict]:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        factories = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (compatible; RSCBot/1.0)"})

            logger.info(f"Loading {self.URL}")
            try:
                page.goto(self.URL, wait_until="networkidle", timeout=60_000)
            except PWTimeout:
                logger.warning("Page load timed out, continuing with partial load")

            # Set per-page to 100 so we have fewer paginations
            try:
                page.select_option("#perPage", "100")
                page.wait_for_selector("#factory-data .card", timeout=15_000)
                time.sleep(2)
            except PWTimeout:
                logger.warning("perPage selector not found, using default page size")

            page_num = 1
            while True:
                # Wait for cards to be visible
                try:
                    page.wait_for_selector("#factory-data .card", timeout=15_000)
                except PWTimeout:
                    logger.error(f"Cards not found on page {page_num}")
                    break

                html = page.inner_html("#factory-data")
                # Wrap in the container ID so selectors work
                page_factories = _parse_page_html(f'<div id="factory-data">{html}</div>')
                factories.extend(page_factories)
                logger.info(f"Page {page_num}: scraped {len(page_factories)} factories (total so far: {len(factories)})")

                # Check if Next button is enabled
                next_btn = page.query_selector("button#next-btn")
                if not next_btn:
                    logger.info("No next button found — done")
                    break

                is_disabled = next_btn.get_attribute("disabled")
                if is_disabled is not None:
                    logger.info("Next button disabled — reached last page")
                    break

                next_btn.click()
                time.sleep(2)  # wait for JS to render new page
                page_num += 1

                # Safety cap
                if page_num > 200:
                    logger.warning("Hit 200-page safety cap")
                    break

            browser.close()

        logger.info(f"Scraping complete: {len(factories)} factories across {page_num} pages")
        return factories

    @staticmethod
    def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("rsc_scraper")
    scraper = RSCScraper()
    stats = scraper.run()
    print(stats)
