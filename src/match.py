"""
Phase 2: Match receipts to bank transactions.

Reads the receipts extracted in Phase 1 (output/extracted_receipts.csv) and
every Monzo CSV export in data/, then finds the bank transaction that each
receipt belongs to.

Matching uses plain, explainable rules (no AI), checking three things:
  1. Amount   - must be identical, compared in the receipt's own currency.
  2. Date     - the bank date must be within DATE_WINDOW_DAYS of the receipt.
  3. Merchant - the names must be similar enough (MIN_NAME_SIMILARITY).

Each receipt gets one of three statuses:
  matched       - amount, date and merchant all agree
  needs review  - the amount agrees, but only one of date/merchant does
  unmatched     - no transaction has the right amount (or nothing else fits)

Outputs:
  output/matches.csv                 - one row per receipt
  output/unmatched_transactions.csv  - payments with no receipt

Run from the project root (after src/extract.py):
    python src/match.py
"""

import csv
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
RECEIPTS_CSV = OUTPUT_DIR / "extracted_receipts.csv"
MATCHES_CSV = OUTPUT_DIR / "matches.csv"
UNMATCHED_TRANSACTIONS_CSV = OUTPUT_DIR / "unmatched_transactions.csv"

# How many days apart the receipt date and bank date can be. Card payments
# sometimes appear a day or two after the purchase.
DATE_WINDOW_DAYS = 3

# How similar two merchant names must be, from 0 (nothing alike) to 1 (same).
MIN_NAME_SIMILARITY = 0.6

# Monzo transaction types that aren't purchases, so never need a receipt.
# Pot transfers are just money moving between your own Monzo pots.
IGNORED_TRANSACTION_TYPES = {"Pot transfer"}

# Words that appear in company names but don't help identify the merchant,
# e.g. "Anthropic, PBC" and "Anthropic" should count as the same name.
COMPANY_SUFFIXES = {"ltd", "limited", "plc", "inc", "llc", "pbc", "co", "corp", "the"}

MATCH_COLUMNS = [
    "status",
    "reason",
    "receipt_file",
    "receipt_merchant",
    "receipt_date",
    "receipt_total",
    "receipt_currency",
    "transaction_id",
    "bank_name",
    "bank_date",
    "bank_amount_gbp",      # what left your account, in pounds
    "bank_local_amount",    # the amount in the currency you paid in
    "bank_local_currency",
    "days_apart",
    "name_similarity",
]

UNMATCHED_TRANSACTION_COLUMNS = [
    "transaction_id", "date", "type", "name", "amount_gbp",
    "local_amount", "local_currency", "category", "source_file",
]


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class Receipt:
    """One successfully extracted receipt from Phase 1."""
    source_file: str
    merchant: str
    date: date
    total: float
    currency: str


@dataclass
class Transaction:
    """One outgoing payment from a Monzo CSV export."""
    transaction_id: str
    date: date
    type: str
    name: str
    description: str
    amount_gbp: float      # positive number, e.g. 4.55
    local_amount: float    # positive number in local currency, e.g. 6.00
    local_currency: str    # e.g. "USD"
    category: str
    source_file: str


@dataclass
class Candidate:
    """A transaction with the right amount, plus how well date and name fit."""
    transaction: Transaction
    days_apart: int
    name_similarity: float

    @property
    def date_ok(self) -> bool:
        return self.days_apart <= DATE_WINDOW_DAYS

    @property
    def name_ok(self) -> bool:
        return self.name_similarity >= MIN_NAME_SIMILARITY


# ---------------------------------------------------------------------------
# Loading data
# ---------------------------------------------------------------------------

