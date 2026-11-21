#!/usr/bin/env python3
"""UK VAT discovery pilot: company names -> source-backed VAT candidates.

LIVE mode searches the web, fetches allowed public HTML pages, and extracts
numbers near a full legal company name. It never guesses a missing VAT number,
and it never calls HMRC or claims that an extracted number has been verified.

The separate file hmrc_confirmed_manual.csv records independent human checks.
Values in that file are NOT fed into discovery and are kept in their own output
column to avoid claiming the script automatically discovered them.

DEMO mode uses LOCAL EXAMPLE PAGES supplied with this project. It is not
independent web discovery. The project can run its demo without ddgs installed.
"""
from __future__ import annotations

import argparse
import csv
import ipaddress
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, quote
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parent
# Synthetic demo pages, not web discoveries.
DEMO_HTML = {'12952353': '<html><body><h2>GB SALES LTD</h2><p>Registered company no 12952353. VAT Number: GB 424 3195 13</p>\n<p>Another company: RANDOM LIMITED VAT Number: GB123456789</p></body></html>\n', '02811195': '<html><body><ul><li>ALPHA LTD — VAT GB111222333</li><li>WARWICK BURT CONSTRUCTION LIMITED — VAT GB623619347</li><li>OMEGA LTD — VAT GB999999999</li></ul></body></html>\n', '11383949': '<html><body><p>LATHAMS HOLDINGS LIMITED. Registered address: B69 2NY. VAT Reg No GB405235432.</p></body></html>\n'}
UA = "VeridionInternshipUKVATPoC/1.0 (student research; respectful crawling)"
MAX_HTML_BYTES = 1_000_000
TIMEOUT_SECONDS = 12
SUMMARY_FIELDS = [
    "sample_id", "company_name", "company_number", "vat_found_by_python",
    "python_source_url", "python_evidence", "python_status",
    "hmrc_confirmed_from_manual_research", "final_confirmed_vat",
    "comparison_with_manual_check", "hmrc_confirmation_source", "research_note"
]
EVIDENCE_FIELDS = [
    "sample_id", "company_name", "company_number", "vat_candidate",
    "source_url", "evidence_type", "text_context", "checked_at_utc"
]
LOG_FIELDS = ["sample_id", "company_name", "event", "query_or_url", "status", "details"]


@dataclass(frozen=True)
class Candidate:
    vat: str
    url: str
    source_type: str
    context: str


class TextExtractor(HTMLParser):
    """Basic HTML -> readable text, without JavaScript, CSS or hidden blocks."""

    SKIP_TAGS = {"script", "style", "noscript", "svg"}
    SEPARATORS = {"div", "p", "li", "tr", "td", "br", "article", "section", "footer", "header", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.skipped: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP_TAGS:
            self.skipped.append(tag)
        if tag in self.SEPARATORS:
            self.fragments.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.skipped and tag == self.skipped[-1]:
            self.skipped.pop()
        if tag in self.SEPARATORS:
            self.fragments.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skipped:
            self.fragments.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.fragments).split())


def html_to_text(html: str) -> str:
    parser = TextExtractor()
    parser.feed(html)
    return parser.text()


def company_pattern(company_name: str) -> re.Pattern:
    """Match the full legal name; LTD and LIMITED are treated as equivalent."""
    tokens = re.findall(r"[A-Z0-9]+", company_name.upper())
    if not tokens:
        raise ValueError("Empty company name")
    terms = ["(?:LIMITED|LTD)" if i == len(tokens) - 1 and s in ("LTD", "LIMITED") else re.escape(s)
             for i, s in enumerate(tokens)]
    return re.compile(r"(?<![A-Z0-9])" + r"[^A-Z0-9]+".join(terms) + r"(?![A-Z0-9])")


# Nine digits with optional spaces, dots or hyphens. UK VAT numbers are not
# assumed to be valid just because they match this format.
VAT_SEQUENCE = r"(?:\d[.\s-]*){8}\d"
VAT_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?:(?:VAT\s*(?:REG(?:ISTRATION|ISTERED|\.)?\s*)?(?:NUMBER|NO\.?|ID)?\s*[:#-]?\s*)"
    r"|(?:GB\s*[.:-]?\s*))(?P<digits>" + VAT_SEQUENCE + r")(?!\d)",
    re.IGNORECASE,
)
LEGAL_ENDING = re.compile(r"\b(?:LTD|LIMITED|PLC|LLP)\b", re.IGNORECASE)


def find_vats_near_name(text: str, company_name: str) -> list[tuple[str, str]]:
    """Only accept a candidate when a complete name and VAT prefix/label are nearby.

    A directory may contain dozens of companies and VAT numbers. A number on
    the same web page is NOT sufficient evidence for a match.
    """
    text = " ".join(text.split())
    upper = text.upper()
    names = list(company_pattern(company_name).finditer(upper))
    vats = list(VAT_PATTERN.finditer(upper))
    matches: dict[str, str] = {}
    for name_match in names:
        for vat_match in vats:
            # Only numbers that FOLLOW the full legal name are considered.
            # Looking backwards can accidentally steal the previous entry's
            # VAT number from a directory of unrelated businesses.
            if vat_match.start() < name_match.end():
                continue
            gap = vat_match.start() - name_match.end()
            if gap > 165:
                continue
            between = upper[name_match.end():vat_match.start()]
            # If another company's legal ending appears in the gap, the VAT
            # probably belongs to a different list entry.
            if LEGAL_ENDING.search(between):
                continue
            digits = re.sub(r"\D", "", vat_match.group("digits"))
            if len(digits) != 9:
                continue
            start = max(0, min(name_match.start(), vat_match.start()) - 30)
            end = min(len(text), max(name_match.end(), vat_match.end()) + 35)
            matches[digits] = text[start:end]
    return sorted(matches.items())


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)



