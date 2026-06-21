"""
Module 2: RSC CAP Bulk Downloader
Downloads CAP PDFs for each factory with resume capability.
"""

import csv
import logging
import time
import urllib3
from datetime import datetime
from pathlib import Path

import requests
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)


class CAPDownloader:
    """Downloads Corrective Action Plan PDFs from RSC/Accord."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.accord_base = self.config["rsc"]["accord_cap_base"]
        self.output_dir = Path(self.config["paths"]["raw_data"]) / "caps"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_file = self.output_dir / "manifest.csv"
        self.input_file = Path(self.config["paths"]["raw_data"]) / "rsc_factories.csv"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.config["scraping"]["user_agent"],
        })
        self.delay = self.config["scraping"]["delay_between_requests"]
        self.max_retries = self.config["scraping"]["max_retries"]
        self.timeout = self.config["scraping"]["timeout"]

        self.downloaded = 0
        self.failed = 0
        self.skipped = 0
        self.errors = []

    def _load_existing(self) -> set:
        """Load already downloaded files to enable resume."""
        existing = set()
        for pdf_file in self.output_dir.glob("*.pdf"):
            existing.add(pdf_file.stem)
        return existing

    def _load_factories(self) -> list:
        """Load factory list from CSV."""
        factories = []
        if not self.input_file.exists():
            logger.error(f"Input file not found: {self.input_file}")
            return factories

        with open(self.input_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                factories.append(row)
        return factories

    def _construct_url(self, factory: dict) -> str:
        """Build CAP download URL for a factory."""
        # Use direct URL if available
        direct_url = factory.get("cap_download_url", "").strip()
        if direct_url and direct_url.startswith("http"):
            return direct_url

        # Construct from Accord pattern
        rsc_id = factory.get("rsc_id", "").strip()
        if rsc_id:
            return f"{self.accord_base}?id={rsc_id}"

        return ""

    def _download_pdf(self, url: str, filepath: Path, retries: int = 0) -> bool:
        """Download a single PDF with retry and exponential backoff."""
        try:
            time.sleep(self.delay)
            resp = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True, verify=False)

            if resp.status_code == 404:
                logger.warning(f"404 Not Found: {url}")
                return False
            if resp.status_code == 403:
                logger.warning(f"403 Forbidden: {url}")
                return False
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                if retries < self.max_retries:
                    return self._download_pdf(url, filepath, retries + 1)
                return False

            resp.raise_for_status()

            # Verify it's a PDF
            content_type = resp.headers.get("Content-Type", "")
            if "pdf" not in content_type.lower():
                # Check first bytes for PDF signature
                first_bytes = next(resp.iter_content(4))
                if not first_bytes.startswith(b"%PDF"):
                    logger.warning(f"Non-PDF content from {url} (type: {content_type})")
                    return False
                # Reset iterator
                resp = self.session.get(url, timeout=self.timeout, stream=True, verify=False)
                time.sleep(self.delay)

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            return True

        except requests.RequestException as e:
            if retries < self.max_retries:
                wait = 2 ** retries
                logger.warning(f"Retry {retries+1} for {url} after {wait}s: {e}")
                time.sleep(wait)
                return self._download_pdf(url, filepath, retries + 1)
            self.errors.append(f"Failed {url}: {e}")
            return False

    def _write_manifest(self, entries: list):
        """Write download manifest CSV."""
        fieldnames = [
            "rsc_id", "factory_name", "url", "filename",
            "status", "timestamp", "error",
        ]
        with open(self.manifest_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for entry in entries:
                writer.writerow(entry)

    def run(self, uploader: GitHubUploader = None) -> dict:
        """Execute the CAP downloader."""
        logger.info("Starting CAP downloader")

        factories = self._load_factories()
        if not factories:
            logger.warning("No factories to process")
            return self._get_stats()

        existing = self._load_existing()
        logger.info(f"Found {len(factories)} factories, {len(existing)} already downloaded")

        manifest_entries = []
        if self.manifest_file.exists():
            with open(self.manifest_file, "r", encoding="utf-8") as f:
                manifest_entries = list(csv.DictReader(f))

        for factory in factories:
            rsc_id = factory.get("rsc_id", "").strip() or factory.get("name", "unknown").replace(" ", "_")
            filename = f"{rsc_id}.pdf"
            filepath = self.output_dir / filename

            # Resume: skip if already downloaded
            if rsc_id in existing and filepath.exists():
                logger.debug(f"Skipping {filename} - already exists")
                self.skipped += 1
                continue

            url = self._construct_url(factory)
            if not url:
                logger.warning(f"No URL for factory {rsc_id}")
                self.failed += 1
                manifest_entries.append({
                    "rsc_id": rsc_id,
                    "factory_name": factory.get("name", ""),
                    "url": "",
                    "filename": filename,
                    "status": "no_url",
                    "timestamp": datetime.now().isoformat(),
                    "error": "No download URL available",
                })
                continue

            logger.info(f"Downloading CAP for {rsc_id}...")
            success = self._download_pdf(url, filepath)

            entry = {
                "rsc_id": rsc_id,
                "factory_name": factory.get("name", ""),
                "url": url,
                "filename": filename,
                "status": "downloaded" if success else "failed",
                "timestamp": datetime.now().isoformat(),
                "error": "" if success else (self.errors[-1] if self.errors else "Unknown"),
            }
            manifest_entries.append(entry)

            if success:
                self.downloaded += 1
            else:
                self.failed += 1

        # Write manifest
        self._write_manifest(manifest_entries)
        logger.info(f"Downloaded: {self.downloaded}, Failed: {self.failed}, Skipped: {self.skipped}")

        # Upload to GitHub
        if uploader:
            uploader.upload_directory(
                str(self.output_dir),
                "data/raw/caps",
                "cap_downloader",
            )

        return self._get_stats()

    def _get_stats(self) -> dict:
        return {
            "module": "cap_downloader",
            "downloaded": self.downloaded,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors[:10],
            "manifest": str(self.manifest_file) if self.manifest_file.exists() else None,
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("cap_downloader")
    downloader = CAPDownloader()
    stats = downloader.run()
    print(stats)
