"""
Module 6: Data Merger & Deduplicator
Fuzzy-matches and merges data from RSC, brands, and BGMEA sources.
"""

import csv
import json
import logging
from pathlib import Path

import pandas as pd
import yaml
from rapidfuzz import fuzz, process

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)


class DataMerger:
    """Merges and deduplicates factory data from multiple sources."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.raw_dir = Path(self.config["paths"]["raw_data"])
        self.output_dir = Path(self.config["paths"]["processed_data"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output_dir / "master_database.csv"
        self.unmatched_file = self.output_dir / "unmatched_records.csv"

        # Fuzzy matching thresholds
        self.name_threshold = 85
        self.address_threshold = 70

        self.merged_count = 0
        self.unmatched_count = 0

    def _load_csv(self, filepath: Path) -> pd.DataFrame:
        """Load CSV into DataFrame."""
        if not filepath.exists():
            logger.warning(f"File not found: {filepath}")
            return pd.DataFrame()
        try:
            return pd.read_csv(filepath, encoding="utf-8")
        except Exception as e:
            logger.error(f"Error loading {filepath}: {e}")
            return pd.DataFrame()

    def _normalize_name(self, name: str) -> str:
        """Normalize factory name for matching."""
        if pd.isna(name):
            return ""
        name = str(name).upper().strip()
        # Remove common suffixes
        suffixes = ["LIMITED", "LTD", "PVT", "PRIVATE", "LLC", "INC", "CORP", "CO.", "CO", "."]
        for suffix in suffixes:
            name = name.replace(suffix, "")
        return " ".join(name.split())

    def _find_best_match(self, row: pd.Series, base_df: pd.DataFrame) -> tuple:
        """
        Find best fuzzy match for a factory in the base DataFrame.
        Returns (index, confidence_score) or (None, 0).
        """
        name = self._normalize_name(row.get("factory_name", row.get("name", "")))
        address = str(row.get("address", "")).lower().strip()

        if not name:
            return None, 0

        # Build list of normalized names from base
        base_names = base_df["_norm_name"].tolist()

        # Fuzzy match name
        result = process.extractOne(name, base_names, scorer=fuzz.ratio)
        if not result:
            return None, 0

        match_name, name_score, match_idx = result

        if name_score < self.name_threshold:
            # Try token sort ratio for reordered words
            result2 = process.extractOne(name, base_names, scorer=fuzz.token_sort_ratio)
            if result2:
                _, ts_score, ts_idx = result2
                if ts_score > name_score:
                    name_score = ts_score
                    match_idx = ts_idx

        if name_score < self.name_threshold:
            return None, 0

        # Check address similarity
        base_address = str(base_df.iloc[match_idx].get("address", "")).lower().strip()
        address_score = fuzz.ratio(address, base_address) if address and base_address else 0

        # Combined scoring
        if address and base_address and address_score < self.address_threshold:
            # Name matches but address doesn't - still keep but flag
            confidence = name_score * 0.7
        else:
            confidence = name_score * 0.6 + min(address_score, 100) * 0.4

        return match_idx, round(confidence, 1)

    def _merge_row(self, base_row: pd.Series, match_row: pd.Series, confidence: float) -> dict:
        """Merge two factory records."""
        merged = {}

        # RSC data as base
        rsc_fields = ["rsc_id", "name", "address", "district", "remediation_status",
                      "cap_progress_percent", "safety_training_status", "cap_download_url",
                      "workers_count", "scraped_at"]
        for field in rsc_fields:
            merged[field] = base_row.get(field, "")

        # Add brand data
        brand_name = str(match_row.get("brand_name", "")).strip()
        if brand_name:
            existing_brands = str(merged.get("foreign_buyers", ""))
            if existing_brands:
                try:
                    brands = json.loads(existing_brands)
                except:
                    brands = [existing_brands] if existing_brands else []
            else:
                brands = []
            if brand_name not in brands:
                brands.append(brand_name)
            merged["foreign_buyers"] = json.dumps(brands)

        # Add BGMEA data
        bgmea_fields = {
            "phone": match_row.get("phone", ""),
            "email": match_row.get("email", ""),
            "website": match_row.get("website", ""),
            "contact_person": match_row.get("contact_person", ""),
            "member_type": match_row.get("member_type", ""),
        }
        for key, value in bgmea_fields.items():
            if value and not pd.isna(value):
                merged[key] = value

        merged["match_confidence"] = confidence
        merged["match_source"] = match_row.get("_source", "")
        merged["merged_at"] = pd.Timestamp.now().isoformat()

        return merged

    def run(self, uploader: GitHubUploader = None) -> dict:
        """Execute the merge process."""
        logger.info("Starting data merger")

        # Load all sources
        rsc_df = self._load_csv(self.raw_dir / "rsc_factories.csv")
        brand_df = self._load_csv(self.raw_dir / "brand_suppliers.csv")
        bgmea_df = self._load_csv(self.raw_dir / "bgmea_factories.csv")

        logger.info(f"Loaded: RSC={len(rsc_df)}, Brand={len(brand_df)}, BGMEA={len(bgmea_df)}")

        if rsc_df.empty and brand_df.empty and bgmea_df.empty:
            logger.error("No data sources available")
            return self._get_stats()

        # Use RSC as base if available, otherwise brand or BGMEA
        if not rsc_df.empty:
            base_df = rsc_df.copy()
            base_df["_source"] = "rsc"
            # Support both old field name ("name") and new scraper field name ("factory_name")
            name_col = "name" if "name" in base_df.columns else "factory_name"
            base_df["_norm_name"] = base_df[name_col].apply(self._normalize_name)
        elif not brand_df.empty:
            base_df = brand_df.copy()
            base_df["_source"] = "brand"
            base_df["_norm_name"] = base_df["factory_name"].apply(self._normalize_name)
        else:
            base_df = bgmea_df.copy()
            base_df["_source"] = "bgmea"
            base_df["_norm_name"] = base_df["factory_name"].apply(self._normalize_name)

        # Initialize merged records with base data
        merged_records = []
        matched_indices = set()

        for _, base_row in base_df.iterrows():
            record = {
                "rsc_id": base_row.get("rsc_id", ""),
                "name": base_row.get("name", base_row.get("factory_name", "")),
                "address": base_row.get("address", ""),
                "district": base_row.get("district", ""),
                "remediation_status": base_row.get("remediation_status", ""),
                # Support both old (cap_progress_percent) and new (progress_rate_pct) field names
                "cap_progress_percent": base_row.get("cap_progress_percent",
                                                     base_row.get("progress_rate_pct", "")),
                # Support both old (safety_training_status) and new (safety_training) field names
                "safety_training_status": base_row.get("safety_training_status",
                                                       base_row.get("safety_training", "")),
                # Support both old (cap_download_url) and new (cap_url) field names
                "cap_download_url": base_row.get("cap_download_url",
                                                 base_row.get("cap_url", "")),
                "workers_count": base_row.get("workers_count", ""),
                # Boiler lead fields — carry through from new RSC scraper
                "boiler_missing": base_row.get("boiler_missing", ""),
                "fire_pdf_url": base_row.get("fire_pdf_url", ""),
                "structural_pdf_url": base_row.get("structural_pdf_url", ""),
                "electrical_pdf_url": base_row.get("electrical_pdf_url", ""),
                "boiler_pdf_url": base_row.get("boiler_pdf_url", ""),
                "phone": "",
                "email": "",
                "website": "",
                "contact_person": "",
                "member_type": "",
                "foreign_buyers": "[]",
                "match_confidence": "",
                "match_source": base_row.get("_source", ""),
                "scraped_at": base_row.get("scraped_at", ""),
                "merged_at": pd.Timestamp.now().isoformat(),
            }
            merged_records.append(record)

        # Merge brand data
        if not brand_df.empty:
            logger.info("Merging brand supplier data...")
            brand_df["_source"] = "brand"
            brand_df["_norm_name"] = brand_df["factory_name"].apply(self._normalize_name)

            for idx, brand_row in brand_df.iterrows():
                match_idx, confidence = self._find_best_match(brand_row, base_df)
                if match_idx is not None:
                    brand_name = str(brand_row.get("brand_name", "")).strip()
                    existing = merged_records[match_idx]
                    if brand_name:
                        try:
                            brands = json.loads(existing["foreign_buyers"])
                        except:
                            brands = []
                        if brand_name not in brands:
                            brands.append(brand_name)
                        existing["foreign_buyers"] = json.dumps(brands)
                    existing["match_confidence"] = confidence
                    matched_indices.add(idx)
                else:
                    # Add unmatched brand record
                    self.unmatched_count += 1
                    merged_records.append({
                        "rsc_id": "",
                        "name": brand_row.get("factory_name", ""),
                        "address": brand_row.get("address", ""),
                        "district": "",
                        "remediation_status": "",
                        "cap_progress_percent": "",
                        "safety_training_status": "",
                        "cap_download_url": "",
                        "workers_count": "",
                        "phone": "",
                        "email": "",
                        "website": "",
                        "contact_person": "",
                        "member_type": "",
                        "foreign_buyers": json.dumps([brand_row.get("brand_name", "")]),
                        "match_confidence": 0,
                        "match_source": "brand_only",
                        "scraped_at": brand_row.get("scraped_at", ""),
                        "merged_at": pd.Timestamp.now().isoformat(),
                    })

        # Merge BGMEA data
        if not bgmea_df.empty:
            logger.info("Merging BGMEA member data...")
            bgmea_df["_source"] = "bgmea"
            bgmea_df["_norm_name"] = bgmea_df["factory_name"].apply(self._normalize_name)

            for idx, bgmea_row in bgmea_df.iterrows():
                match_idx, confidence = self._find_best_match(bgmea_row, base_df)
                if match_idx is not None:
                    existing = merged_records[match_idx]
                    for field in ["phone", "email", "website", "contact_person", "member_type"]:
                        value = bgmea_row.get(field, "")
                        if value and not pd.isna(value):
                            existing[field] = value
                    existing["match_confidence"] = confidence
                    matched_indices.add(match_idx)
                else:
                    # Add unmatched BGMEA record
                    self.unmatched_count += 1
                    merged_records.append({
                        "rsc_id": "",
                        "name": bgmea_row.get("factory_name", ""),
                        "address": bgmea_row.get("address", ""),
                        "district": "",
                        "remediation_status": "",
                        "cap_progress_percent": "",
                        "safety_training_status": "",
                        "cap_download_url": "",
                        "workers_count": "",
                        "phone": bgmea_row.get("phone", ""),
                        "email": bgmea_row.get("email", ""),
                        "website": bgmea_row.get("website", ""),
                        "contact_person": bgmea_row.get("contact_person", ""),
                        "member_type": bgmea_row.get("member_type", ""),
                        "foreign_buyers": "[]",
                        "match_confidence": 0,
                        "match_source": "bgmea_only",
                        "scraped_at": bgmea_row.get("scraped_at", ""),
                        "merged_at": pd.Timestamp.now().isoformat(),
                    })

        # Write outputs
        self.merged_count = len(merged_records)

        # Main database
        if merged_records:
            df = pd.DataFrame(merged_records)
            df.to_csv(self.output_file, index=False, encoding="utf-8")
            logger.info(f"Saved {len(merged_records)} records to {self.output_file}")

            if uploader:
                uploader.upload_file(
                    str(self.output_file),
                    "data/processed/master_database.csv",
                    "merger",
                )

        # Unmatched records for manual review
        unmatched = [r for r in merged_records if r.get("match_source") in ("brand_only", "bgmea_only")]
        if unmatched:
            pd.DataFrame(unmatched).to_csv(self.unmatched_file, index=False, encoding="utf-8")
            logger.info(f"Saved {len(unmatched)} unmatched records to {self.unmatched_file}")

            if uploader:
                uploader.upload_file(
                    str(self.unmatched_file),
                    "data/processed/unmatched_records.csv",
                    "merger",
                )

        return self._get_stats()

    def _get_stats(self) -> dict:
        return {
            "module": "merger",
            "total_records": self.merged_count,
            "unmatched_records": self.unmatched_count,
            "output_file": str(self.output_file) if self.output_file.exists() else None,
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("merger")
    merger = DataMerger()
    stats = merger.run()
    print(stats)
