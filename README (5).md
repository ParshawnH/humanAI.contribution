# FOA Ingestion Pipeline

Screening task submission — a small pipeline that pulls a Funding Opportunity Announcement from Grants.gov or NSF, extracts the key fields, tags it with research topics, and writes out `foa.json` + `foa.csv`.

## Setup

```bash
pip install -r requirements.txt
```

Just `requests` and `beautifulsoup4`. Nothing exotic.

## Running it

```bash
# Grants.gov
python main.py --url "https://www.grants.gov/search-results-detail/349644" --out_dir ./out

# NSF
python main.py --url "https://www.nsf.gov/awardsearch/showAward?AWD_ID=2401234" --out_dir ./out
```

It'll print what it finds and write two files to `--out_dir`:

- **foa.json** — everything including tag arrays
- **foa.csv** — same data, flat, tags joined with semicolons

## How it works

1. **Parse the URL** to figure out if it's Grants.gov or NSF, and extract the opportunity/award ID.

2. **Hit the API.** Both Grants.gov (`fetchOpportunity`) and NSF (Research.gov Awards API) have free, no-auth REST endpoints. If the API is down, it falls back to scraping the HTML page (less structured, but better than crashing).

3. **Normalize** into a common schema — same fields regardless of source:

   | Field | Example |
   |---|---|
   | foa_id | `HHS-2024-ACF-OCC-TP-0068` or `NSF-2401234` |
   | title | Full opportunity title |
   | agency | `Health & Human Services`, `NSF`, etc. |
   | open_date | `2024-03-15` (ISO format) |
   | close_date | `2024-06-01` |
   | eligibility | Who can apply |
   | description | Program description / abstract |
   | award_range | `$500,000 – $2,000,000` |
   | source_url | The original URL |
   | source | `grants.gov` or `nsf` |

4. **Tag it** using a keyword-based ontology with four categories: research domains, methods/approaches, populations, and sponsor themes. It's just substring matching — nothing fancy, but it's deterministic and easy to extend. The ontology lives in `ONTOLOGY` dict in `main.py`.

5. **Export** to JSON and CSV.

## Tagging approach

The tagger concatenates the title, description, and eligibility text, lowercases it, and checks for keyword matches. One match is enough to assign a tag. I used partial stems where it helps (e.g. `"epidemiolog"` catches both "epidemiology" and "epidemiological").

This is obviously limited — a production system would want embeddings or at least proper NLP. But for a deterministic baseline it works reasonably well and is trivial to audit.

## Files

```
main.py            # everything lives here
requirements.txt   # requests, beautifulsoup4
README.md          # you're reading it
out/
  foa.json         # sample output
  foa.csv          # sample output
```

## Notes

- Dates from Grants.gov are surprisingly inconsistent (sometimes "Oct 11, 2023 12:00:00 AM EDT", sometimes other formats). The parser tries several patterns and falls back to returning the raw string.
- The NSF Awards API returns funded awards, not open solicitations. That's a known limitation — for actual open NSF funding opportunities you'd want to scrape their solicitations pages, which is a bigger lift.
- Python 3.8+ required.
