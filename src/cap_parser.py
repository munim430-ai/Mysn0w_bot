"""
Module 3: CAP PDF Parser
Extracts structured findings from CAP PDFs using pdfplumber + regex fallback.
"""

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

import pdfplumber
import yaml

from github_uploader import GitHubUploader

logger = logging.getLogger(__name__)

# Finding code patterns
FINDING_CODE_RE = re.compile(r"\b([FESB]\s*-\s*\d+)\b", re.IGNORECASE)
PRIORITY_RE = re.compile(r"\b(P[123])\b", re.IGNORECASE)
TIMEFRAME_RE = re.compile(r"(\d+)\s*(DAY|WEEK|MONTH|YEAR)S?", re.IGNORECASE)
STATUS_RE = re.compile(r"\b(OPEN|CLOSED|IN\s*PROGRESS|PENDING)\b", re.IGNORECASE)
CATEGORY_MAP = {
    "F": "Fire",
    "E": "Electrical",
    "S": "Structural",
    "B": "Boiler",
}


class CAPParser:
    """Parses CAP PDFs to extract safety findings."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.input_dir = Path(self.config["paths"]["raw_data"]) / "caps"
        self.output_dir = Path(self.config["paths"]["raw_data"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output_dir / "parsed_findings.csv"

        self.parsed_count = 0
        self.error_count = 0
        self.errors = []
        self.missing_boiler_note = []

    def _extract_text_from_pdf(self, pdf_path: Path) -> list:
        """Extract text from all pages of a PDF."""
        pages = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text:
                        pages.append({"page": i + 1, "text": text})
        except Exception as e:
            logger.error(f"Error reading {pdf_path}: {e}")
            self.errors.append(f"{pdf_path.name}: {e}")
        return pages

    def _extract_tables_from_pdf(self, pdf_path: Path) -> list:
        """Extract tables from PDF pages."""
        tables = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    page_tables = page.extract_tables()
                    for table in page_tables:
                        tables.append({"page": i + 1, "table": table})
        except Exception as e:
            logger.error(f"Table extraction error in {pdf_path}: {e}")
        return tables

    def _parse_finding_code(self, text: str) -> str:
        """Extract finding code like F-1, E-2, S-1, B-1."""
        match = FINDING_CODE_RE.search(text)
        if match:
            return match.group(1).replace(" ", "")
        return ""

    def _get_category(self, finding_code: str) -> str:
        """Determine category from finding code prefix."""
        if not finding_code:
            return "Other"
        prefix = finding_code[0].upper()
        return CATEGORY_MAP.get(prefix, "Other")

    def _parse_priority(self, text: str) -> str:
        """Extract priority level P1/P2/P3."""
        match = PRIORITY_RE.search(text)
        if match:
            return match.group(1).upper()
        return ""

    def _parse_timeframe(self, text: str) -> str:
        """Extract remediation timeframe."""
        match = TIMEFRAME_RE.search(text)
        if match:
            return f"{match.group(1)} {match.group(2).upper()}"
        return ""

    def _parse_status(self, text: str) -> str:
        """Extract finding status."""
        match = STATUS_RE.search(text)
        if match:
            return match.group(1).upper().replace(" ", " ")
        return ""

    def _extract_findings_from_text(self, pages: list, factory_id: str) -> list:
        """Extract findings using regex on full text."""
        findings = []
        boiler_found = False

        for page_data in pages:
            text = page_data["text"]
            page_num = page_data["page"]

            # Check for boiler references
            if re.search(r"\b[Bb]oiler\b", text):
                boiler_found = True

            # Split by finding codes
            # Look for patterns like "F-1", "E-1", etc. and split text around them
            finding_sections = re.split(r"(?=\b[FESB]\s*-\s*\d+\b)", text, flags=re.IGNORECASE)

            for section in finding_sections:
                finding_code = self._parse_finding_code(section)
                if not finding_code:
                    continue

                category = self._get_category(finding_code)
                if category == "Boiler":
                    boiler_found = True

                # Clean description - remove code and priority markers
                description = section.strip()
                description = FINDING_CODE_RE.sub("", description).strip()
                description = PRIORITY_RE.sub("", description).strip()
                description = re.sub(r"\s+", " ", description)[:2000]  # Limit length

                findings.append({
                    "factory_id": factory_id,
                    "finding_code": finding_code,
                    "category": category,
                    "description": description,
                    "priority": self._parse_priority(section),
                    "remediation_timeframe": self._parse_timeframe(section),
                    "status": self._parse_status(section),
                    "page_number": page_num,
                    "parsed_at": datetime.now().isoformat(),
                })

        return findings, boiler_found

    def _extract_findings_from_tables(self, tables: list, factory_id: str) -> list:
        """Extract findings from PDF tables."""
        findings = []

        for table_data in tables:
            table = table_data["table"]
            page_num = table_data["page"]

            if not table or len(table) < 2:
                continue

            # Try to identify header row
            header = [str(cell or "").lower().strip() for cell in table[0]]
            col_map = {}
            for i, h in enumerate(header):
                if any(k in h for k in ["finding", "code", "id"]):
                    col_map["code"] = i
                elif any(k in h for k in ["category", "type"]):
                    col_map["category"] = i
                elif any(k in h for k in ["description", "issue", "comment", "detail"]):
                    col_map["description"] = i
                elif any(k in h for k in ["priority", "level"]):
                    col_map["priority"] = i
                elif any(k in h for k in ["time", "deadline", "due", "period"]):
                    col_map["timeframe"] = i
                elif any(k in h for k in ["status", "state"]):
                    col_map["status"] = i

            for row in table[1:]:
                if not row or not any(row):
                    continue

                code = ""
                if "code" in col_map and col_map["code"] < len(row):
                    code = self._parse_finding_code(str(row[col_map["code"]]))
                if not code:
                    # Try finding code anywhere in row
                    for cell in row:
                        code = self._parse_finding_code(str(cell))
                        if code:
                            break

                if not code:
                    continue

                category = self._get_category(code)
                if "category" in col_map and col_map["category"] < len(row):
                    category = str(row[col_map["category"]]).strip() or category

                description = ""
                if "description" in col_map and col_map["description"] < len(row):
                    description = str(row[col_map["description"]]).strip()

                priority = ""
                if "priority" in col_map and col_map["priority"] < len(row):
                    priority = self._parse_priority(str(row[col_map["priority"]]))

                timeframe = ""
                if "timeframe" in col_map and col_map["timeframe"] < len(row):
                    timeframe = self._parse_timeframe(str(row[col_map["timeframe"]]))

                status = ""
                if "status" in col_map and col_map["status"] < len(row):
                    status = self._parse_status(str(row[col_map["status"]]))

                findings.append({
                    "factory_id": factory_id,
                    "finding_code": code,
                    "category": category,
                    "description": description[:2000],
                    "priority": priority,
                    "remediation_timeframe": timeframe,
                    "status": status,
                    "page_number": page_num,
                    "parsed_at": datetime.now().isoformat(),
                })

        return findings

    def _check_missing_boiler(self, findings: list, factory_id: str) -> bool:
        """Check if boiler findings are missing from CAP (common per CPD report)."""
        boiler_findings = [f for f in findings if f["category"] == "Boiler"]
        if not boiler_findings:
            self.missing_boiler_note.append(factory_id)
            logger.warning(f"Boiler findings MISSING from CAP for factory {factory_id} - hidden risk flagged")
            return True
        return False

    def run(self, uploader: GitHubUploader = None) -> dict:
        """Execute the CAP parser on all PDFs."""
        logger.info("Starting CAP parser")

        if not self.input_dir.exists():
            logger.error(f"CAP directory not found: {self.input_dir}")
            return self._get_stats()

        pdf_files = list(self.input_dir.glob("*.pdf"))
        if not pdf_files:
            logger.warning("No PDF files found to parse")
            return self._get_stats()

        logger.info(f"Found {len(pdf_files)} PDF(s) to parse")
        all_findings = []

        for pdf_path in pdf_files:
            factory_id = pdf_path.stem
            logger.info(f"Parsing {pdf_path.name}...")

            try:
                pages = self._extract_text_from_pdf(pdf_path)
                tables = self._extract_tables_from_pdf(pdf_path)

                text_findings, boiler_in_text = self._extract_findings_from_text(pages, factory_id)
                table_findings = self._extract_findings_from_tables(tables, factory_id)

                # Merge findings, deduplicate by code
                findings = text_findings + table_findings
                seen_codes = set()
                unique_findings = []
                for f in findings:
                    key = (f["factory_id"], f["finding_code"])
                    if key not in seen_codes and f["finding_code"]:
                        seen_codes.add(key)
                        unique_findings.append(f)

                # Check for missing boiler findings
                has_boiler_in_findings = any(f["category"] == "Boiler" for f in unique_findings)
                if not has_boiler_in_findings:
                    self._check_missing_boiler(unique_findings, factory_id)

                all_findings.extend(unique_findings)
                self.parsed_count += 1
                logger.info(f"  -> {len(unique_findings)} findings extracted")

            except Exception as e:
                self.error_count += 1
                self.errors.append(f"{pdf_path.name}: {e}")
                logger.error(f"Error parsing {pdf_path.name}: {e}")

        # Write output
        if all_findings:
            self._write_csv(all_findings)
            logger.info(f"Saved {len(all_findings)} findings to {self.output_file}")

            if uploader:
                uploader.upload_file(
                    str(self.output_file),
                    "data/raw/parsed_findings.csv",
                    "cap_parser",
                )
        else:
            logger.warning("No findings extracted from any PDF")

        if self.missing_boiler_note:
            logger.warning(f"Boiler findings missing from {len(self.missing_boiler_note)} factory CAPs")

        return self._get_stats()

    def _write_csv(self, findings: list):
        """Write findings to CSV."""
        fieldnames = [
            "factory_id", "finding_code", "category", "description",
            "priority", "remediation_timeframe", "status", "page_number", "parsed_at",
        ]
        with open(self.output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for finding in findings:
                row = {k: finding.get(k, "") for k in fieldnames}
                writer.writerow(row)

    def _get_stats(self) -> dict:
        return {
            "module": "cap_parser",
            "pdfs_parsed": self.parsed_count,
            "errors": self.error_count,
            "total_findings": 0,  # Updated during run
            "missing_boiler_caps": len(self.missing_boiler_note),
            "error_details": self.errors[:10],
            "output_file": str(self.output_file) if self.output_file.exists() else None,
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from logger import setup_logger
    setup_logger("cap_parser")
    parser = CAPParser()
    stats = parser.run()
    print(stats)
