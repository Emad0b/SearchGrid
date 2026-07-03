# SearchGrid — Local Living & Commute Dashboard

An interactive Streamlit dashboard that helps a CS/AI graduate compare living options in **London and nearby commuter towns** (Ilford, Watford, Slough, Luton). It combines live public web search, listing pages, commute APIs, and optional LLM extraction to rank accommodation by affordability, commute, career opportunity, and data confidence.

> Research tool only — always verify listings, bills, tenancy terms, fares, and salaries at the original source before making decisions.

## Features

- **Accommodation search** — rooms, studios, and small flats under a configurable rent cap, sourced from public pages on SpareRoom, Rightmove, OpenRent, Zoopla, and general web search. Rent is capped separately from utilities.
- **Commute to London tech hubs** — public transport and **bicycle** estimates to Shoreditch/Old Street, King's Cross, Liverpool Street, Canary Wharf, and more, using TfL Journey Planner and OpenRouteService with distance-based fallbacks.
- **Career signals** — entry-level CS/AI salary ranges, demand scores, and live job listings per city, including **AWS Solution Architect** roles.
- **Monthly Budget page** — build a custom budget (rent, utilities, groceries, subscriptions, commute, custom categories) that syncs into the research inputs.
- **Weighted ranking** — adjustable weights for affordability, budget fit, commute, cycling, career, and source confidence.
- **Accuracy scoring** — results are tiered by verification level; pasting individual listing URLs enables a higher-accuracy verification path.
- **Map view** — pins for the best option in each home location.
- **Exports** — download any table as CSV, the full dataset as JSON, or everything as a ZIP bundle.

## Screenshots

_Add screenshots of the Decision Snapshot, Ranked Grid, map, and Monthly Budget page here._

## Quick Start

Requires Python 3.11+.

```powershell
git clone https://github.com/<your-username>/SearchGrid.git
cd SearchGrid

python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows
# source .venv/bin/activate         # macOS/Linux

pip install -r requirements.txt
Copy-Item .env.example .env         # cp .env.example .env on macOS/Linux

streamlit run app.py
```

The app opens at `http://localhost:8501`. Set your inputs in the sidebar and click **Run live search**. Use the **Monthly Budget** page in the sidebar to build a personal budget first if you like.

## Configuration

All configuration lives in `.env` (never committed — see `.env.example` for the template). **Everything is optional**; the app degrades gracefully to heuristics without any keys.

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Enables LLM extraction of rents, salaries, and costs from messy web snippets (any OpenAI-compatible endpoint) |
| `OPENAI_BASE_URL` | Override the OpenAI-compatible endpoint (default `https://api.openai.com/v1`) |
| `OPENAI_MODEL` | Model used for extraction (default `gpt-4o-mini`) |
| `ORS_API_KEY` | [OpenRouteService](https://openrouteservice.org/) key for real cycling routes |
| `TFL_APP_ID`, `TFL_APP_KEY` | [TfL API](https://api.tfl.gov.uk/) credentials for better journey-planning rate limits |
| `CACHE_TTL_HOURS` | Web/LLM cache lifetime (default 24) |
| `REQUEST_TIMEOUT` | HTTP timeout in seconds (default 20) |

## Project Structure

```
app.py                     # Main dashboard (inputs, research pipeline, results)
pages/
  1_Monthly_Budget.py      # Separate budget-builder page
src/
  accommodation.py         # Listing search, extraction, verification tiers
  budget.py                # Monthly budget state, editor, and exports
  cache.py                 # JSON file cache for web/LLM results
  commute.py               # TfL, OpenRouteService, tech-hub commute estimates
  config.py                # Settings and candidate city loading
  costs.py                 # City living-cost estimates
  jobs.py                  # Salary, demand, and job-listing signals
  llm.py                   # Optional OpenAI-compatible extraction
  models.py                # Dataclasses for listings, commutes, careers, rankings
  ranking.py               # Weighted scoring model
  search.py                # Web search (ddgs) and page fetching
  ui.py                    # Shared styles
data/
  candidate_cities.yaml    # Cities with coordinates and notes
```

## How Ranking Works

Each listing is scored 0–100 from weighted components:

- **Affordability** — estimated leftover after rent, utilities, commute, groceries, and other expenses vs. estimated take-home salary
- **Budget fit** — closeness of rent to your target range
- **Commute** — public transport duration and monthly cost to the nearest selected tech hub
- **Cycling** — bike commute practicality
- **Career** — entry-level salary and job-demand signals
- **Confidence** — source quality; individual verified listing URLs rank highest, search pages lower

## Privacy & Security Notes

- API keys are read from `.env`, which is excluded by `.gitignore`. Never commit `.env`.
- The local `.cache/` directory (raw web search and LLM responses) is also excluded.
- Personal defaults (cash on hand, budgets) are entered in the UI at runtime and are not persisted to the repo.

## License

MIT — see [LICENSE](LICENSE) (add one before publishing if missing).
