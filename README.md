# Receipt Matcher

Matches receipts to bank transactions using AI document extraction. It's a small version of an enterprise invoice-matching workflow: documents are read by an AI model, turned into structured data, and then reconciled against system records.

## Status

| Phase | What it does | Status |
|---|---|---|
| 1 | Extract merchant, date, total, currency and confidence from receipts with the Claude API | ✅ Done |
| 2+ | Match extracted receipts against bank CSV transactions | Planned |

## Project structure

```
receipt-matcher/
├── receipts/        # Input: receipt images and PDFs (git-ignored)
├── data/            # Input: bank statement CSV (git-ignored)
├── output/          # Results and logs (git-ignored)
├── src/
│   └── extract.py   # Phase 1: receipt extraction
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

## Run

```bash
python src/extract.py
```

This creates:

- `output/extracted_receipts.csv`, with one row per receipt. Columns: `source_file`, `status`, `merchant`, `date`, `total`, `currency`, `confidence`, `notes`, `error`.
- `output/extraction.log`, a log of the run that includes the reason for each failure.

## How extraction works

1. Each receipt is base64-encoded and sent to Claude: an image block for photos, a document block for PDFs.
2. The request includes a JSON schema (built from the `ReceiptData` Pydantic model), so the reply always has the expected fields and types.
3. The result is checked for business rules: the date is real and not in the future, the total is positive, and the currency is a 3-letter code.
4. A receipt that fails any step is logged and saved as a `failed` row, and the script moves on to the next file.

Dates are read day-first (UK format), and the currency defaults to GBP unless the receipt shows a different one.

## Privacy

`.env` (API key), `receipts/`, `data/` and `output/` are all in `.gitignore`. That keeps personal financial data and credentials out of the repository. Only empty placeholder files (`.gitkeep`) are committed so the folder structure is still visible.
