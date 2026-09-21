# $0 OLED / TADF Daily Literature Tracker

A no-paid-API literature monitoring pipeline for GitHub Actions.

It checks **Semantic Scholar + Crossref + arXiv** every day, merges duplicate papers, remembers what you have already seen, applies a transparent keyword relevance score, and saves a Markdown + CSV report. If relevant new papers are found, GitHub can open a daily Issue as the notification.

**No OpenAI API is used. No paid service is required.**

## What is already configured

The included `config.yml` is tuned for research around:

- TADF / thermally activated delayed fluorescence
- polymer TADF
- blue TADF OLEDs
- crosslinkable / photocrosslinkable OLED materials
- photopatternable OLEDs
- solution-processed OLEDs
- stretchable OLEDs
- solvent-resistant emissive layers
- polymer hosts / host–guest OLED systems
- sequential solution deposition / RGB OLED processing

On the **first run**, the tracker searches the previous **7 days**. After that, it uses a **40-hour overlap window** so a delayed daily run is less likely to miss records. `state/seen.json` prevents repeat notifications.

## What you need to do

### 1. Create an empty GitHub repository

A public repository is simplest for staying at $0. A private repository also works if your account has enough included GitHub Actions minutes.

Suggested repository name:

`oled-tadf-paper-tracker`

### 2. Upload this project

Upload **all files and folders** from this package to the repository root, including:

- `.github/workflows/daily.yml`
- `tracker.py`
- `config.yml`
- `requirements.txt`
- `state/seen.json`
- `reports/.gitkeep`

The `.github` folder is important because it contains the daily automation.

### 3. Enable GitHub Actions

Open the repository → **Actions**. If GitHub asks you to enable workflows, enable them.

The supplied workflow runs every day at:

`13:00 UTC`

That is approximately **8:00 AM Chicago during daylight-saving time** and **7:00 AM Chicago during standard time**.

You can also run it manually at any time:

**Actions → Daily literature tracker → Run workflow**

### 4. Give the workflow write permission if GitHub blocks commits/issues

For most repositories the workflow permission in `daily.yml` is sufficient. If GitHub reports a permission error, open:

**Settings → Actions → General → Workflow permissions**

and allow GitHub Actions to read/write repository contents.

## What happens each day

The workflow performs:

```text
Semantic Scholar ─┐
Crossref ──────────┼─> merge/deduplicate
arXiv ─────────────┘
                         ↓
                  remove seen papers
                         ↓
                  keyword relevance score
                         ↓
               reports/YYYY-MM-DD.md
               reports/YYYY-MM-DD.csv
                         ↓
                GitHub Issue notification
```

A report entry looks roughly like:

```text
Title
Authors
Journal/category
Publication date
DOI / arXiv ID
Source(s)
Keyword relevance score
Matched keywords
Abstract
```

There is deliberately **no AI-generated summary** in the $0 version. When you want deeper analysis, upload that day's Markdown/CSV to ChatGPT and ask it to rank or summarize the papers.

## How relevance filtering works

There is no hidden model. `config.yml` contains explicit keyword weights, for example:

```yaml
keywords:
  "tadf": 5
  "oled": 3
  "stretchable": 5
  "photocrosslink": 7
  "photopattern": 6
  "polymer tadf": 7

min_score: 5
```

The tracker searches title + abstract + venue/category. Matching a keyword adds its weight; a match in the **title** gets an additional bonus.

This makes the behavior easy to tune and keeps the system free.

## Changing the research topics

Edit `config.yml`.

### Search queries

These are sent to the external scholarly databases:

```yaml
queries:
  - "thermally activated delayed fluorescence"
  - "TADF OLED"
  - "polymer TADF"
  - "crosslinkable OLED"
```

Add or remove queries as your project evolves.

### Keyword weights

Use higher values for topics that matter more to you:

```yaml
keywords:
  "crosslinkable tadf": 10
  "blue tadf": 8
  "stretchable oled": 8
```

Lower `min_score` to increase recall; raise it to reduce noise.

## Optional: Semantic Scholar API key

The tracker is designed to work without a paid key. If you later obtain a free Semantic Scholar API key and want to use it, add a GitHub repository secret named:

`SEMANTIC_SCHOLAR_API_KEY`

Path:

**Settings → Secrets and variables → Actions → New repository secret**

This is optional. Do not put the key directly into `config.yml` or Python code.

## Optional: Crossref contact email

Crossref recommends that automated clients identify themselves. You can put your email in:

```yaml
crossref_mailto: "you@example.edu"
```

This is optional and is not an account/API key.

## Files generated automatically

```text
reports/2026-09-21.md
reports/2026-09-21.csv
reports/run_meta.json
reports/issue_body.md
state/seen.json
```

`state/seen.json` is important. Do not delete it unless you intentionally want the tracker to treat old papers as unseen again.

## If too many irrelevant papers appear

Raise:

```yaml
min_score: 5
```

to 7–10, or add a phrase to:

```yaml
exclude_phrases:
```

## If you think papers are being missed

1. Add broader items under `queries`.
2. Add synonyms under `keywords`.
3. Lower `min_score`.
4. Increase the per-query page caps in `max_pages_per_query`.

For your OLED/TADF use case, it is generally better to search broadly and filter locally than to make every database query overly narrow.

## Important limitation

No literature database has literally 100% coverage of all scholarly output. This project improves recall by taking the **union of three sources**, but papers can still be missing because of indexing delays, incomplete metadata, absent abstracts, database coverage, or API availability.

If one service temporarily rate-limits or fails, the script prints the error and continues with the other sources rather than failing the entire daily report.