def ascii_safe_url(url: str) -> str:
    """Prepare a search-result URL for urllib (RFC 3986 ASCII-safe URL).

    A search engine may return URLs containing literal Unicode characters,
    e.g. /căutare or ?query=firmă. HTTP request targets must use percent-
    encoded UTF-8, while existing %HH escapes must not be encoded again.
    """
    u = urlsplit(url)
    if not u.scheme or not u.netloc:
        return url
    host = (u.hostname or "").encode("idna").decode("ascii")
    # Fetcher only accepts HTTPS public hosts without credentials or ports.
    # Reject those before normalizing so we don't silently change their meaning.
    if not valid_web_url(url):
        return url
    return urlunsplit((
        u.scheme, host,
        quote(u.path, safe="/%:@!$&'()*+,;=-._~"),
        quote(u.query, safe="=&%/?+,:@!$'()*;-._~"),
        "",  # fragments are client-side only and are not sent to servers
    ))


def valid_web_url(url: str) -> bool:
    try:
        u = urlsplit(url)
        host = u.hostname or ""
        # Only fetch remote public-looking HTTPS URLs, not local files/ports.
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            public_host = host not in {"localhost"} and not host.endswith((".local", ".internal"))
        else:
            public_host = addr.is_global
        return (u.scheme == "https" and bool(host) and "." in host and
                public_host and not u.username and not u.password and not u.port)
    except ValueError:
        return False


class WebPageReader:
    """Robots-aware simple HTML fetcher. No proxy, CAPTCHA bypass or retry flood."""

    def __init__(self, pause: float = 1.0) -> None:
        self.robots: dict[str, RobotFileParser | str] = {}
        self.pages: dict[str, tuple[str, str]] = {}
        self.pause = pause
        self.last_fetch = 0.0

    def get_robots(self, host: str) -> RobotFileParser | str:
        if host in self.robots:
            return self.robots[host]
        url = f"https://{host}/robots.txt"
        try:
            with urlopen(Request(url, headers={"User-Agent": UA}), timeout=TIMEOUT_SECONDS) as resp:
                content = resp.read(200_000).decode("utf-8", "replace")
            rp = RobotFileParser()
            rp.parse(content.splitlines())
            self.robots[host] = rp
        except HTTPError as e:
            if e.code == 404:
                rp = RobotFileParser()
                rp.parse(["User-agent: *", "Allow: /"])
                self.robots[host] = rp
            else:
                self.robots[host] = f"ROBOTS_HTTP_{e.code}"
        except (URLError, TimeoutError) as e:
            self.robots[host] = "ROBOTS_UNAVAILABLE"
        return self.robots[host]

    def fetch(self, url: str) -> tuple[str, str]:
        if not valid_web_url(url):
            return "SKIPPED_UNSAFE_URL", ""
        try:
            url = ascii_safe_url(url)
        except (ValueError, UnicodeError):
            return "SKIPPED_MALFORMED_URL", ""
        if url in self.pages:
            return self.pages[url]
        host = urlsplit(url).hostname or ""
        rp = self.get_robots(host)
        if isinstance(rp, str):
            return rp, ""
        if not rp.can_fetch(UA, url):
            return "ROBOTS_DISALLOWED", ""
        wait = self.pause - (time.monotonic() - self.last_fetch)
        if wait > 0:
            time.sleep(wait)
        self.last_fetch = time.monotonic()
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                if not valid_web_url(resp.geturl()):
                    answer = ("REDIRECT_UNSAFE", "")
                elif "text/html" not in resp.headers.get("Content-Type", "").lower():
                    answer = ("NOT_HTML", "")
                else:
                    raw = resp.read(MAX_HTML_BYTES + 1)
                    if len(raw) > MAX_HTML_BYTES:
                        answer = ("PAGE_TOO_LARGE", "")
                    else:
                        # HTML encoding sometimes differs by site. Replacement
                        # chars are safer than crashing on a single bad byte.
                        answer = ("OK", html_to_text(raw.decode("utf-8", "replace")))
        except HTTPError as e:
            answer = (f"HTTP_{e.code}", "")
        except (URLError, TimeoutError, OSError, UnicodeError, ValueError):
            answer = ("NETWORK_ERROR", "")
        self.pages[url] = answer
        return answer


