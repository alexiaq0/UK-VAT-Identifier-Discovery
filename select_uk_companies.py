"""Veridion VAT discovery — create a reproducible 30-company research sample.

Place this script next to the official Companies House snapshot ZIP:
    BasicCompanyDataAsOneFile-2026-11-01.zip
Run with:
    python3 select_uk_companies.py

No third-party Python packages are needed. Does not extract the entire ZIP.
"""

import csv
import io
import random
import sys
import zipfile
from collections import Counter
from pathlib import Path

SEED = 20261120
SAMPLE_SIZE = 30
FILENAME_PREFIX = "BasicCompanyDataAsOneFile-"
ELIGIBLE_TYPES = {"private limited company", "public limited company"}

OUTPUT_FIELDS = [
    "sample_id", "company_name", "company_number", "company_status",
    "company_type", "sic_1", "accounts_category", "registered_city",
    "registered_postcode", "registered_address", "companies_house_url",
    "website_url", "vat_candidate", "vat_source_url", "source_type",
    "hmrc_check_result", "hmrc_registered_name", "company_match",
    "final_decision", "search_notes",
]


def find_snapshot():
    """Find the official full snapshot in the same folder as the script."""
    folder = Path(__file__).resolve().parent
    for suffix in (".zip", ".csv"):
        candidates = sorted(folder.glob(FILENAME_PREFIX + "*" + suffix))
        if candidates:
            return candidates[-1]
    print("Nu am gasit snapshot-ul Companies House langa acest script.")
    print("Descarca 'BasicCompanyDataAsOneFile-2026-11-01.zip' de pe:")
    print("https://download.companieshouse.gov.uk/en_output.html")
    print("Salveaza-l in acelasi folder cu scriptul, fara sa-l dezarhivezi.")
    sys.exit(1)


def iter_rows(snapshot):
    """Read only one CSV record at a time, even if the ZIP contains gigabytes."""
    if snapshot.suffix.lower() == ".zip":
        with zipfile.ZipFile(snapshot) as archive:
            entries = [entry for entry in archive.namelist()
                       if entry.lower().endswith(".csv") and not entry.startswith("__MACOSX/")]
            if len(entries) != 1:
                raise RuntimeError("Arhiva ar trebui sa contina exact un CSV; am gasit: " + repr(entries))
            with archive.open(entries[0]) as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="") as source:
                    reader = csv.DictReader(source)
                    reader.fieldnames = [(name or "").strip() for name in reader.fieldnames]
                    yield from reader
    else:
        with snapshot.open("r", encoding="utf-8-sig", errors="replace", newline="") as source:
            reader = csv.DictReader(source)
            reader.fieldnames = [(name or "").strip() for name in reader.fieldnames]
            yield from reader


def value(row, field):
    return (row.get(field) or "").strip()


def to_output(row):
    number = value(row, "CompanyNumber")
    addr_fields = [
        "RegAddress.AddressLine1", "RegAddress.AddressLine2",
        "RegAddress.PostTown", "RegAddress.County",
        "RegAddress.PostCode", "RegAddress.Country",
    ]
    address = ", ".join(filter(None, (value(row, key) for key in addr_fields)))
    return {
        "company_name": value(row, "CompanyName"),
        "company_number": number,  # keep as text; leading zeroes are important
        "company_status": value(row, "CompanyStatus"),
        "company_type": value(row, "CompanyCategory"),
        "sic_1": value(row, "SICCode.SicText_1"),
        "accounts_category": value(row, "Accounts.AccountCategory"),
        "registered_city": value(row, "RegAddress.PostTown"),
        "registered_postcode": value(row, "RegAddress.PostCode"),
        "registered_address": address,
        "companies_house_url": (
            "https://find-and-update.company-information.service.gov.uk/company/" + number
        ),
    }


def main():
    snapshot = find_snapshot()
    rng = random.Random(SEED)
    selected = []
    eligible_count = 0
    scanned_count = 0

    print(f"Citesc: {snapshot.name}")
    print("Selectez 30 companii aleatorii. Nu este nevoie sa dezarhivezi fisierul.")
    for row in iter_rows(snapshot):
        scanned_count += 1
        if value(row, "CompanyStatus").casefold() != "active":
            continue
        if value(row, "CompanyCategory").casefold() not in ELIGIBLE_TYPES:
            continue
        if not value(row, "CompanyNumber") or not value(row, "CompanyName"):
            continue

        eligible_count += 1
        company = to_output(row)
        if len(selected) < SAMPLE_SIZE:
            selected.append(company)
        else:
            # Reservoir sampling: each eligible company has the same probability.
            replace_index = rng.randrange(eligible_count)
            if replace_index < SAMPLE_SIZE:
                selected[replace_index] = company

    if eligible_count < SAMPLE_SIZE:
        raise RuntimeError(f"Prea putine companii eligibile: {eligible_count}")

    # Preserve the selected sample, but sort for convenient manual research.
    selected.sort(key=lambda company: company["company_name"].casefold())
    for number, company in enumerate(selected, 1):
        company["sample_id"] = number

    out_dir = Path(__file__).resolve().parent
    csv_path = out_dir / "uk_vat_research_30_companies.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(selected)

    methodology = out_dir / "sample_methodology.txt"
    type_counts = Counter(row["company_type"] for row in selected)
    methodology.write_text(
        "UK VAT discovery — sample methodology\n"
        "====================================\n"
        f"Official source: https://download.companieshouse.gov.uk/en_output.html\n"
        f"Input snapshot: {snapshot.name}\n"
        f"Random seed: {SEED}\n"
        f"Total registry records scanned: {scanned_count:,}\n"
        f"Eligible companies: {eligible_count:,}\n"
        f"Companies selected: {len(selected)}\n"
        "\nEligibility criteria:\n"
        "  - CompanyStatus = Active\n"
        "  - CompanyCategory = Private Limited Company or Public Limited Company\n"
        "  - CompanyName and CompanyNumber are present\n"
        "\nSampling approach:\n"
        "  Uniform random sampling without replacement (reservoir sampling),\n"
        "  with a fixed random seed to make the exact sample reproducible.\n"
        "  Company selection did NOT depend on VAT-number discoverability.\n"
        "\nLimitations:\n"
        "  1. This is a sample of active UK limited companies, NOT of the\n"
        "     manufacturer's actual 40,000 suppliers. It should not be claimed\n"
        "     to be representative of that supplier population.\n"
        "  2. Active company status does not imply VAT registration or active trading.\n"
        "  3. The registered office is not necessarily the trading location.\n"
        "  4. Thirty companies give a useful pilot, not a precise national estimate.\n"
        "  5. Missing VAT numbers are NOT evidence of non-registration.\n"
        "  6. HMRC validity checks and entity-name matching must be done\n"
        "     separately before counting a VAT as verified.\n"
        "\nType distribution in selected 30:\n"
        + "".join(f"  {k}: {v}\n" for k, v in sorted(type_counts.items()))
        + "\nEvidence to keep per company: search query, candidate URL, VAT number,\n"
          "HMRC check result and returned name, company match decision, notes.\n",
        encoding="utf-8",
    )

    print("\nGATA! Au fost create:")
    print(f"  1. {csv_path.name}")
    print(f"  2. {methodology.name}")
    print(f"Companii eligibile in sursa: {eligible_count:,}")
    print("IMPORTANT: Nu exista inca numere VAT verificate. Urmeaza research-ul.")
    print("Atentie: cand deschizi CSV in Excel, importa coloana 'company_number' ca TEXT")
    print("pentru a nu pierde zerourile de la inceputul identificatorului.")


if __name__ == "__main__":
    main()
