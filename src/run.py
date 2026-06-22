#!/usr/bin/env python3
"""
Main Orchestrator for Bangladesh RMG Factory Intelligence Pipeline

Usage:
    python src/run.py              # Run all modules
    python src/run.py --module 1   # Run specific module
    python src/run.py --module 1,2,3  # Run modules 1-3
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from logger import setup_logger
from github_uploader import GitHubUploader
from rsc_scraper import RSCScraper
from cap_downloader import CAPDownloader
from cap_parser import CAPParser
from brand_scraper import BrandScraper
from bgmea_scraper import BGMEAScraper
from merger import DataMerger
from scorer import PriorityScorer
from app_exporter import AppExporter

logger = logging.getLogger(__name__)

MODULES = {
    1: ("rsc_scraper", RSCScraper),
    2: ("cap_downloader", CAPDownloader),
    3: ("cap_parser", CAPParser),
    4: ("brand_scraper", BrandScraper),
    5: ("bgmea_scraper", BGMEAScraper),
    6: ("merger", DataMerger),
    7: ("scorer", PriorityScorer),
    8: ("app_exporter", AppExporter),
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Bangladesh RMG Factory Intelligence Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python src/run.py                    Run all modules in sequence
  python src/run.py --module 1         Run only RSC scraper
  python src/run.py --module 1,2       Run modules 1 and 2
  python src/run.py --module 1-3       Run modules 1 through 3
  python src/run.py --skip-upload      Run without uploading to GitHub
  python src/run.py --dry-run          Run without uploading to GitHub
        """,
    )
    parser.add_argument(
        "--module",
        type=str,
        default="",
        help="Module(s) to run: 1, 1,2, 1-3, etc. (default: all)",
    )
    parser.add_argument(
        "--skip-upload",
        "--dry-run",
        action="store_true",
        help="Skip GitHub uploads (dry run mode)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    return parser.parse_args()


def parse_module_selection(selection: str) -> list:
    """Parse module selection string into list of module numbers."""
    if not selection:
        return list(MODULES.keys())

    modules = set()
    for part in selection.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            modules.update(range(int(start), int(end) + 1))
        else:
            modules.add(int(part))

    # Validate
    valid = sorted([m for m in modules if m in MODULES])
    invalid = sorted([m for m in modules if m not in MODULES])
    if invalid:
        logger.warning(f"Ignoring invalid module numbers: {invalid}")

    return valid


def run_pipeline(selected_modules: list, config_path: str, skip_upload: bool = False) -> dict:
    """Run selected pipeline modules."""
    start_time = time.time()

    # Setup
    logger.info("=" * 60)
    logger.info("Bangladesh RMG Factory Intelligence Pipeline")
    logger.info(f"Started: {datetime.now().isoformat()}")
    logger.info(f"Modules: {selected_modules}")
    logger.info(f"GitHub upload: {'disabled' if skip_upload else 'enabled'}")
    logger.info("=" * 60)

    # Initialize GitHub uploader
    uploader = None
    if not skip_upload:
        try:
            uploader = GitHubUploader(config_path)
            if uploader.ensure_branch():
                logger.info(f"GitHub branch '{uploader.branch}' ready")
            else:
                logger.warning("GitHub branch setup failed - uploads will be skipped")
                uploader = None
        except Exception as e:
            logger.error(f"GitHub uploader init failed: {e}")
            uploader = None

    # Run modules
    results = {}
    for module_num in selected_modules:
        name, cls = MODULES[module_num]
        logger.info("")
        logger.info("-" * 50)
        logger.info(f"Module {module_num}: {name}")
        logger.info("-" * 50)

        module_start = time.time()
        try:
            instance = cls(config_path)
            stats = instance.run(uploader=uploader)
            elapsed = time.time() - module_start
            stats["elapsed_seconds"] = round(elapsed, 2)
            results[name] = stats
            logger.info(f"Completed in {elapsed:.1f}s: {stats}")
        except Exception as e:
            elapsed = time.time() - module_start
            logger.error(f"Module {module_num} failed after {elapsed:.1f}s: {e}", exc_info=True)
            results[name] = {
                "status": "error",
                "error": str(e),
                "elapsed_seconds": round(elapsed, 2),
            }

    # Final report
    total_time = time.time() - start_time
    logger.info("")
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info(f"Total time: {total_time:.1f}s")
    logger.info(f"Modules run: {len(selected_modules)}")
    logger.info(f"Successful: {sum(1 for r in results.values() if 'error' not in r)}")
    logger.info(f"Failed: {sum(1 for r in results.values() if 'error' in r)}")
    logger.info("=" * 60)

    # Write report JSON
    report = {
        "timestamp": datetime.now().isoformat(),
        "total_time_seconds": round(total_time, 2),
        "modules_run": selected_modules,
        "results": results,
    }

    report_file = Path("logs") / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_file.parent.mkdir(exist_ok=True)
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"Report saved to {report_file}")

    # Upload report
    if uploader:
        uploader.upload_file(str(report_file), f"logs/{report_file.name}", "pipeline_report")

    return report


if __name__ == "__main__":
    args = parse_args()

    # Setup logging
    setup_logger("pipeline")

    # Parse module selection
    selected = parse_module_selection(args.module)
    if not selected:
        logger.error("No valid modules selected")
        sys.exit(1)

    # Run
    report = run_pipeline(selected, args.config, args.skip_upload)

    # Exit with error code if any module failed
    has_errors = any("error" in r for r in report["results"].values())
    sys.exit(1 if has_errors else 0)
