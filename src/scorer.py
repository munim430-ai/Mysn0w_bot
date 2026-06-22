"""
Module 7: Priority Scorer
Scores factories 0-100 based on safety findings, compliance status, and buyer pressure.
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)

# Scoring rules
SCORING_RULES = {
    "p1_finding": {"points": 40, "reason": "P1 finding: {count}x critical safety issue"},
    "p2_finding": {"points": 20, "reason": "P2 finding: {count}x high priority issue"},
    "status_terminated": {"points": 50, "reason": "RSC status: Terminated - needs new equipment"},
    "status_ineligible": {"points": 40, "reason": "RSC status: Ineligible - compliance gap"},
    "status_behind": {"points": 20, "reason": "RSC status: Behind schedule"},
    "boiler_present": {"points": 30, "reason": "Boiler finding present - direct equipment need"},
    "boiler_missing_direct": {"points": 35, "reason": "No boiler safety PDF on RSC - likely needs upgrade"},
    "boiler_missing": {"points": 15, "reason": "Boiler findings MISSING from CAP - hidden risk"},
    "boiler_age_15plus": {"points": 30, "reason": "Boiler age >15 years - replacement window"},
    "boiler_age_8to15": {"points": 15, "reason": "Boiler age 8-15 years - approaching replacement"},
    "buyer_eu": {"points": 20, "reason": "EU buyer ({brand}) - CSDDD/ESPR pressure"},
    "buyer_us": {"points": 10, "reason": "US buyer ({brand}) - audit pressure"},
    "no_recent_purchase": {"points": 10, "reason": "No recent boiler purchase (<2 years)"},
    "user_expanding": {"points": 25, "reason": "User note: factory expanding - guaranteed equipment need"},
}

EU_BRANDS = {"H&M", "Inditex", "C&A", "Uniqlo", "Adidas", "Zara"}
US_BRANDS = {"Walmart", "Gap", "Nike", "Patagonia"}


class PriorityScorer:
    """Scores factories based on priority rules."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.processed_dir = Path(self.config["paths"]["processed_data"])
        self.raw_dir = Path(self.config["paths"]["raw_data"])
        self.output_file = self.processed_dir / "scored_database.csv"
        self.annotations_file = self.raw_dir / "user_annotations.csv"

        self.scored_count = 0

    def _load_master(self) -> pd.DataFrame:
        """Load master database, falling back to raw RSC data if not merged yet."""
        master_file = self.processed_dir / "master_database.csv"
        if master_file.exists():
            return pd.read_csv(master_file, encoding="utf-8")

        # Fallback: score directly from rsc_factories.csv
        rsc_file = Path(self.config["paths"]["raw_data"]) / "rsc_factories.csv"
        if rsc_file.exists():
            logger.info(f"No master database — using RSC factories directly: {rsc_file}")
            df = pd.read_csv(rsc_file, encoding="utf-8")
            # Normalise field names to match the scorer's expectations
            df = df.rename(columns={"factory_name": "name", "progress_rate_pct": "cap_progress_percent"})
            return df

        logger.error("Master database not found: %s", master_file)
        return pd.DataFrame()

    def _load_findings(self) -> pd.DataFrame:
        """Load parsed findings."""
        findings_file = self.raw_dir / "parsed_findings.csv"
        if not findings_file.exists():
            logger.warning(f"Findings file not found: {findings_file}")
            return pd.DataFrame()
        return pd.read_csv(findings_file, encoding="utf-8")

    def _load_annotations(self) -> dict:
        """Load user annotations (expanding notes, boiler age, etc.)."""
        annotations = {}
        if not self.annotations_file.exists():
            return annotations

        try:
            df = pd.read_csv(self.annotations_file, encoding="utf-8")
            for _, row in df.iterrows():
                factory_id = str(row.get("factory_id", "")).strip()
                if factory_id:
                    annotations[factory_id] = {
                        "expanding": str(row.get("expanding", "")).lower() in ("true", "yes", "1", "y"),
                        "boiler_age_years": row.get("boiler_age_years", None),
                        "boiler_purchase_year": row.get("boiler_purchase_year", None),
                        "notes": str(row.get("notes", "")),
                    }
        except Exception as e:
            logger.warning(f"Error loading annotations: {e}")

        return annotations

    def _create_annotations_template(self):
        """Create template for user annotations."""
        if self.annotations_file.exists():
            return

        template_file = self.raw_dir / "user_annotations_template.csv"
        fieldnames = ["factory_id", "factory_name", "expanding", "boiler_age_years", "boiler_purchase_year", "notes"]
        with open(template_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({
                "factory_id": "example-id-123",
                "factory_name": "Example Factory Ltd",
                "expanding": "yes",
                "boiler_age_years": 18,
                "boiler_purchase_year": 2007,
                "notes": "Factory is expanding production line",
            })
        logger.info(f"Created annotations template: {template_file}")

    def _score_factory(self, row: pd.Series, findings_df: pd.DataFrame, annotations: dict) -> tuple:
        """
        Calculate priority score for a factory.
        Returns (score, color, reason_text).
        """
        score = 0
        reasons = []
        factory_id = str(row.get("rsc_id", row.get("name", "")))

        # Count findings by priority for this factory
        factory_findings = findings_df[findings_df["factory_id"] == factory_id] if not findings_df.empty else pd.DataFrame()

        # Direct boiler_missing flag from RSC scraper (highest-confidence signal)
        boiler_missing_flag = str(row.get("boiler_missing", "")).lower()
        if boiler_missing_flag == "true":
            score += SCORING_RULES["boiler_missing_direct"]["points"]
            reasons.append(SCORING_RULES["boiler_missing_direct"]["reason"])

        if not factory_findings.empty:
            p1_count = len(factory_findings[factory_findings["priority"].str.upper() == "P1"])
            p2_count = len(factory_findings[factory_findings["priority"].str.upper() == "P2"])
            boiler_findings = factory_findings[factory_findings["category"].str.lower() == "boiler"]

            # P1 findings
            if p1_count > 0:
                points = SCORING_RULES["p1_finding"]["points"] * p1_count
                score += points
                reasons.append(SCORING_RULES["p1_finding"]["reason"].format(count=p1_count))

            # P2 findings
            if p2_count > 0:
                points = SCORING_RULES["p2_finding"]["points"] * p2_count
                score += points
                reasons.append(SCORING_RULES["p2_finding"]["reason"].format(count=p2_count))

            # Boiler findings present
            if len(boiler_findings) > 0:
                score += SCORING_RULES["boiler_present"]["points"]
                reasons.append(SCORING_RULES["boiler_present"]["reason"])
        elif boiler_missing_flag != "true":
            # No findings parsed and no direct flag — treat as hidden risk
            score += SCORING_RULES["boiler_missing"]["points"]
            reasons.append(SCORING_RULES["boiler_missing"]["reason"])

        # RSC status — use substring match; real values are "Behind schedule", etc.
        status = str(row.get("remediation_status", "")).strip().lower()
        if "terminated" in status:
            score += SCORING_RULES["status_terminated"]["points"]
            reasons.append(SCORING_RULES["status_terminated"]["reason"])
        elif "ineligible" in status:
            score += SCORING_RULES["status_ineligible"]["points"]
            reasons.append(SCORING_RULES["status_ineligible"]["reason"])
        elif "behind" in status:
            score += SCORING_RULES["status_behind"]["points"]
            reasons.append(SCORING_RULES["status_behind"]["reason"])

        # Foreign buyers
        foreign_buyers = row.get("foreign_buyers", "[]")
        try:
            buyers = json.loads(foreign_buyers) if isinstance(foreign_buyers, str) else []
        except:
            buyers = []

        for buyer in buyers:
            buyer_name = buyer.strip()
            if buyer_name in EU_BRANDS:
                score += SCORING_RULES["buyer_eu"]["points"]
                reasons.append(SCORING_RULES["buyer_eu"]["reason"].format(brand=buyer_name))
            elif buyer_name in US_BRANDS:
                score += SCORING_RULES["buyer_us"]["points"]
                reasons.append(SCORING_RULES["buyer_us"]["reason"].format(brand=buyer_name))

        # User annotations
        annotation = annotations.get(factory_id, {})
        if annotation.get("expanding", False):
            score += SCORING_RULES["user_expanding"]["points"]
            reasons.append(SCORING_RULES["user_expanding"]["reason"])

        # Boiler age
        boiler_age = annotation.get("boiler_age_years")
        if boiler_age is not None:
            try:
                age = int(boiler_age)
                if age > 15:
                    score += SCORING_RULES["boiler_age_15plus"]["points"]
                    reasons.append(SCORING_RULES["boiler_age_15plus"]["reason"])
                elif age >= 8:
                    score += SCORING_RULES["boiler_age_8to15"]["points"]
                    reasons.append(SCORING_RULES["boiler_age_8to15"]["reason"])
            except (ValueError, TypeError):
                pass

        # Recent boiler purchase
        purchase_year = annotation.get("boiler_purchase_year")
        if purchase_year is not None:
            try:
                year = int(purchase_year)
                if datetime.now().year - year >= 2:
                    score += SCORING_RULES["no_recent_purchase"]["points"]
                    reasons.append(SCORING_RULES["no_recent_purchase"]["reason"])
            except (ValueError, TypeError):
                pass

        # Cap score at 100
        score = min(score, 100)

        # Determine color
        if score >= 80:
            color = "Red"
        elif score >= 50:
            color = "Yellow"
        else:
            color = "Green"

        # Build reason text
        reason_text = " + ".join(reasons[:5]) if reasons else "No priority triggers"

        return score, color, reason_text

    def run(self, uploader: GitHubUploader = None) -> dict:
        """Execute the scoring process."""
        logger.info("Starting priority scorer")

        master_df = self._load_master()
        findings_df = self._load_findings()
        annotations = self._load_annotations()

        if master_df.empty:
            logger.error("No master data to score")
            return self._get_stats()

        # Create annotations template if not exists
        self._create_annotations_template()

        # Score each factory
        scores = []
        colors = []
        reasons = []

        for _, row in master_df.iterrows():
            score, color, reason = self._score_factory(row, findings_df, annotations)
            scores.append(score)
            colors.append(color)
            reasons.append(reason)

        master_df["priority_score"] = scores
        master_df["priority_color"] = colors
        master_df["priority_reason"] = reasons

        # Write output
        master_df.to_csv(self.output_file, index=False, encoding="utf-8")
        self.scored_count = len(master_df)

        # Summary stats
        red_count = sum(1 for c in colors if c == "Red")
        yellow_count = sum(1 for c in colors if c == "Yellow")
        green_count = sum(1 for c in colors if c == "Green")

        logger.info(f"Scored {self.scored_count} factories: Red={red_count}, Yellow={yellow_count}, Green={green_count}")

        if uploader:
            uploader.upload_file(
                str(self.output_file),
                "data/processed/scored_database.csv",
                "scorer",
            )

        return self._get_stats()

    def _get_stats(self) -> dict:
        return {
            "module": "scorer",
            "factories_scored": self.scored_count,
            "output_file": str(self.output_file) if self.output_file.exists() else None,
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("scorer")
    scorer = PriorityScorer()
    stats = scorer.run()
    print(stats)
