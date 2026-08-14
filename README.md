# Elite Diving Judge-Level Results (2000–2026)

Processed, analysis-ready CSVs of individual diving at senior international meets. Each modern row is one dive by one athlete, with all seven panel scores on the same line. Synchronized and team events are not included.

## Files

| File | Rows × cols | Unit | Source |
| --- | --- | --- | --- |
| `scores.csv` | 69,687 × 21 | One dive | Parsed Omega Timing results |
| `judge_scores.csv` | 69,687 × 29 | Same dives, plus `JudgeName1`–`7` and `Panel` | `scores` joined to the panel |
| `judges.csv` | 5,460 × 10 | One panel seat (J1–J7) per event/round/panel | Omega “Panel of Judges” |
| `judge_country_lookup.csv` | 126 × 2 | One cleaned judge name → ISO country | Manual lookup |
| `Diving2000.csv` | 10,787 × 10 | One judge’s score on one dive (long form) | Emerson et al. (2009), Sydney Olympics |

## Coverage (modern Omega files)

- **87 meets**, 2000–2026 (World Championships, World Cups, World Series, European Championships, Champions Cups, and selected multi-sport Games)
- **~3,900 divers** from **110 countries**
- Rounds: Preliminary, Semifinal, Final
- Events: individual 1m / 3m springboard and 10m platform only

## Key columns (`scores.csv` / `judge_scores.csv`)

`Meet`, `MeetId`, `Event`, `Round`, `EventDate`, `Diver`, `Country`, `OverallRank`, `DiveNo`, `DiveCode`, `Difficulty`, `JScore1`–`JScore7`, `PenaltyFlag`, `DivePoints`, `TotalPoints`.  
`judge_scores.csv` also has `JudgeName1`–`JudgeName7` and `Panel`.

`Diving2000.csv` is already long-form (`JScore`, `Judge`, `JCountry`) and covers four Sydney 2000 events (men’s/women’s 3m and 10m).

## Provenance

Modern tables come from official Omega Timing results PDFs (World Aquatics / European Aquatics).  
`Diving2000.csv` is the companion data to Emerson, Seltzer, and Lin (2009), *Assessing Judging Bias: An Example from the 2000 Olympic Games*, *The American Statistician*.

## Known gaps

- `EventDate` is missing on some older meets.
- Judge names are not always recoverable from the PDF layout (`JudgeName1` is missing on about half of modern dives).
- Omega PDFs do not include judge nationality; use `judge_country_lookup.csv` (126 names, 43 countries) or `Diving2000.csv` (`JCountry` is complete).


# Parser

Parse Omega Timing diving results books into CSVs

## Setup

```bash
conda env create -f environment.yml
conda activate datasci112
```

## Add a meet

Edit `config/meets.yaml`. Minimum fields:

```yaml
- id: some_meet_id
  name: Display Name
  event_page: https://www.omegatiming.com/...-live-results
```

`pdf_url` is optional — discovered automatically if missing.

## Download PDFs

```bash
python -m src --discover-pdfs          # find/save pdf_url for each meet
python -m src --download-pdfs          # cache PDFs under data/raw/pdfs/
python -m src --meet antalya_2025 --download-pdfs
python -m src --download-pdfs --force-download   # re-download
```

PDFs are fetched from live Omega via Google Chrome (first page load can take ~30s; 4s pause between files), then Wayback Machine if Chrome cannot get them. Use `--skip-omega` for Wayback only.

Requires Playwright (`pip install playwright`) and Google Chrome.

## Parse

```bash
python -m src                          # all meets in meets.yaml
python -m src --meet singapore_2025    # one meet
```

Writes / upserts (by `MeetId`) into:

- `data/processed/scores.csv`
- `data/processed/judges.csv`
- `data/processed/judge_scores.csv`

Individual events only (1m / 3m / 10m). Synchro and team events are skipped.
