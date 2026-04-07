#!/usr/bin/env python3
"""Generate references.bib from a list of arXiv IDs and DOIs in sources.txt."""

import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import json
from pathlib import Path

MAX_RETRIES = 5
RETRY_DELAY = 10  # seconds


def fetch_arxiv(arxiv_id: str) -> str:
    """Fetch metadata from arXiv API and return a bib entry."""
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read().decode()

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(data)
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise ValueError(f"arXiv entry not found for {arxiv_id}")

    # Check for error
    id_elem = entry.find("atom:id", ns)
    if id_elem is not None and "error" in (id_elem.text or "").lower():
        summary = entry.find("atom:summary", ns)
        msg = summary.text.strip() if summary is not None else "unknown error"
        raise ValueError(f"arXiv API error for {arxiv_id}: {msg}")

    title_elem = entry.find("atom:title", ns)
    if title_elem is None or not title_elem.text:
        raise ValueError(f"arXiv entry for {arxiv_id} has no title")
    title = re.sub(r"\s+", " ", title_elem.text.strip())

    authors = []
    for author in entry.findall("atom:author", ns):
        name = author.find("atom:name", ns).text.strip()
        authors.append(name)
    if not authors:
        raise ValueError(f"arXiv entry for {arxiv_id} has no authors")

    published = entry.find("atom:published", ns)
    if published is None or not published.text:
        raise ValueError(f"arXiv entry for {arxiv_id} has no publication date")
    year = published.text[:4]

    # Primary category
    primary_cat = entry.find("arxiv:primary_category", ns)
    category = primary_cat.get("term", "") if primary_cat is not None else ""

    # Build citation key: first author last name + year + first word of title
    first_last = re.sub(r"[^a-z]", "", authors[0].split()[-1].lower())
    first_word = re.sub(r"[^a-z]", "", title.split()[0].lower())
    key = f"{first_last}{year}{first_word}"

    # Format authors as "Last, First and Last, First"
    formatted_authors = []
    for name in authors:
        parts = name.split()
        if len(parts) >= 2:
            formatted_authors.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
        else:
            formatted_authors.append(name)
    author_str = " and ".join(formatted_authors)

    lines = [
        f"@article{{{key},",
        f"  title     = {{{title}}},",
        f"  author    = {{{author_str}}},",
        f"  year      = {{{year}}},",
        f"  eprint    = {{{arxiv_id}}},",
        f"  archiveprefix = {{arXiv}},",
    ]
    if category:
        lines.append(f"  primaryclass  = {{{category}}},")
    lines.append("}")

    return "\n".join(lines)


def fetch_doi(doi: str) -> str:
    """Fetch metadata from DOI.org and return a bib entry."""
    url = f"https://doi.org/{doi}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.citationstyles.csl+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    title = data.get("title", "")
    if isinstance(title, list):
        title = title[0]
    if not title:
        raise ValueError(f"DOI entry for {doi} has no title")

    authors = []
    for author in data.get("author", []):
        given = author.get("given", "")
        family = author.get("family", "")
        if family:
            authors.append(f"{family}, {given}" if given else family)
    if not authors:
        raise ValueError(f"DOI entry for {doi} has no authors")

    # Extract year
    date_parts = data.get("issued", {}).get("date-parts", [[None]])
    year = str(date_parts[0][0]) if date_parts[0][0] else ""
    if not year:
        raise ValueError(f"DOI entry for {doi} has no publication year")

    # Container (journal)
    journal = data.get("container-title", "")
    if isinstance(journal, list):
        journal = journal[0] if journal else ""

    # Entry type
    csl_type = data.get("type", "article-journal")
    if csl_type in ("paper-conference",):
        bib_type = "inproceedings"
    else:
        bib_type = "article"

    # Citation key
    first_last = authors[0].split(",")[0].lower() if authors else "unknown"
    first_last = re.sub(r"[^a-z]", "", first_last)
    first_word = re.sub(r"[^a-z]", "", title.split()[0].lower()) if title else "untitled"
    key = f"{first_last}{year}{first_word}"

    author_str = " and ".join(authors)

    lines = [f"@{bib_type}{{{key},"]
    lines.append(f"  title     = {{{title}}},")
    lines.append(f"  author    = {{{author_str}}},")
    lines.append(f"  year      = {{{year}}},")
    if journal:
        if bib_type == "inproceedings":
            lines.append(f"  booktitle = {{{journal}}},")
        else:
            lines.append(f"  journal   = {{{journal}}},")
    lines.append(f"  doi       = {{{doi}}},")
    lines.append("}")

    return "\n".join(lines)


