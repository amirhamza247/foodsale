# Food Sale · FastAPI

A single-file Python web app for Halmstad grocery offers. All Python, HTML, CSS, and JavaScript are in `app.py`; there is no Streamlit dependency, frontend build, browser automation, or external UI library.

## Run locally

Requires Python 3.11 or newer. From the repository folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

The app opens `http://127.0.0.1:8000` after the server responds. Keep Terminal running; stop with Ctrl+C. To start without opening a browser, use `python app.py --no-browser`. For an occupied port, choose another, for example `python app.py --port 8001`. On Windows activate the environment with `.venv\Scripts\activate`.

Alternatively:

```sh
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

## Interface

- Native browser controls update the offer grid without reloading the page.
- Light/dark appearance follows the system initially and persists in local storage and the URL. Store selection also persists in the URL.
- Search handles accents and word order, and includes grouped Coop variety names.
- Membership and expiry filters; source, name, expiry, percentage reduction, and saving-per-priced-unit sorts.
- First 48 cards load initially; show more or all matches. Search always checks the complete retrieved dataset.
- Images load lazily. Prices, multi-buy wording, membership, dates, limits, and grouped varieties are preserved. Coop tiered prices and “buy X, pay Y” offers are supported.
- Retrieval time, loading/error/empty states, stale-data warnings, and the official source link remain visible where appropriate.

## Store support

19 selectable stores; **12 integrated product views and seven official-page links only**. Changing framework does not resolve the seven missing data sources.

| Stores | Support |
|---|---|
| Willys Öster and Flygstaden | Product cards, search, filters, comparable-price savings |
| Hemköp Halmstad C | Product cards, search, filters, Klubb Hemköp labels and comparable-price savings |
| Stora Coop, Gamletull, Flygaregatan, Furuvägen, Fyllinge, X:-tra Snöstorpsvägen | Product cards, search and filters. Source supplies no ordinary prices, so calculated savings remain unknown. |
| Lidl Torpaledsvägen, Gesällgatan, Tegelbruket | Shared published campaigns; prices must agree across all three Halmstad regions. Branch identity is checked. Reference-price savings appear only when supported by the data. |
| Maxi ICA Högskolan, Maxi ICA Flygstaden, ICA Supermarket Vallås, ICA Supermarket Söndrum, ICA Nära Frösakull | Official-page links; direct access was refused in earlier checks. |
| Tempo Halmstad Bäckagård | Official-page link; publication provider refused automated extraction. |
| City Gross Halmstad | Official store/leaflet link; a complete product feed remains unresolved. |

No personal coupons, account logins, purchases, or local stock guarantees. Missing or incompatible comparison prices are never invented. “Biggest price drop” compares the source's displayed ordinary/reference price with the offer, rather than tracking historical price changes.

## Caching and responsiveness

The page shell and health endpoint do not wait for grocery websites. Synchronous extraction runs on worker threads rather than the asynchronous request loop. Three refresh workers bound concurrent source work, and simultaneous visitors share a single extraction per store/date. Lidl's three branches share one regional offers cache.

Public offers are fresh for five minutes. Recent snapshots can be displayed for up to five additional minutes during background refresh, with a warning. Failed refreshes do not extend the snapshot age; over-age prices are refused. Expired offers are filtered again for every response. Metadata is cached separately for 24 hours with bounded entries and single-flight loading. There are no nested five-minute offers caches.

Manual refresh is coalesced and limited to one check per 30 seconds per cached source. Failures have a cooldown; source Retry-After is honored. HTTP reads have finite timeouts, bounded response sizes, and at most one retry for selected transient server errors. Access refusals are not retried automatically. Lidl HTML is processed in bounded batches to reduce peak memory.

Caches are process-local and disappear on restart or hosting sleep. **Run one worker** for this small app; additional workers have separate caches and can duplicate extraction. The app does not create a database or external cache service.

## Render

This version needs a Python Web Service; Streamlit Community Cloud cannot run it as a Streamlit app. Render configuration, following its [official FastAPI guide](https://render.com/docs/deploy-fastapi):

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check path: `/healthz`

Use the repository folder containing `app.py` as the service root. No browser binaries or secrets are required. The direct `python app.py --no-browser` launcher also honors the host's `PORT` environment variable.

**Not deployed or verified from Render.** After deployment, check every integrated chain from the actual host before sharing. Local success does not establish cloud access. Hosting cold starts, source latency, and image downloads can still cause delays.

## Verification

Automated API checks cover all 19 store states, 12 product views using recorded fixtures, full-dataset search, membership, pagination, input validation, escaped source content, refresh-origin checks, stale warnings, and graceful source errors. Cache checks cover simultaneous cold reads, one background refresh, replacement, failure, branch isolation, and the hard age cap. Pricing regression checks cover multi-buy totals, incompatible units, percentage/saving sorts, and the newly encountered Coop deal formats.

JavaScript interactions were tested with an emulated DOM against a real local HTTP API: initial/all cards, empty search, reset, store/member changes, persistent theme, official-page fallback, and manual refresh. This was not visual browser testing; the user handles visual checks.

Final local live checks passed for all 12 integrated branches. There were 12 successful extraction attempts out of 13 including the initial Stora Coop failure that prompted the format fix. Peak macOS process RSS during the extraction run was 95.3 MiB. One real HTTP smoke test measured the shell at 0.93 ms, cold Willys offers at 0.970 seconds, and a warm filtered response at 4.91 ms. These are local observations, not end-to-end page-load or hosted performance guarantees. Detailed benchmark evidence is supplied with the migration report.
