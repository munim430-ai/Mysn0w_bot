"""
Module 8: App Exporter
Exports scored database to app-ready CSV schemas for any CRM/importer.
"""

import csv
import json
import logging
from pathlib import Path

import pandas as pd
import yaml

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)


class AppExporter:
    """Exports data to app-ready CSV formats."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.processed_dir = Path(self.config["paths"]["processed_data"])
        self.raw_dir = Path(self.config["paths"]["raw_data"])
        self.output_dir = self.processed_dir / "app_import"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.exported_files = []

    def _load_scored_db(self) -> pd.DataFrame:
        """Load scored database."""
        scored_file = self.processed_dir / "scored_database.csv"
        if not scored_file.exists():
            logger.error(f"Scored database not found: {scored_file}")
            return pd.DataFrame()
        return pd.read_csv(scored_file, encoding="utf-8")

    def _load_findings(self) -> pd.DataFrame:
        """Load parsed findings."""
        findings_file = self.raw_dir / "parsed_findings.csv"
        if not findings_file.exists():
            return pd.DataFrame()
        return pd.read_csv(findings_file, encoding="utf-8")

    def _generate_id(self, prefix: str, index: int) -> str:
        """Generate a deterministic ID."""
        return f"{prefix}_{index + 1:06d}"

    def _export_factories(self, df: pd.DataFrame) -> Path:
        """Export factories.csv - the main factory table."""
        output_file = self.output_dir / "factories.csv"

        # Map fields from scored database
        records = []
        for idx, row in df.iterrows():
            factory_id = self._generate_id("F", idx)

            # Parse foreign buyers JSON
            buyers = row.get("foreign_buyers", "[]")
            try:
                buyers_list = json.loads(buyers) if isinstance(buyers, str) else []
            except:
                buyers_list = []

            # Determine compliance pressure
            compliance_pressure = ""
            if buyers_list:
                eu_brands = {"H&M", "Inditex", "C&A", "Uniqlo", "Adidas", "Zara"}
                us_brands = {"Walmart", "Gap", "Nike", "Patagonia"}
                pressures = []
                for b in buyers_list:
                    if b in eu_brands:
                        pressures.append("CSDDD/ESPR")
                    elif b in us_brands:
                        pressures.append("US Audit")
                compliance_pressure = "; ".join(set(pressures))

            records.append({
                "id": factory_id,
                "name": row.get("name", ""),
                "address": row.get("address", ""),
                "district": row.get("district", ""),
                "phone": row.get("phone", ""),
                "email": row.get("email", ""),
                "website": row.get("website", ""),
                "rsc_id": row.get("rsc_id", ""),
                "rsc_status": row.get("remediation_status", ""),
                "cap_progress": row.get("cap_progress_percent", ""),
                "workers": row.get("workers_count", ""),
                "product_type": row.get("product_category", ""),
                "priority": row.get("priority_color", ""),
                "priority_score": row.get("priority_score", 0),
                "priority_reason": row.get("priority_reason", ""),
                "foreign_buyers": buyers,
                "compliance_pressure": compliance_pressure,
                "created_at": row.get("scraped_at", ""),
                "updated_at": row.get("merged_at", ""),
            })

        factories_df = pd.DataFrame(records)
        factories_df.to_csv(output_file, index=False, encoding="utf-8")
        logger.info(f"Exported {len(records)} factories to {output_file}")
        return output_file

    def _export_findings(self, findings_df: pd.DataFrame, factories_df: pd.DataFrame) -> Path:
        """Export findings.csv with factory_id references."""
        output_file = self.output_dir / "findings.csv"

        if findings_df.empty:
            # Write empty template with headers
            fieldnames = [
                "id", "factory_id", "finding_code", "category",
                "description", "priority", "remediation_timeframe", "status",
            ]
            with open(output_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
            return output_file

        # Build factory_id lookup from rsc_id
        factory_lookup = {}
        for idx, row in factories_df.iterrows():
            rsc_id = str(row.get("rsc_id", "")).strip()
            factory_id = row.get("id", "")
            if rsc_id and factory_id:
                factory_lookup[rsc_id] = factory_id

        records = []
        for idx, row in findings_df.iterrows():
            finding_id = self._generate_id("FIND", idx)
            factory_ref = str(row.get("factory_id", "")).strip()
            factory_id = factory_lookup.get(factory_ref, factory_ref)

            records.append({
                "id": finding_id,
                "factory_id": factory_id,
                "finding_code": row.get("finding_code", ""),
                "category": row.get("category", ""),
                "description": row.get("description", ""),
                "priority": row.get("priority", ""),
                "remediation_timeframe": row.get("remediation_timeframe", ""),
                "status": row.get("status", ""),
            })

        out_df = pd.DataFrame(records)
        out_df.to_csv(output_file, index=False, encoding="utf-8")
        logger.info(f"Exported {len(records)} findings to {output_file}")
        return output_file

    def _export_boilers_template(self) -> Path:
        """Export empty boilers.csv template for user to fill."""
        output_file = self.output_dir / "boilers.csv"
        fieldnames = [
            "id", "factory_id", "registration_number", "manufacturer",
            "capacity_kg_hr", "fuel_type", "manufacture_year", "age_years",
            "inspection_status", "p1_count", "p2_count", "p3_count",
            "finding_codes", "replacement_urgency",
        ]

        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            # Sample row
            writer.writerow({
                "id": "B_000001",
                "factory_id": "F_000001",
                "registration_number": "B-2023-1234",
                "manufacturer": "Miura Boiler",
                "capacity_kg_hr": "3000",
                "fuel_type": "Natural Gas",
                "manufacture_year": "2008",
                "age_years": "17",
                "inspection_status": "Overdue",
                "p1_count": "2",
                "p2_count": "1",
                "p3_count": "0",
                "finding_codes": "B-1, B-3",
                "replacement_urgency": "Critical",
            })

        logger.info(f"Exported boilers template to {output_file}")
        return output_file

    def _export_interactions_template(self) -> Path:
        """Export empty interactions.csv template."""
        output_file = self.output_dir / "interactions.csv"
        fieldnames = [
            "id", "factory_id", "type", "date", "notes",
            "outcome", "next_action", "reminder_date",
        ]

        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({
                "id": "I_000001",
                "factory_id": "F_000001",
                "type": "Initial Contact",
                "date": "2025-01-15",
                "notes": "Spoke with factory manager about boiler upgrade needs",
                "outcome": "Interested - requesting quote",
                "next_action": "Send proposal",
                "reminder_date": "2025-01-22",
            })

        logger.info(f"Exported interactions template to {output_file}")
        return output_file

    def _export_competitors_template(self) -> Path:
        """Export empty competitors.csv template."""
        output_file = self.output_dir / "competitors.csv"
        fieldnames = [
            "id", "factory_id", "competitor_name", "product_offered",
            "price_quoted", "relationship_status",
        ]

        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({
                "id": "C_000001",
                "factory_id": "F_000001",
                "competitor_name": "XYZ Boilers Ltd",
                "product_offered": "3-ton Gas Boiler",
                "price_quoted": "$45,000",
                "relationship_status": "Current supplier",
            })

        logger.info(f"Exported competitors template to {output_file}")
        return output_file

    def _export_boiler_leads(self, df: pd.DataFrame) -> Path:
        """
        Export a clean boiler_sales_leads.csv — factories where boiler_missing=True,
        sorted by priority score descending. This is the primary deliverable.
        """
        output_file = self.output_dir / "boiler_sales_leads.csv"

        # Support both merged (master) and direct RSC scraper output column names
        name_col = "name" if "name" in df.columns else "factory_name"
        workers_col = "workers_count"
        status_col = "remediation_status"
        progress_col = "cap_progress_percent" if "cap_progress_percent" in df.columns else "progress_rate_pct"

        leads = df[df["boiler_missing"].astype(str).str.lower() == "true"].copy()
        if "priority_score" in leads.columns:
            leads = leads.sort_values("priority_score", ascending=False)

        records = []
        for idx, row in leads.iterrows():
            records.append({
                "rank": len(records) + 1,
                "factory_name": row.get(name_col, ""),
                "workers_count": row.get(workers_col, ""),
                "remediation_status": row.get(status_col, ""),
                "cap_progress_pct": row.get(progress_col, ""),
                "priority_score": row.get("priority_score", ""),
                "priority_color": row.get("priority_color", ""),
                "priority_reason": row.get("priority_reason", ""),
                "fire_pdf_url": row.get("fire_pdf_url", ""),
                "structural_pdf_url": row.get("structural_pdf_url", ""),
                "electrical_pdf_url": row.get("electrical_pdf_url", ""),
                "boiler_pdf_url": "",
                "cap_url": row.get("cap_url", ""),
                "safety_training": row.get("safety_training", ""),
                "scraped_at": row.get("scraped_at", ""),
            })

        out_df = pd.DataFrame(records)
        out_df.to_csv(output_file, index=False, encoding="utf-8")
        logger.info(f"Exported {len(records)} boiler leads → {output_file}")
        return output_file

    def run(self, uploader: GitHubUploader = None) -> dict:
        """Execute the export process."""
        logger.info("Starting app exporter")

        scored_df = self._load_scored_db()
        findings_df = self._load_findings()

        if scored_df.empty:
            # Fall back to raw rsc_factories.csv when scorer hasn't run yet
            raw_file = Path(self.config["paths"]["raw_data"]) / "rsc_factories.csv"
            if raw_file.exists():
                logger.info(f"No scored DB — exporting directly from {raw_file}")
                scored_df = pd.read_csv(raw_file, encoding="utf-8")
            else:
                logger.error("No scored data to export")
                return self._get_stats()

        # Always export boiler leads — the primary deliverable
        leads_file = self._export_boiler_leads(scored_df)
        self.exported_files = [leads_file]

        # Full export only when master/scored data is available
        if "priority_score" in scored_df.columns or "rsc_id" in scored_df.columns:
            factories_file = self._export_factories(scored_df)
            findings_file = self._export_findings(findings_df, pd.read_csv(factories_file, encoding="utf-8"))
            boilers_file = self._export_boilers_template()
            interactions_file = self._export_interactions_template()
            competitors_file = self._export_competitors_template()
            self.exported_files += [factories_file, findings_file, boilers_file,
                                     interactions_file, competitors_file]

        logger.info(f"Exported {len(self.exported_files)} files to {self.output_dir}")

        if uploader:
            uploader.upload_directory(
                str(self.output_dir),
                "data/processed/app_import",
                "app_exporter",
            )

        return self._get_stats()

    def _get_stats(self) -> dict:
        return {
            "module": "app_exporter",
            "files_exported": [str(f.name) for f in self.exported_files],
            "output_dir": str(self.output_dir),
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("app_exporter")
    exporter = AppExporter()
    stats = exporter.run()
    print(stats)
