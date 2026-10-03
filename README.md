# Receipt Matcher

Matches receipts to bank transactions using AI document extraction. It's a small version of an enterprise invoice-matching workflow: documents are read by an AI model, turned into structured data, and then reconciled against system records.

## Status

| Phase | What it does | Status |
|---|---|---|
| 1 | Extract merchant, date, total, currency and confidence from receipts with the Claude API | ✅ Done |
| 2 | Match extracted receipts against bank CSV transactions | ✅ Done |

## Project structure

```
receipt-matcher/
├── receipts/        # Input: receipt images and PDFs (git-ignored)
├── data/            # Input: bank statement CSV (git-ignored)
├── output/          # Results and logs (git-ignored)
├── src/
│   ├── extract.py   # Phase 1: receipt extraction
│   └── match.py     # Phase 2: receipt-to-transaction matching
├── .env.example     # Template for your settings
├── requirements.txt
└── README.md
```

## Setup

1. Create a virtual environment and install the dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and add your Anthropic API key.
3. Put your receipts (`.jpg`, `.jpeg`, `.png` or `.pdf`) in `receipts/`.
4. Put your bank statement CSV exports in `data/`. The matcher currently reads the Monzo CSV format, and you can add several files (for example one per month or per account).

## Run

```bash
python src/extract.py   # Phase 1: read the receipts
python src/match.py     # Phase 2: match them to bank transactions
```

This creates:

- `output/extracted_receipts.csv`, with one row per receipt. Columns: `source_file`, `status`, `merchant`, `date`, `total`, `currency`, `confidence`, `notes`, `error`.
- `output/extraction.log`, a log of the extraction run that includes the reason for each failure.
- `output/matches.csv`, with one row per receipt: its status (`matched`, `needs review` or `unmatched`), the reason, and the matching bank transaction.
- `output/unmatched_transactions.csv`, listing outgoing payments that have no receipt.

## How extraction works

1. Each receipt is base64-encoded and sent to Claude: an image block for photos, a document block for PDFs.
2. The request includes a JSON schema (built from the `ReceiptData` Pydantic model), so the reply always has the expected fields and types.
3. The result is checked for business rules: the date is real and not in the future, the total is positive, and the currency is a 3-letter code.
4. A receipt that fails any step is logged and saved as a `failed` row, and the script moves on to the next file.

Dates are read day-first (UK format), and the currency defaults to GBP unless the receipt shows a different one.

## How matching works

Matching uses plain rules rather than AI. That makes it free to run, gives the same result every time, and lets every decision be explained.

1. **Bank data is cleaned:** only outgoing payments are kept. Incoming money, £0.00 card checks, transfers between your own Monzo pots, and duplicates across files are removed.
2. **Each receipt is compared with each payment on three checks:**
   - **Amount** must be identical, compared in the receipt's own currency. Monzo records the original amount of foreign payments, so a $6.00 receipt matches the $6.00 payment that cost £4.55, with no exchange-rate guessing.
   - **Date** must be within 3 days, because card payments can post after the purchase.
   - **Merchant** names must be similar after removing punctuation and words like "Ltd" or "PBC", so "Anthropic, PBC" matches "Anthropic".
3. **The best pairings are assigned first, across all receipts.** Each payment can only be used once, so a weak match can't take a payment that is a perfect fit for a different receipt.
4. **Every receipt gets a status and a reason:**
   - `matched`: all three checks agree.
   - `needs review`: the amount agrees, but only one of date or merchant does.
   - `unmatched`: there's no suitable payment. The reason says why, for example that no payment has that amount, or that the payment was already used by another receipt (a possible duplicate).

The date window and the name-similarity threshold are settings at the top of `src/match.py`.

## Privacy

`.env` (API key), `receipts/`, `data/` and `output/` are all in `.gitignore`. That keeps personal financial data and credentials out of the repository. Only empty placeholder files (`.gitkeep`) are committed so the folder structure is still visible.
