"""
Phase 1: Receipt extraction.

Reads every receipt (JPG, PNG or PDF) in the receipts/ folder, sends it to
Claude, and gets back structured data: merchant, date, total, currency and
a confidence rating. Results are saved to output/extracted_receipts.csv.

If a receipt can't be extracted (unreadable image, API error, bad data),
it is logged and recorded as "failed" in the CSV. The script keeps going
with the remaining receipts instead of crashing.

Run from the project root:
    python src/extract.py
"""

import base64
import csv
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Folder paths, worked out relative to this file so the script runs from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECEIPTS_DIR = PROJECT_ROOT / "receipts"
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_CSV = OUTPUT_DIR / "extracted_receipts.csv"
LOG_FILE = OUTPUT_DIR / "extraction.log"

# File types we accept, mapped to the "media type" the Claude API expects.
SUPPORTED_FILES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".pdf": "application/pdf",
}

# Columns in the output CSV, in order.
CSV_COLUMNS = [
    "source_file",  # which receipt file this row came from
    "status",       # "success" or "failed"
    "merchant",
    "date",         # YYYY-MM-DD
    "total",
    "currency",
    "confidence",   # high / medium / low
    "notes",        # Claude's comments on anything unclear
    "error",        # why extraction failed (empty on success)
]

# Instructions sent to Claude with every receipt.
EXTRACTION_PROMPT = """You are extracting data from a UK receipt for bookkeeping.

Return these fields:
- merchant: the business name as printed on the receipt.
- date: the transaction date in YYYY-MM-DD format. UK receipts write dates
  day-first, so 03/04/2026 means 3 April 2026.
- total: the final amount paid, including VAT and any tip, as a plain number
  (for example 12.50). Do not use the subtotal.
- currency: the 3-letter ISO currency code. Assume GBP unless the receipt
  clearly shows another currency.
- confidence: "high" if every field is clearly legible, "medium" if you had
  to make a small judgement call, "low" if the image is hard to read or
  you are guessing.
- notes: a short explanation of anything unclear. Leave empty if nothing was.

If a field genuinely cannot be found, set it to null rather than guessing.
If the file is not a receipt at all, set every field to null and explain
in notes."""


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

class ReceiptData(BaseModel):
    """
    The JSON structure we ask Claude to return.

    The SDK turns this class into a JSON schema and sends it with the request,
    so Claude's reply is guaranteed to have these fields and types. Fields are
    Optional so Claude can say "not found" (null) instead of inventing a value;
    validate_receipt() below then decides whether the result is usable.
    """
    merchant: Optional[str] = Field(description="Business name on the receipt")
    date: Optional[str] = Field(description="Transaction date as YYYY-MM-DD")
    total: Optional[float] = Field(description="Final amount paid")
    currency: Optional[str] = Field(description="3-letter ISO code, e.g. GBP")
    confidence: Literal["high", "medium", "low"]
    notes: str = Field(description="Anything unclear, or empty string")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Log to the terminal and to output/extraction.log at the same time."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    # Hide the HTTP library's per-request messages so the log stays readable.
    # (The Anthropic SDK uses httpx2; older SDK versions used httpx.)
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)


def find_receipts() -> list[Path]:
    """Return all supported receipt files in receipts/, sorted by name."""
    files = [
        path for path in RECEIPTS_DIR.iterdir()
        if path.suffix.lower() in SUPPORTED_FILES
    ]
    return sorted(files)


def build_file_block(path: Path) -> dict:
    """
    Turn a receipt file into the content block format the Claude API expects.

    Files are sent as base64 text (a standard way to put binary data inside
    JSON). Images use an "image" block and PDFs use a "document" block.
    """
    media_type = SUPPORTED_FILES[path.suffix.lower()]
    encoded = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    block_type = "document" if media_type == "application/pdf" else "image"
    return {
        "type": block_type,
        "source": {"type": "base64", "media_type": media_type, "data": encoded},
    }


def ask_claude(client: anthropic.Anthropic, model: str, path: Path) -> ReceiptData:
    """
    Send one receipt to Claude and return the parsed result.

    messages.parse() sends our ReceiptData schema along with the request and
    converts Claude's JSON reply back into a ReceiptData object for us.
    """
    response = client.messages.parse(
        model=model,
        max_tokens=16000,
        messages=[{
            "role": "user",
            # The file goes first, then the instructions.
            "content": [
                build_file_block(path),
                {"type": "text", "text": EXTRACTION_PROMPT},
            ],
        }],
        output_format=ReceiptData,
    )

    # Claude can decline a request or run out of room; in both cases there's
    # no usable data, so raise an error that the caller will log.
    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to process this file")
    if response.stop_reason == "max_tokens":
        raise ValueError("Claude's reply was cut off before it finished")
    if response.parsed_output is None:
        raise ValueError("Claude's reply could not be parsed")

    return response.parsed_output


