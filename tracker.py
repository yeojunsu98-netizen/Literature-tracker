#!/usr/bin/env python3
"""$0 daily literature tracker.

Sources:
  - Semantic Scholar Academic Graph API
  - Crossref REST API
  - arXiv API

No paid API or LLM is used. Relevance is determined by configurable keyword scoring.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import xml.etree.ElementTree as ET
import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yml"
STATE_PATH = ROOT / "state" / "seen.json"
REPORT_DIR = ROOT / "reports"
META_PATH = REPORT_DIR / "run_meta.json"

USER_AGENT = "zero-literature-tracker/1.0 (GitHub Actions; non-commercial research monitoring)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class Paper:
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    publication_date: str = ""
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""
    sources: list[str] = field(default_factory=list)
    score: int = 0
    matched_keywords: list[str] = field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = TAG_RE.sub(" ", text)
    text = WS_RE.sub(" ", text).strip()
    return text


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.strip()


def normalize_title(title: str) -> str:
    t = clean_text(title).lower()
    return NON_ALNUM_RE.sub(" ", t).strip()


def title_hash(title: str) -> str:
    norm = normalize_title(title)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:20]


def paper_key(p: Paper) -> str:
    if p.doi:
        return f"doi:{normalize_doi(p.doi)}"
    if p.arxiv_id:
        return f"arxiv:{p.arxiv_id.lower()}"
    return f"title:{title_hash(p.title)}"


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_seen(seen: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def request_json(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                 retries: int = 5, timeout: int = 45) -> dict[str, Any]:
    merged_headers = {}
    if headers:
        merged_headers.update(headers)
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, headers=merged_headers, timeout=timeout)
            if r.status_code == 429:
                wait = min(60, 2 ** (attempt + 2))
                print(f"  rate limited (429); sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                wait = min(30, 2 ** (attempt + 1))
                print(f"  server error {r.status_code}; sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            wait = min(30, 2 ** (attempt + 1))
            print(f"  request error: {e}; sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Request failed after retries: {url}: {last_exc}")


def semantic_scholar_search(query: str, cutoff: datetime, max_pages: int, api_key: str = "") -> list[Paper]:
    print(f"[Semantic Scholar] {query}")
    url = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
    fields = "title,abstract,year,venue,authors,url,publicationDate,externalIds"
    headers = {"x-api-key": api_key} if api_key else None
    token = None
    out: list[Paper] = []

    for page in range(max_pages):
        params: dict[str, Any] = {
            "query": query,
            "fields": fields,
            "sort": "publicationDate:desc",
        }
        if token:
            params["token"] = token
        try:
            data = request_json(url, params=params, headers=headers)
        except RuntimeError as e:
            print(f"  skipped Semantic Scholar query after error: {e}", file=sys.stderr)
            break

        rows = data.get("data") or []
        if not rows:
            break
        older_seen = False
        for item in rows:
            pub = clean_text(item.get("publicationDate"))
            dt = parse_dt(pub)
            if dt and dt < cutoff:
                older_seen = True
                continue
            # If publicationDate is absent, do not treat it as newly published.
            if not dt:
                continue
            ext = item.get("externalIds") or {}
            doi = normalize_doi(ext.get("DOI"))
            arxiv_id = clean_text(ext.get("ArXiv"))
            authors = [clean_text(a.get("name")) for a in (item.get("authors") or []) if a.get("name")]
            out.append(Paper(
                title=clean_text(item.get("title")),
                abstract=clean_text(item.get("abstract")),
                authors=authors,
                venue=clean_text(item.get("venue")),
                publication_date=pub,
                doi=doi,
                arxiv_id=arxiv_id,
                url=clean_text(item.get("url")),
                sources=["Semantic Scholar"],
            ))
        token = data.get("token")
        # Sorted newest-first. Once this page includes older records, later pages will be older too.
        if older_seen or not token:
            break
        time.sleep(1.0 if not api_key else 0.2)
    return out


def crossref_date(item: dict[str, Any]) -> str:
    # Prefer online/issued publication dates; fall back to Crossref created timestamp.
    for key in ("published-online", "published-print", "published", "issued"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if parts and parts[0]:
            nums = parts[0]
            try:
                y = int(nums[0]); m = int(nums[1]) if len(nums) > 1 else 1; d = int(nums[2]) if len(nums) > 2 else 1
                return f"{y:04d}-{m:02d}-{d:02d}"
            except (ValueError, TypeError):
                pass
    created = (item.get("created") or {}).get("date-time")
    if created:
        return clean_text(created)[:10]
    return ""


def crossref_search(query: str, cutoff: datetime, now: datetime, max_pages: int, mailto: str = "") -> list[Paper]:
    print(f"[Crossref] {query}")
    url = "https://api.crossref.org/works"
    cursor = "*"
    out: list[Paper] = []
    from_ts = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for _page in range(max_pages):
        params: dict[str, Any] = {
            "query.bibliographic": query,
            # index-date catches new deposits and metadata refreshes; seen.json removes repeats.
            "filter": f"from-index-date:{from_ts},until-index-date:{until_ts}",
            "rows": 1000,
            "cursor": cursor,
        }
        if mailto:
            params["mailto"] = mailto
        try:
            data = request_json(url, params=params)
        except RuntimeError as e:
            print(f"  skipped Crossref query after error: {e}", file=sys.stderr)
            break
        message = data.get("message") or {}
        items = message.get("items") or []
        if not items:
            break
        for item in items:
            titles = item.get("title") or []
            title = clean_text(titles[0] if titles else "")
            if not title:
                continue
            authors = []
            for a in item.get("author") or []:
                name = " ".join(x for x in [clean_text(a.get("given")), clean_text(a.get("family"))] if x)
                if name:
                    authors.append(name)
            containers = item.get("container-title") or []
            doi = normalize_doi(item.get("DOI"))
            out.append(Paper(
                title=title,
                abstract=clean_text(item.get("abstract")),
                authors=authors,
                venue=clean_text(containers[0] if containers else ""),
                publication_date=crossref_date(item),
                doi=doi,
                url=(f"https://doi.org/{doi}" if doi else clean_text(item.get("URL"))),
                sources=["Crossref"],
            ))
        next_cursor = message.get("next-cursor")
        if not next_cursor or next_cursor == cursor or len(items) < 1000:
            break
        cursor = next_cursor
        time.sleep(0.2)
    return out


def arxiv_search(query: str, cutoff: datetime, max_pages: int) -> list[Paper]:
    print(f"[arXiv] {query}")
    url = "https://export.arxiv.org/api/query"
    out: list[Paper] = []
    page_size = 100
    atom_ns = "http://www.w3.org/2005/Atom"
    arxiv_ns = "http://arxiv.org/schemas/atom"
    ns = {"a": atom_ns, "x": arxiv_ns}

    # arXiv's legacy API supports fielded search. Quoting keeps multi-word terms together.
    search_query = f'all:"{query.replace(chr(34), "")}"'
    for page in range(max_pages):
        params = {
            "search_query": search_query,
            "start": page * page_size,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        try:
            r = SESSION.get(url, params=params, timeout=60)
            r.raise_for_status()
            root = ET.fromstring(r.text)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"  skipped arXiv query after error: {e}", file=sys.stderr)
            break
        entries = root.findall("a:entry", ns)
        if not entries:
            break
        older_seen = False
        for entry in entries:
            def txt(path: str) -> str:
                el = entry.find(path, ns)
                return clean_text(el.text if el is not None else "")

            published = txt("a:published")
            updated = txt("a:updated")
            dt = parse_dt(published) or parse_dt(updated)
            if dt and dt < cutoff:
                older_seen = True
                continue
            if not dt:
                continue
            entry_id = txt("a:id")
            m = re.search(r"arxiv\.org/abs/([^v/?#]+)", entry_id, re.I)
            arxiv_id = m.group(1) if m else ""
            doi = normalize_doi(txt("x:doi"))
            authors = [clean_text(a.text) for a in entry.findall("a:author/a:name", ns) if a.text]
            primary = entry.find("x:primary_category", ns)
            venue = clean_text(primary.attrib.get("term") if primary is not None else "arXiv")
            out.append(Paper(
                title=txt("a:title"),
                abstract=txt("a:summary"),
                authors=authors,
                venue=venue or "arXiv",
                publication_date=dt.date().isoformat(),
                doi=doi,
                arxiv_id=arxiv_id,
                url=entry_id,
                sources=["arXiv"],
            ))
        if older_seen or len(entries) < page_size:
            break
        # Be conservative with arXiv request rate.
        time.sleep(3.0)
    return out


def merge_papers(papers: Iterable[Paper]) -> list[Paper]:
    merged: dict[str, Paper] = {}
    title_to_key: dict[str, str] = {}
    for p in papers:
        if not p.title:
            continue
        key = paper_key(p)
        tnorm = normalize_title(p.title)
        # If DOI/arXiv differs but normalized title is identical, merge anyway.
        existing_key = title_to_key.get(tnorm)
        if existing_key and existing_key in merged:
            key = existing_key
        if key not in merged:
            merged[key] = p
            title_to_key[tnorm] = key
            continue
        a = merged[key]
        if len(p.abstract) > len(a.abstract):
            a.abstract = p.abstract
        if not a.doi and p.doi:
            a.doi = p.doi
        if not a.arxiv_id and p.arxiv_id:
            a.arxiv_id = p.arxiv_id
        if not a.url and p.url:
            a.url = p.url
        if not a.venue and p.venue:
            a.venue = p.venue
        if not a.publication_date and p.publication_date:
            a.publication_date = p.publication_date
        if len(p.authors) > len(a.authors):
            a.authors = p.authors
        a.sources = sorted(set(a.sources + p.sources))
    return list(merged.values())


def score_paper(p: Paper, keyword_weights: dict[str, int], exclude_phrases: list[str]) -> tuple[int, list[str], bool]:
    hay = " ".join([p.title, p.abstract, p.venue]).lower()
    for phrase in exclude_phrases:
        if phrase.lower() in hay:
            return 0, [], True
    score = 0
    matched = []
    for phrase, weight in keyword_weights.items():
        if phrase.lower() in hay:
            score += int(weight)
            matched.append(phrase)
    # Title hits are particularly informative; add a small bonus once per matching phrase.
    title_lower = p.title.lower()
    for phrase, weight in keyword_weights.items():
        if phrase.lower() in title_lower:
            score += max(1, int(weight) // 2)
    return score, matched, False


def paper_sort_key(p: Paper) -> tuple[int, str, str]:
    return (p.score, p.publication_date or "", p.title.lower())


def markdown_escape(text: str) -> str:
    return text.replace("|", "\\|")


def write_reports(papers: list[Paper], raw_new_count: int, run_day: str, project_name: str) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"{run_day}.md"
    csv_path = REPORT_DIR / f"{run_day}.csv"

    lines = [
        f"# {project_name} — {run_day}",
        "",
        f"- **New candidate records across sources:** {raw_new_count}",
        f"- **Papers passing keyword filter:** {len(papers)}",
        "- **Sources:** Semantic Scholar, Crossref, arXiv",
        "- **Cost:** $0 (no paid API / no LLM)",
        "",
    ]
    if not papers:
        lines += ["No new papers passed the configured relevance threshold today.", ""]
    else:
        lines += ["## New papers", ""]
        for i, p in enumerate(sorted(papers, key=paper_sort_key, reverse=True), 1):
            link = p.url or (f"https://doi.org/{p.doi}" if p.doi else "")
            title_line = f"### {i}. {p.title}"
            if link:
                title_line = f"### {i}. [{p.title}]({link})"
            lines += [title_line, ""]
            if p.authors:
                author_display = ", ".join(p.authors[:12]) + (" et al." if len(p.authors) > 12 else "")
                lines.append(f"**Authors:** {author_display}  ")
            if p.venue:
                lines.append(f"**Venue/category:** {p.venue}  ")
            if p.publication_date:
                lines.append(f"**Date:** {p.publication_date}  ")
            if p.doi:
                lines.append(f"**DOI:** `{p.doi}`  ")
            if p.arxiv_id:
                lines.append(f"**arXiv:** `{p.arxiv_id}`  ")
            lines.append(f"**Source(s):** {', '.join(p.sources)}  ")
            lines.append(f"**Keyword score:** {p.score}  ")
            if p.matched_keywords:
                lines.append(f"**Matched:** {', '.join(p.matched_keywords)}  ")
            lines.append("")
            if p.abstract:
                lines += ["**Abstract**", "", p.abstract, ""]
            lines.append("---")
            lines.append("")

    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    fields = ["title", "authors", "venue", "publication_date", "doi", "arxiv_id", "url", "sources", "score", "matched_keywords", "abstract"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in sorted(papers, key=paper_sort_key, reverse=True):
            w.writerow({
                "title": p.title,
                "authors": "; ".join(p.authors),
                "venue": p.venue,
                "publication_date": p.publication_date,
                "doi": p.doi,
                "arxiv_id": p.arxiv_id,
                "url": p.url,
                "sources": "; ".join(p.sources),
                "score": p.score,
                "matched_keywords": "; ".join(p.matched_keywords),
                "abstract": p.abstract,
            })
    return md_path, csv_path


def main() -> int:
    cfg = load_config()
    seen = load_seen()
    now = utcnow()
    bootstrap_days = int(cfg.get("bootstrap_days", 7))
    lookback_hours = int(cfg.get("lookback_hours", 40))
    cutoff = now - (timedelta(days=bootstrap_days) if not seen else timedelta(hours=lookback_hours))
    print(f"Tracking window: {cutoff.isoformat()} -> {now.isoformat()}")

    queries = [str(q).strip() for q in cfg.get("queries", []) if str(q).strip()]
    caps = cfg.get("max_pages_per_query") or {}
    s2_pages = int(caps.get("semantic_scholar", 3))
    cr_pages = int(caps.get("crossref", 5))
    ax_pages = int(caps.get("arxiv", 3))
    s2_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    mailto = str(cfg.get("crossref_mailto") or "").strip()

    all_papers: list[Paper] = []
    for query in queries:
        all_papers.extend(semantic_scholar_search(query, cutoff, s2_pages, s2_key))
        all_papers.extend(crossref_search(query, cutoff, now, cr_pages, mailto))
        all_papers.extend(arxiv_search(query, cutoff, ax_pages))

    merged = merge_papers(all_papers)
    print(f"Fetched {len(all_papers)} records; {len(merged)} after source-level deduplication.")

    # A paper counts as new if neither its primary key nor its exact normalized title was seen before.
    seen_titles = {k for k in seen if k.startswith("title:")}
    new_candidates: list[Paper] = []
    for p in merged:
        key = paper_key(p)
        tkey = f"title:{title_hash(p.title)}"
        if key in seen or tkey in seen_titles or tkey in seen:
            continue
        new_candidates.append(p)

    keyword_weights = {str(k): int(v) for k, v in (cfg.get("keywords") or {}).items()}
    excludes = [str(x) for x in (cfg.get("exclude_phrases") or [])]
    min_score = int(cfg.get("min_score", 1))
    kept: list[Paper] = []
    for p in new_candidates:
        score, matched, excluded = score_paper(p, keyword_weights, excludes)
        p.score = score
        p.matched_keywords = matched
        if not excluded and score >= min_score:
            kept.append(p)

    # Mark every candidate as seen, including filtered-out noise, so the overlap window doesn't reprocess it daily.
    stamp = now.isoformat()
    for p in new_candidates:
        seen[paper_key(p)] = stamp
        seen[f"title:{title_hash(p.title)}"] = stamp
    save_seen(seen)

    run_day = now.date().isoformat()
    md_path, csv_path = write_reports(
        kept,
        raw_new_count=len(new_candidates),
        run_day=run_day,
        project_name=str(cfg.get("project_name") or "Daily Literature Tracker"),
    )
    meta = {
        "run_day": run_day,
        "report_path": str(md_path.relative_to(ROOT)),
        "csv_path": str(csv_path.relative_to(ROOT)),
        "new_candidate_count": len(new_candidates),
        "kept_count": len(kept),
        "create_github_issue": bool(cfg.get("create_github_issue", True)),
    }
    # GitHub issue bodies have a size limit; keep a compact/truncated copy for notification.
    issue_path = REPORT_DIR / "issue_body.md"
    full_body = md_path.read_text(encoding="utf-8")
    if len(full_body) > 50000:
        full_body = full_body[:50000].rstrip() + f"\n\n---\nFull report is saved in `{md_path.relative_to(ROOT)}`.\n"
    issue_path.write_text(full_body, encoding="utf-8")
    meta["issue_body_path"] = str(issue_path.relative_to(ROOT))
    META_PATH.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