def load_receipts() -> tuple[list[Receipt], list[dict]]:
    """
    Read Phase 1 results. Returns (usable receipts, failed rows). Failed rows
    are carried through so they still appear in the final report.
    """
    receipts, failed = [], []
    with open(RECEIPTS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["status"] != "success":
                failed.append(row)
                continue
            receipts.append(Receipt(
                source_file=row["source_file"],
                merchant=row["merchant"],
                date=datetime.strptime(row["date"], "%Y-%m-%d").date(),
                total=float(row["total"]),
                currency=row["currency"].upper(),
            ))
    return receipts, failed


def load_transactions() -> list[Transaction]:
    """
    Read every Monzo CSV in data/ and keep only outgoing payments.

    Skipped: money coming in, £0.00 card checks, pot transfers, and any
    transaction already seen (in case two exports overlap).
    """
    transactions = []
    seen_ids = set()

    for csv_path in sorted(DATA_DIR.glob("*.csv")):
        # "utf-8-sig" handles the invisible marker some apps put at the
        # start of CSV files.
        with open(csv_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                amount = float(row["Amount"] or 0)
                if amount >= 0:                          # money in, or £0.00 check
                    continue
                if row["Type"] in IGNORED_TRANSACTION_TYPES:
                    continue
                if row["Transaction ID"] in seen_ids:     # duplicate across files
                    continue
                seen_ids.add(row["Transaction ID"])

                transactions.append(Transaction(
                    transaction_id=row["Transaction ID"],
                    date=datetime.strptime(row["Date"], "%d/%m/%Y").date(),  # UK format
                    type=row["Type"],
                    name=row["Name"],
                    description=row["Description"],
                    amount_gbp=abs(amount),
                    local_amount=abs(float(row["Local amount"] or amount)),
                    local_currency=(row["Local currency"] or row["Currency"]).upper(),
                    category=row["Category"],
                    source_file=csv_path.name,
                ))

    return transactions


# ---------------------------------------------------------------------------
# Comparison rules
# ---------------------------------------------------------------------------

def amounts_match(receipt: Receipt, txn: Transaction) -> bool:
    """
    True if the transaction is for exactly the receipt's amount.

    The comparison is done in the receipt's currency. Monzo records both the
    pounds that left your account and the original amount in the currency you
    paid in, so a $6.00 receipt is checked against the $6.00 "local amount",
    not the £4.55 it cost after conversion.
    """
    if receipt.currency == txn.local_currency:
        return abs(receipt.total - txn.local_amount) < 0.005
    if receipt.currency == "GBP":
        return abs(receipt.total - txn.amount_gbp) < 0.005
    return False


def normalise_name(name: str) -> str:
    """
    Simplify a merchant name for comparison: lowercase, strip punctuation,
    and drop company words like "Ltd" or "PBC".
    "Anthropic, PBC" -> "anthropic"
    """
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in COMPANY_SUFFIXES)


def name_similarity(receipt_name: str, bank_name: str) -> float:
    """
    Score from 0 to 1 for how alike two merchant names are.

    If every word of one name appears in the other (e.g. "anthropic" inside
    "anthropic claude sub san francisco usa"), that counts as a full match.
    Otherwise we fall back to a character-by-character similarity score.
    """
    a, b = normalise_name(receipt_name), normalise_name(bank_name)
    if not a or not b:
        return 0.0
    words_a, words_b = set(a.split()), set(b.split())
    if words_a <= words_b or words_b <= words_a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def find_candidates(receipt: Receipt, transactions: list[Transaction]) -> list[Candidate]:
    """
    Return every transaction with the right amount, best fit first.

    The merchant is compared against both the clean Monzo name ("Anthropic")
    and the raw card description ("ANTHROPIC* CLAUDE SUB SAN FRANCISCO"),
    keeping whichever scores higher.
    """
    candidates = []
    for txn in transactions:
        if not amounts_match(receipt, txn):
            continue
        candidates.append(Candidate(
            transaction=txn,
            days_apart=abs((txn.date - receipt.date).days),
            name_similarity=max(
                name_similarity(receipt.merchant, txn.name),
                name_similarity(receipt.merchant, txn.description),
            ),
        ))

    # Best first: both checks passing beats one, then most similar name,
    # then closest date.
    candidates.sort(key=fit_rank)
    return candidates


def fit_rank(candidate: Candidate) -> tuple:
    """
    Sort key where smaller means a better fit: both checks passing beats one,
    then the most similar name, then the closest date.
    """
    checks_passed = candidate.date_ok + candidate.name_ok
    return (-checks_passed, -candidate.name_similarity, candidate.days_apart)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_row(receipt: Receipt, status: str, reason: str,
              candidate: Optional[Candidate] = None) -> dict:
    """Build one row of matches.csv."""
    row = {column: "" for column in MATCH_COLUMNS}
    row.update({
        "status": status,
        "reason": reason,
        "receipt_file": receipt.source_file,
        "receipt_merchant": receipt.merchant,
        "receipt_date": receipt.date.isoformat(),
        "receipt_total": f"{receipt.total:.2f}",
        "receipt_currency": receipt.currency,
    })
    if candidate:
        txn = candidate.transaction
        row.update({
            "transaction_id": txn.transaction_id,
            "bank_name": txn.name,
            "bank_date": txn.date.isoformat(),
            "bank_amount_gbp": f"{txn.amount_gbp:.2f}",
            "bank_local_amount": f"{txn.local_amount:.2f}",
            "bank_local_currency": txn.local_currency,
            "days_apart": candidate.days_apart,
            "name_similarity": f"{candidate.name_similarity:.2f}",
        })
    return row


def match_receipts(receipts: list[Receipt], transactions: list[Transaction]) -> tuple[list[dict], set[str]]:
    """
    Match each receipt to at most one transaction, and each transaction to at
    most one receipt. Returns (rows for matches.csv, IDs of used transactions).
    """
    # Step 1: list every possible (receipt, transaction) pairing. A pairing
    # needs the right amount plus at least one of date/merchant agreeing;
    # same amount alone is probably a coincidence.
    candidates_by_receipt = {id(r): find_candidates(r, transactions) for r in receipts}
    pairings = [
        (receipt, candidate)
        for receipt in receipts
        for candidate in candidates_by_receipt[id(receipt)]
        if candidate.date_ok or candidate.name_ok
    ]

    # Step 2: hand out the best pairings first, across ALL receipts. This
    # stops a weak match from taking a transaction that is a perfect match
    # for a different receipt.
    pairings.sort(key=lambda pair: fit_rank(pair[1]))
    chosen: dict[int, Candidate] = {}   # receipt -> its transaction
    used_ids: set[str] = set()
    for receipt, candidate in pairings:
        txn_id = candidate.transaction.transaction_id
        if id(receipt) in chosen or txn_id in used_ids:
            continue
        chosen[id(receipt)] = candidate
        used_ids.add(txn_id)

    # Step 3: build a result row for every receipt, explaining the outcome.
    rows = []
    for receipt in receipts:
        best = chosen.get(id(receipt))
        if best and best.date_ok and best.name_ok:
            reason = "Amount, date and merchant all agree"
            txn = best.transaction
            if txn.local_currency != "GBP":
                reason += (f" (paid {txn.local_amount:.2f} {txn.local_currency}, "
                           f"charged £{txn.amount_gbp:.2f})")
            rows.append(match_row(receipt, "matched", reason, best))
        elif best and best.date_ok:
            rows.append(match_row(
                receipt, "needs review",
                f"Amount and date agree, but merchant name differs ('{best.transaction.name}')",
                best,
            ))
        elif best:
            rows.append(match_row(
                receipt, "needs review",
                f"Amount and merchant agree, but dates are {best.days_apart} days apart",
                best,
            ))
        else:
            rows.append(match_row(receipt, "unmatched",
                                  unmatched_reason(receipt, candidates_by_receipt[id(receipt)])))

    return rows, used_ids


def unmatched_reason(receipt: Receipt, candidates: list[Candidate]) -> str:
    """Explain why a receipt ended up without a transaction."""
    if not candidates:
        return f"No payment of {receipt.total:.2f} {receipt.currency} found in the bank data"
    if any(c.date_ok or c.name_ok for c in candidates):
        return ("The matching payment was already matched to another receipt "
                "(possible duplicate receipt)")
    return "Only payments with the same amount but a different date and merchant were found"


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def failed_extraction_row(row: dict) -> dict:
    """A matches.csv row for a receipt that Phase 1 couldn't read."""
    out = {column: "" for column in MATCH_COLUMNS}
    out.update({
        "status": "unmatched",
        "reason": f"Receipt extraction failed: {row['error']}",
        "receipt_file": row["source_file"],
    })
    return out


def save_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def unmatched_transaction_row(txn: Transaction) -> dict:
    return {
        "transaction_id": txn.transaction_id,
        "date": txn.date.isoformat(),
        "type": txn.type,
        "name": txn.name,
        "amount_gbp": f"{txn.amount_gbp:.2f}",
        "local_amount": f"{txn.local_amount:.2f}",
        "local_currency": txn.local_currency,
        "category": txn.category,
        "source_file": txn.source_file,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s  %(message)s")

    if not RECEIPTS_CSV.exists():
        logging.error("%s not found. Run src/extract.py first.", RECEIPTS_CSV.relative_to(PROJECT_ROOT))
        sys.exit(1)
    if not list(DATA_DIR.glob("*.csv")):
        logging.error("No bank CSV files found in data/.")
        sys.exit(1)

    receipts, failed = load_receipts()
    transactions = load_transactions()
    logging.info("Loaded %d receipt(s) and %d outgoing bank payment(s).",
                 len(receipts), len(transactions))

    rows, used_ids = match_receipts(receipts, transactions)
    rows += [failed_extraction_row(row) for row in failed]
    rows.sort(key=lambda r: r["receipt_file"])
    save_csv(MATCHES_CSV, MATCH_COLUMNS, rows)

    # Payments with no receipt, newest first.
    leftover = sorted((t for t in transactions if t.transaction_id not in used_ids),
                      key=lambda t: t.date, reverse=True)
    save_csv(UNMATCHED_TRANSACTIONS_CSV, UNMATCHED_TRANSACTION_COLUMNS,
             [unmatched_transaction_row(t) for t in leftover])

    # Summary.
    for row in rows:
        logging.info("%-26s %-13s %s", row["receipt_file"], row["status"], row["reason"])
    counts = {s: sum(r["status"] == s for r in rows) for s in ("matched", "needs review", "unmatched")}
    logging.info("Done: %d matched, %d need review, %d unmatched. %d payment(s) have no receipt.",
                 counts["matched"], counts["needs review"], counts["unmatched"], len(leftover))
    logging.info("Results saved to %s and %s",
                 MATCHES_CSV.relative_to(PROJECT_ROOT),
                 UNMATCHED_TRANSACTIONS_CSV.relative_to(PROJECT_ROOT))


if __name__ == "__main__":
    main()
