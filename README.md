# UK VAT Identifier Discovery — Veridion internship challenge

This repository contains a **documented research investigation**, not a commercial VAT database. It tests whether UK company VAT identifiers can be discovered from the open web and verified against the correct legal entity.

**Result (21 November 2026):** 30 companies examined; 3 VAT mappings found through manual research and checked on HMRC; general-search Python and postcode-directory Python found 0/30; combined Python live run discovered **1/30 candidate**, which matches one already HMRC-confirmed mapping. The data is not sufficiently representative to make national coverage or precision claims.

## Read first

- [**Report (Word, with HMRC screenshots)**](REPORT.docx) — the three required parts and appendix.
- [Report (Markdown)](REPORT.md) — text version for browsing on GitHub.
- [`companies.csv`](companies.csv) — actual working input.
- [`experiment_3_results.csv`](experiment_3_results.csv) — all 30 live results.
- [`experiment_3_candidates.csv`](experiment_3_candidates.csv) — the single candidate with URL/snippet.
- [`experiment_3_audit.csv`](experiment_3_audit.csv) — URLs and status codes, including dead ends.
- [Evidence index](EVIDENCE_INDEX.md), [GB Sales](hmrc_1_gb_sales.png), [Lathams](hmrc_2_lathams.png) and [Warwick Burt](hmrc_3_warwick_burt.png) — three official HMRC checks and the manual research log.

## Run the combined program (from this repository folder)

Python 3.9+ and a network connection are required for live discovery. From the repository root:

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest -v test_pipeline
python3 vat_pipeline.py --demo --input companies.csv
python3 vat_pipeline.py --live --input companies.csv --limit 3
python3 vat_pipeline.py --live --input companies.csv
```

- `--demo` uses **synthetic examples embedded directly in Python source** and **does not count as open-web discovery**.
- `--live` uses public search results and web pages, with access restrictions respected. Results may vary by date.
- Outputs go to `rezultate_live/` unless `--out` is specified. Do not assume an unreachable site has no VAT.
- A provided Company Number/postcode is an input hint, **not freshly validated** without an optional Companies House API key. The three `hmrc_confirmed_manual.csv` records are **displayed for comparison after extraction**; they are not candidate-generation inputs. Do **not** publish API credentials.
- The program **does not verify numbers against HMRC**. Manual HMRC checks are joined only after discovery.

For a names-only input, obtain a Companies House developer API key from https://developer.company-information.service.gov.uk/get-started and set `COMPANIES_HOUSE_API_KEY` privately in your shell. Do not copy any secret to this repository. Exact-name matching remains unproven in the supplied live run.

## What was attempted

| Attempt | Results | Stored evidence |
|---|---|---|
| General web search | 0/30; irrelevant URLs and access errors | `experiment_1_*` |
| Postcode directory | 0/30; 28 access-limited; 2 lacked postcode in this version of input | `experiment_2_terminal_record.txt` |
| Combined Python | 1/30 candidate; 90 queries, 179 page requests | `experiment_3_*` |
| Independent manual checks | 3 valid VAT numbers and legal names confirmed on HMRC | `hmrc_confirmed_manual.csv`, `hmrc_*.png` |

Sample caveat: a seeded random selection from a November 2026 Companies House snapshot was adapted with **two manually substituted England firms**. It is exploratory, not representative of a customer's real 40,000 suppliers; see the report.

## Debate topics (requested by the brief)

### Could we exploit UK VAT checksum validity to probe HMRC numbers?

A checksum merely narrows syntactically possible numbers; it does **not map** a number to a company. Mass enumeration would create substantial traffic, likely violate intended service usage/rate limits and produce no reliable company assignment without further evidence. **Do not enumerate or brute-force VAT numbers.** Use HMRC to validate already-discovered, attributable candidates [HMRC API](https://developer.service.hmrc.gov.uk/api-documentation/docs/api/service/vat-registered-companies-api/2.0).

### How would the data be kept current?

Store last-seen source timestamps, source URLs, registered company identity and HMRC validation date. Recheck on company status/address changes, source changes and periodic refresh; explicitly expire stale mappings. Preserve old states for audit, and support customer corrections/deregistrations.

### How could wrong data be detected without a complete ground truth?

Independently audit a sample of accepted mappings (not just those flagged uncertain), compare returned HMRC legal entity with the supplier's legal name/address and audit rejection cases. Track wrong-link rates, source drift, suspicious VAT reuse and manual corrections. Treat the three correct checks in this report as **too few** to establish commercial precision.

### Which sources would be unsuitable for a sold data product?

Do not bulk harvest or resell from commercial directories or sites with unclear rights simply because pages can be read. Explicitly review permissions, database rights, contract terms, rate limits and data provenance; license data where necessary. The current third-party lead is for research and validation, not proof of commercial reuse rights.

## Beyond the UK (brief comparison, optional)

**Romania** has an official public ANAF register for checking VAT registration, and the Romanian VAT identifier is tied more closely to the domestic fiscal identifier than the UK Company Number is to the VAT number. Therefore, once a Romanian company's fiscal ID is reliably resolved, verification may be substantially easier than UK name-to-VAT discovery. This is a hypothesis for market prioritisation, not evidence of a production-ready Romanian pipeline. See [ANAF](https://www.anaf.ro/RegistruTVA/). Separately, [VIES](https://europa.eu/youreurope/business/finance-and-tax/vat/check-vat-number-vies/index_en.htm) checks supplied EU VAT IDs but is not a reverse name-to-ID service.

## Constraints and reproducibility

Python scripts are exploratory. The original search software sometimes returned irrelevant results. Source content may change, and rate-limited sites are not bypassed. The report distinguishes **manual discoveries**, **actual live script discoveries**, **unverified candidates**, and **synthetic demonstration fixtures**. The repository contains no original enormous Companies House bulk snapshot and no private API keys. The sample is exploratory; the code is not a production-ready service.

## AI contribution and researcher testing

ChatGPT provided the list of companies and helped write, debug and document the Python scripts. I personally ran all three programs on my Mac, examined the real CSV and terminal results, checked three VAT numbers manually on HMRC and reviewed the report. Synthetic examples are clearly separated from live findings.