def validate_receipt(data: ReceiptData) -> list[str]:
    """
    Check the extracted data makes sense. Returns a list of problems;
    an empty list means the receipt passed every check.
    """
    problems = []

    if not data.merchant or not data.merchant.strip():
        problems.append("merchant is missing")

    # The date must exist, be in YYYY-MM-DD format, be a real calendar date,
    # and not be in the future.
    if not data.date:
        problems.append("date is missing")
    else:
        try:
            parsed_date = datetime.strptime(data.date, "%Y-%m-%d").date()
            if parsed_date > date.today():
                problems.append(f"date {data.date} is in the future")
        except ValueError:
            problems.append(f"date '{data.date}' is not a valid YYYY-MM-DD date")

    if data.total is None:
        problems.append("total is missing")
    elif data.total <= 0:
        problems.append(f"total {data.total} is not a positive amount")

    if not data.currency:
        problems.append("currency is missing")
    elif len(data.currency) != 3 or not data.currency.isalpha():
        problems.append(f"currency '{data.currency}' is not a 3-letter code")

    return problems


def success_row(path: Path, data: ReceiptData) -> dict:
    """Build a CSV row for a receipt that extracted cleanly."""
    return {
        "source_file": path.name,
        "status": "success",
        "merchant": data.merchant.strip(),
        "date": data.date,
        "total": f"{data.total:.2f}",  # always two decimal places, e.g. 12.50
        "currency": data.currency.upper(),
        "confidence": data.confidence,
        "notes": data.notes,
        "error": "",
    }


def failed_row(path: Path, error: str) -> dict:
    """Build a CSV row for a receipt that failed, so it's visible in the output."""
    row = {column: "" for column in CSV_COLUMNS}
    row.update({"source_file": path.name, "status": "failed", "error": error})
    return row


def process_receipt(client: anthropic.Anthropic, model: str, path: Path) -> dict:
    """
    Extract one receipt and return its CSV row.

    Every error is caught here and turned into a "failed" row, which is what
    stops one bad receipt from crashing the whole run.
    """
    try:
        data = ask_claude(client, model, path)
    except anthropic.AuthenticationError:
        # A bad API key will fail for every receipt, so stop immediately.
        raise
    except anthropic.APIStatusError as e:
        # The API rejected the request (e.g. file too large, server error).
        error = f"API error {e.status_code}: {e.message}"
        logging.error("%s: %s", path.name, error)
        return failed_row(path, error)
    except anthropic.APIConnectionError:
        error = "Could not connect to the Claude API"
        logging.error("%s: %s", path.name, error)
        return failed_row(path, error)
    except (ValidationError, ValueError) as e:
        # Claude's reply didn't match the expected structure.
        error = f"Invalid response: {e}"
        logging.error("%s: %s", path.name, error)
        return failed_row(path, error)

    problems = validate_receipt(data)
    if problems:
        error = "; ".join(problems)
        logging.warning("%s: failed validation (%s)", path.name, error)
        return failed_row(path, error)

    logging.info(
        "%s: %s, %s, %.2f %s (confidence: %s)",
        path.name, data.merchant, data.date, data.total, data.currency, data.confidence,
    )
    return success_row(path, data)


def save_csv(rows: list[dict]) -> None:
    """Write all rows to output/extracted_receipts.csv, replacing any old file."""
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()

    # Load ANTHROPIC_API_KEY and CLAUDE_MODEL from the .env file.
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "sk-ant-your-key-here":
        logging.error("No API key found. Add ANTHROPIC_API_KEY to your .env file.")
        sys.exit(1)
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

    receipts = find_receipts()
    if not receipts:
        logging.warning("No receipts found in %s (supported: JPG, PNG, PDF).", RECEIPTS_DIR)
        return
    logging.info("Found %d receipt(s). Extracting with %s...", len(receipts), model)

    client = anthropic.Anthropic(api_key=api_key)
    rows = []
    for path in receipts:
        try:
            rows.append(process_receipt(client, model, path))
        except anthropic.AuthenticationError:
            logging.error("The API key was rejected. Check ANTHROPIC_API_KEY in .env.")
            sys.exit(1)

    save_csv(rows)

    # Summary.
    succeeded = sum(1 for row in rows if row["status"] == "success")
    failed = len(rows) - succeeded
    logging.info("Done: %d succeeded, %d failed.", succeeded, failed)
    logging.info("Results saved to %s", OUTPUT_CSV.relative_to(PROJECT_ROOT))
    if failed:
        logging.info("See %s for failure details.", LOG_FILE.relative_to(PROJECT_ROOT))


if __name__ == "__main__":
    main()