def load_cache(cache_path: Path) -> dict[str, str]:
    """Load cached bib entries keyed by source identifier."""
    if not cache_path.exists():
        return {}
    cache = {}
    current_key = None
    current_lines = []
    for line in cache_path.read_text().splitlines():
        if line.startswith("%%% "):
            if current_key and current_lines:
                cache[current_key] = "\n".join(current_lines).strip()
            current_key = line[4:]
            current_lines = []
        else:
            current_lines.append(line)
    if current_key and current_lines:
        cache[current_key] = "\n".join(current_lines).strip()
    return cache


def save_cache(cache_path: Path, cache: dict[str, str]) -> None:
    """Save cached bib entries."""
    lines = []
    for key, entry in cache.items():
        lines.append(f"%%% {key}")
        lines.append(entry)
    cache_path.write_text("\n".join(lines) + "\n")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: generate_bib.py <sources.txt> [output.bib]")
        return 1

    sources_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    cache_path = sources_path.parent / ".bib_cache"
    cache = load_cache(cache_path)

    entries = []
    errors = []
    seen_keys: dict[str, str] = {}  # citation key -> source line
    key_pattern = re.compile(r"^@\w+\{(.+),$", re.MULTILINE)

    def add_entry(entry: str, source: str) -> None:
        match = key_pattern.search(entry)
        if not match:
            errors.append(f"No citation key found in entry from '{source}'")
            return
        key = match.group(1)
        if key in seen_keys:
            errors.append(
                f"Duplicate citation key '{key}' from '{source}' "
                f"(already used by '{seen_keys[key]}')"
            )
            return
        seen_keys[key] = source
        entries.append(entry)

    for line in sources_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Use cache for arxiv/doi entries
        if line in cache and not line.startswith("file:"):
            print(f"Cached: {line}")
            add_entry(cache[line], line)
            continue

        for attempt in range(MAX_RETRIES):
            try:
                if line.startswith("arxiv:"):
                    arxiv_id = line.removeprefix("arxiv:")
                    print(f"Fetching arXiv:{arxiv_id}...")
                    entry = fetch_arxiv(arxiv_id)
                    add_entry(entry, line)
                    cache[line] = entry
                elif line.startswith("doi:"):
                    doi = line.removeprefix("doi:")
                    print(f"Fetching DOI:{doi}...")
                    entry = fetch_doi(doi)
                    add_entry(entry, line)
                    cache[line] = entry
                elif line.startswith("file:"):
                    file_ref = line.removeprefix("file:")
                    bib_path = sources_path.parent / file_ref
                    print(f"Including {bib_path}...")
                    content = bib_path.read_text().strip()
                    if not content:
                        errors.append(f"Empty bib file: {bib_path}")
                    else:
                        add_entry(content, line)
                else:
                    errors.append(f"Unknown source format: {line}")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < MAX_RETRIES - 1:
                    wait = RETRY_DELAY * (attempt + 1)
                    print(f"  Rate limited, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                errors.append(f"Failed to fetch {line}: {e}")
                break
            except Exception as e:
                errors.append(f"Failed to fetch {line}: {e}")
                break

    # Always save cache, even on partial failure
    save_cache(cache_path, cache)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    bib_content = "\n\n".join(entries) + "\n"

    if output_path:
        output_path.write_text(bib_content)
        print(f"Wrote {len(entries)} entries to {output_path}")
    else:
        print(bib_content)

    return 0


if __name__ == "__main__":
    sys.exit(main())