def search_company(name: str, max_results: int) -> list[dict]:
    """Use publicly accessible meta-search (not a guaranteed production API)."""
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError("Lipsește pachetul ddgs. Rulează: python3 -m pip install ddgs") from exc
    queries = [f'"{name}" "VAT"', f'"{name}" "GB" "VAT number"']
    results: list[dict] = []
    search = DDGS(timeout=12)
    for query in queries:
        try:
            findings = search.text(query, region="uk-en", max_results=max_results) or []
            results.append({"query": query, "result": None, "error": ""})
            for item in findings:
                results.append({"query": query, "result": item, "error": ""})
        except Exception as e:
            results.append({"query": query, "result": None, "error": f"SEARCH_ERROR_{type(e).__name__}: {str(e)[:120]}"})
        time.sleep(1.0)
    return results


def demo_search_company(number: str) -> list[dict]:
    """Local illustrative pages, built from the already known manual results."""
    if number not in DEMO_HTML:
        return []
    return [{"query": "LOCAL_DEMO_FIXTURE", "result": {"href": f"https://demo.invalid/{number}", "title": "DEMO", "body": ""}, "error": ""}]


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(input_path: Path, out_dir: Path, manual_path: Path, mode: str,
        limit: int, max_results: int, max_pages: int) -> tuple[list[dict], list[dict], list[dict]]:
    companies = read_csv(input_path)
    if not companies or "company_name" not in companies[0]:
        raise ValueError("companies.csv needs at least a company_name column")
    if limit > 0:
        companies = companies[:limit]
    verified = {}
    if manual_path.exists():
        for entry in read_csv(manual_path):
            verified[entry["company_number"]] = entry

    page_reader = WebPageReader()
    summary: list[dict] = []
    evidence: list[dict] = []
    log: list[dict] = []

    for idx, company in enumerate(companies, 1):
        name = company["company_name"].strip()
        number = company.get("company_number", "").strip()
        sample_id = company.get("sample_id", str(idx))
        found: dict[tuple[str, str], Candidate] = {}
        search_ok = False
        search_error = False
        page_errors = 0
        processed_urls = set()
        try:
            search_results = (demo_search_company(number) if mode == "demo" else
                              search_company(name, max_results))
        except RuntimeError as e:
            search_results = [{"query": "", "result": None, "error": str(e)}]

        page_count = 0
        for item in search_results:
            query = item["query"]
            if item["error"]:
                search_error = True
                log.append({"sample_id": sample_id, "company_name": name, "event": "SEARCH", "query_or_url": query,
                            "status": "ERROR", "details": item["error"]})
                continue
            if item["result"] is None:
                search_ok = True
                log.append({"sample_id": sample_id, "company_name": name, "event": "SEARCH", "query_or_url": query,
                            "status": "OK", "details": "Search request completed; results may be empty"})
                continue
            search_ok = True
            result = item["result"]
            url = (result.get("href") or result.get("url") or "").strip()
            # Search engines may return literal Unicode in links. Quote the
            # URL before urllib, logging and saving it consistently.
            if valid_web_url(url):
                try:
                    url = ascii_safe_url(url)
                except (ValueError, UnicodeError):
                    pass
            if not url or url in processed_urls or page_count >= max_pages:
                continue
            processed_urls.add(url)
            page_count += 1
            if mode == "demo":
                status, page = "DEMO_LOCAL", html_to_text(DEMO_HTML[number])
            else:
                status, page = page_reader.fetch(url)
            log.append({"sample_id": sample_id, "company_name": name, "event": "PAGE", "query_or_url": url,
                        "status": status, "details": query})
            if status not in ("OK", "DEMO_LOCAL"):
                page_errors += 1
            # Only regard snippets as a weaker fallback. We do not treat a
            # search snippet as the same evidentiary quality as an opened page.
            page_matches = (find_vats_near_name(page, name)
                            if status in ("OK", "DEMO_LOCAL") else [])
            for vat, context in page_matches:
                found[(vat, url)] = Candidate(vat, url, "LOCAL_DEMO" if mode == "demo" else "FETCHED_PAGE", context)
            if mode == "live" and not page_matches:
                snippet = (result.get("title", "") + " " + result.get("body", ""))
                for vat, context in find_vats_near_name(snippet, name):
                    found[(vat, url)] = Candidate(vat, url, "SEARCH_SNIPPET_ONLY", context)

        candidates = list(found.values())
        candidates.sort(key=lambda c: (0 if c.source_type in ("FETCHED_PAGE", "LOCAL_DEMO") else 1, c.vat, c.url))
        distinct = sorted({c.vat for c in candidates})
        if not distinct:
            if mode == "demo" and not search_results:
                status = "DEMO_NO_PAGE_PROVIDED"
            elif search_error and not search_ok:
                status = "SEARCH_FAILED"
            elif page_errors and not candidates:
                status = "NO_CANDIDATE_ACCESS_LIMITATIONS"
            else:
                status = "NOT_FOUND_IN_SCANNED_SOURCES"
            selected = None
        elif len(distinct) > 1:
            status = "MULTIPLE_CANDIDATES_REVIEW"
            selected = None
        else:
            selected = candidates[0]
            status = "DEMO_ONLY" if mode == "demo" else ("POSSIBLE_VAT_FROM_PAGE" if selected.source_type == "FETCHED_PAGE" else "POSSIBLE_VAT_FROM_SNIPPET")

        for candidate in candidates:
            evidence.append({
                "sample_id": sample_id, "company_name": name, "company_number": number,
                "vat_candidate": candidate.vat, "source_url": candidate.url,
                "evidence_type": candidate.source_type, "text_context": candidate.context,
                "checked_at_utc": timestamp(),
            })
        manual = verified.get(number, {})
        manual_vat = manual.get("vat_number", "")
        comparison = ("MATCHES_PRIOR_HMRC_CHECK" if selected and manual_vat and selected.vat == manual_vat
                      else "CONFLICTS_WITH_PRIOR_HMRC_CHECK" if selected and manual_vat and selected.vat != manual_vat
                      else "PRIOR_HMRC_CHECK_ONLY" if manual_vat
                      else "NO_PRIOR_HMRC_CHECK")
        # Manual evidence is independent: never pretend a confirmed VAT was
        # found by Python if this live or demo run did not discover it.
        summary.append({
            "sample_id": sample_id, "company_name": name, "company_number": number,
            "vat_found_by_python": selected.vat if selected else "",
            "python_source_url": selected.url if selected and mode == "live" else "",
            "python_evidence": selected.context if selected else "",
            "python_status": status,
            "hmrc_confirmed_from_manual_research": manual_vat,
            "final_confirmed_vat": manual_vat,
            "comparison_with_manual_check": comparison,
            "hmrc_confirmation_source": manual.get("verification_method", ""),
            "research_note": ("An extracted candidate is unverified unless cross-checked against HMRC. "
                              "The separate manual column is NOT an automated discovery.")
        })
        formatted = ", ".join(distinct) if distinct else "(none)"
        print(f"{idx:2}/{len(companies)} {name[:33]:33} | {status:34} | {formatted}", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = "DEMO_" if mode == "demo" else ""
    write_csv(out_dir / f"{prefix}tabel_vat.csv", SUMMARY_FIELDS, summary)
    write_csv(out_dir / f"{prefix}surse_vat.csv", EVIDENCE_FIELDS, evidence)
    write_csv(out_dir / f"{prefix}jurnal_cautari.csv", LOG_FIELDS, log)
    count = sum(bool(row["vat_found_by_python"]) for row in summary)
    print(f"\nPython candidates: {count}/{len(summary)} companies (not HMRC-verified by this script).")
    print(f"Independent HMRC confirmations already supplied by user: {sum(bool(x['final_confirmed_vat']) for x in summary)}")
    print(f"CSV files saved in: {out_dir}")
    return summary, evidence, log


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="read local examples, not the internet")
    mode.add_argument("--live", action="store_true", help="search and fetch public web pages")
    parser.add_argument("--input", type=Path, default=ROOT / "companies.csv")
    parser.add_argument("--manual", type=Path, default=ROOT / "hmrc_confirmed_manual.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "rezultate")
    parser.add_argument("--limit", type=int, default=0, help="first N companies; 0 = all")
    parser.add_argument("--max-results", type=int, default=5, help="search results per query")
    parser.add_argument("--max-pages", type=int, default=6, help="maximum different pages for each firm")
    args = parser.parse_args()
    if args.limit < 0 or args.max_results < 1 or args.max_pages < 1:
        parser.error("limit >= 0 and max-results/max-pages >= 1 required")
    if args.live:
        try:
            import ddgs  # noqa: F401; only needed for web searches
        except ImportError:
            print("Missing 'ddgs' library. Run: python3 -m pip install -r requirements.txt", file=sys.stderr)
            return 1
    try:
        run(args.input, args.out, args.manual, "demo" if args.demo else "live",
            args.limit, args.max_results, args.max_pages)
    except (OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
