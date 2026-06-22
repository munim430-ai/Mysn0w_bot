"""
Module 1: RSC Factory List Scraper

Scrapes https://rsc-bd.org/factories/ — a JS-rendered WordPress page that
loads factory cards into #factory-data via the FFC API.

Key logic:
- Each factory card has 4 inspection-report icons: Fire, Structural, Electrical, Boiler
- A real PDF link  → href starts with "https://"
- A missing report → href="javascript:void(0)"
- boiler_missing = True  means the factory is a sales lead for boiler upgrades

Compatible with both Google Colab (asyncio loop already running) and plain
Python scripts — uses a ThreadPoolExecutor to isolate the event loop.
"""

import asyncio
import concurrent.futures
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


# ---------------------------------------------------------------------------
# HTML parsing (pure, no Playwright dependency)
# ---------------------------------------------------------------------------

def _parse_card(card) -> dict:
    """Extract all fields from a single .card div."""
    record = {f: "" for f in FIELDNAMES}
    record["scraped_at"] = datetime.now().isoformat()

    h5 = card.select_one("h5.card-title")
    record["factory_name"] = h5.get_text(strip=True) if h5 else ""

    for p in card.select("p"):
        txt = p.get_text(" ", strip=True)
        strong = p.find("strong")
        value = strong.get_text(strip=True) if strong else ""
        if "Remediation Status" in txt:
            record["remediation_status"] = value
        elif "Safety Training" in txt:
            record["safety_training"] = value

    for p in card.select("p.fw-bold"):
        txt = p.get_text(strip=True).lstrip(": ").strip()
        if "%" in txt:
            record["progress_rate_pct"] = txt.replace("%", "").strip()
        elif txt.isdigit():
            record["workers_count"] = txt

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

    cap = card.find("a", string=lambda s: s and "CAP" in s)
    record["cap_url"] = cap["href"] if cap else ""

    return record


def _parse_page_html(html: str) -> list:
    """Parse all factory cards from a page's HTML snippet."""
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("#factory-data .card")
    return [_parse_card(c) for c in cards if c.select_one("h5.card-title")]


# ---------------------------------------------------------------------------
# Async scraping core
# ---------------------------------------------------------------------------

async def _scrape_async(url: str, browser_path: str | None) -> list:
    """Load the RSC factories page and scrape all cards across all pages."""
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    if browser_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browser_path

    factories = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_extra_http_headers(
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )

        logger.info(f"Loading {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PWTimeout:
            logger.warning("Initial page load timed out — continuing anyway")

        # Set per-page to 100 to reduce pagination
        try:
            await page.select_option("#perPage", "100")
        except Exception:
            logger.warning("#perPage selector not found — using default page size")

        page_num = 1
        while True:
            # Wait until #totalCount shows a non-zero number — confirms FFC API responded
            try:
                await page.wait_for_function(
                    "() => { const el = document.getElementById('totalCount');"
                    " return el && /[1-9]/.test(el.innerText); }",
                    timeout=30_000,
                )
            except PWTimeout:
                logger.error(
                    f"Factory data did not load on page {page_num} — "
                    "FFC API may be slow or the site is blocking the request"
                )
                break

            # Then wait for card elements
            try:
                await page.wait_for_selector("#factory-data .card", timeout=15_000)
            except PWTimeout:
                logger.error(f"No .card elements found on page {page_num}")
                break

            inner = await page.inner_html("#factory-data")
            page_factories = _parse_page_html(f'<div id="factory-data">{inner}</div>')
            factories.extend(page_factories)
            logger.info(
                f"Page {page_num}: {len(page_factories)} factories "
                f"(running total: {len(factories)})"
            )

            # Check Next button
            next_btn = await page.query_selector("button#next-btn")
            if not next_btn:
                logger.info("Next button not found — done")
                break
            disabled = await next_btn.get_attribute("disabled")
            if disabled is not None:
                logger.info("Last page reached")
                break

            await next_btn.click()
            # Brief pause so the JS re-renders before we check totalCount again
            await asyncio.sleep(2)
            page_num += 1

            if page_num > 200:
                logger.warning("Hit 200-page safety cap")
                break

        await browser.close()

    logger.info(f"Scraping complete: {len(factories)} factories across {page_num} page(s)")
    return factories


# ---------------------------------------------------------------------------
# Module class
# ---------------------------------------------------------------------------

class RSCScraper:
    """Scrapes the RSC Bangladesh factory list via Playwright (async)."""

    URL = "https://rsc-bd.org/factories/"

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.output_dir = Path(self.config["paths"]["raw_data"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output_dir / "rsc_factories.csv"
        self.leads_file = self.output_dir / "boiler_leads.csv"

    def _resolve_browser_path(self) -> str | None:
        """Return PLAYWRIGHT_BROWSERS_PATH only when the pre-installed dir exists."""
        preinstalled = "/opt/pw-browsers"
        if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
            return os.environ["PLAYWRIGHT_BROWSERS_PATH"]
        if os.path.isdir(preinstalled):
            return preinstalled
        return None

    def run(self, uploader: "GitHubUploader | None" = None) -> dict:
        browser_path = self._resolve_browser_path()
        factories = self._run_async(_scrape_async(self.URL, browser_path))

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

        _write_csv(self.output_file, factories, FIELDNAMES)
        logger.info(f"Saved {len(factories)} factories → {self.output_file}")

        leads = [f for f in factories if f.get("boiler_missing") == "True"]
        _write_csv(self.leads_file, leads, FIELDNAMES)
        logger.info(f"Saved {len(leads)} boiler leads → {self.leads_file}")

        stats["output_file"] = str(self.output_file)
        stats["leads_file"] = str(self.leads_file)

        if uploader:
            uploader.upload_file(str(self.output_file), "data/raw/rsc_factories.csv", "rsc_scraper")
            uploader.upload_file(str(self.leads_file), "data/raw/boiler_leads.csv", "rsc_scraper")

        return stats

    @staticmethod
    def _run_async(coro):
        """
        Run an async coroutine safely regardless of whether an event loop is
        already running (Google Colab / Jupyter) or not (plain Python script).

        Strategy: always spin up a fresh event loop in a background thread so
        we never conflict with an existing loop.
        """
        def _thread_target():
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_thread_target)
            return future.result()


# ---------------------------------------------------------------------------
# CSV helper
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list, fieldnames: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("rsc_scraper")
    scraper = RSCScraper()
    stats = scraper.run()
    print(stats)
