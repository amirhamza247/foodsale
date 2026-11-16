# Food Sale · FastAPI

A single-file Python web app for Halmstad grocery offers. All application Python, HTML, CSS, and JavaScript are in `app.py`; there is no Streamlit dependency, frontend build, or external UI library. ICA extraction uses an on-demand Playwright Chromium browser; all other existing product sources use HTTP.

## Run locally

Requires Python 3.11 or newer. From the repository folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install --only-shell chromium
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

19 selectable stores; **17 product integrations**, plus City Gross leaflet links and a Tempo official-page link. ICA falls back to the official page if browser access or extraction fails.

| Stores | Support |
|---|---|
| Willys Öster and Flygstaden | Product cards, search, filters, comparable-price savings |
| Hemköp Halmstad C | Product cards, search, filters, Klubb Hemköp labels and comparable-price savings |
| Stora Coop, Gamletull, Flygaregatan, Furuvägen, Fyllinge, X:-tra Snöstorpsvägen | Product cards, search and filters. Source supplies no ordinary prices, so calculated savings remain unknown. |
| Lidl Torpaledsvägen, Gesällgatan, Tegelbruket | Shared published campaigns; prices must agree across all three Halmstad regions. Branch identity is checked. Reference-price savings appear only when supported by the data. |
| Maxi ICA Högskolan, Maxi ICA Flygstaden, ICA Supermarket Vallås, ICA Supermarket Söndrum, ICA Nära Frösakull | Playwright product cards, search, filters and safe discount calculations; verified locally for all five branches. Source supplies expiry dates but no start dates. |
| Tempo Halmstad Bäckagård | Official-page link; publication provider refused automated extraction. |
| City Gross Halmstad | Official store/leaflet link; a complete product feed remains unresolved. |

No personal coupons, account logins, purchases, or local stock guarantees. Missing or incompatible comparison prices are never invented. “Biggest price drop” compares the source's displayed ordinary/reference price with the offer, rather than tracking historical price changes.

## Caching and responsiveness

The page shell and health endpoint do not wait for grocery websites. Synchronous extraction runs on worker threads rather than the asynchronous request loop. Three refresh workers serve HTTP sources. ICA has a separate single-worker queue/cache so browser work cannot occupy those workers; at most one Chromium instance runs at a time. Simultaneous visitors share a single extraction per store/date. Lidl's three branches share one regional offers cache.

Public offers are fresh for five minutes. Recent snapshots can be displayed for up to five additional minutes during background refresh, with a warning. Failed refreshes do not extend the snapshot age; over-age prices are refused. Expired offers are filtered again for every response. Metadata is cached separately for 24 hours with bounded entries and single-flight loading. There are no nested five-minute offers caches.

Manual refresh is coalesced and limited to one check per 30 seconds per cached source. Failures have a cooldown; source Retry-After is honored. HTTP reads have finite timeouts, bounded response sizes, and at most one retry for selected transient server errors. Access refusals are not retried automatically. Lidl HTML is processed in bounded batches to reduce peak memory.

Caches are process-local and disappear on restart or hosting sleep. **Run one worker** for this small app; additional workers have separate caches and can duplicate extraction. The app does not create a database or external cache service.

## Render

This version needs a Python Web Service; Streamlit Community Cloud cannot run it as a Streamlit app. Render configuration, following its [official FastAPI guide](https://render.com/docs/deploy-fastapi):

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check path: `/healthz`

Use the repository folder containing `app.py` as the service root. The Python-only commands above do **not** install the Linux browser/system libraries required for ICA. For ICA use the included **Dockerfile**: choose a Docker Web Service on Render with this repo folder as its root and leave the Docker Command empty. The image installs pinned Playwright and its matching Chromium headless shell plus Linux libraries. Its default command runs `python app.py --no-browser`, honoring Render's `PORT` and binding to `0.0.0.0`. Keep a single instance/worker to share the cache and browser lock. No secrets are required.

If the existing Render service uses the Python runtime, deploy this version as a Docker service (or use an equivalently configured browser-capable runtime); pushing Python code alone cannot install missing OS packages. Browser binaries are installed at build time, never on startup. The image runs as a non-root user. Render's free plan has 512 MB RAM; actual service memory and ICA access must be tested there before relying on it.

**Not deployed or verified from Render.** After deployment, check every integrated chain from the actual host before sharing. Local success does not establish cloud access. Hosting cold starts, source latency, and image downloads can still cause delays.

## Verification

Automated API checks cover all 19 store states, 17 product views using recorded fixtures, full-dataset search, membership, pagination, input validation, escaped source content, refresh-origin checks, stale warnings, and graceful source errors. Cache checks cover simultaneous cold reads, one background refresh, replacement, failure, branch isolation, and the hard age cap. Pricing regression checks cover multi-buy totals, incompatible units, percentage/saving sorts, and the newly encountered Coop deal formats.

JavaScript interactions were tested with an emulated DOM against a real local HTTP API: initial/all cards, empty search, reset, store/member changes, persistent theme, official-page fallback, and manual refresh. This was not visual browser testing; the user handles visual checks.

Final local live checks passed for all 12 integrated branches. There were 12 successful extraction attempts out of 13 including the initial Stora Coop failure that prompted the format fix. Peak macOS process RSS during the extraction run was 95.3 MiB. One real HTTP smoke test measured the shell at 0.93 ms, cold Willys offers at 0.970 seconds, and a warm filtered response at 4.91 ms. These are local observations, not end-to-end page-load or hosted performance guarantees. Detailed benchmark evidence is supplied with the migration report.


## City Gross leaflet support and ICA access

Selecting City Gross now loads its current official Halmstad leaflet links and the source's validity wording directly in the app. Links are checked against store 3210 and an HTTPS publication URL allowlist, with a five-minute cache, bounded background refresh and a ten-minute maximum age. Expired publication links are excluded. A feed failure keeps the official store link available and shows a clear message.

This is **leaflet-link support, not a product integration**: City Gross still has no product search, membership filtering or discount sorting here. Its public product feed returned zero results, and the linked publication reader closed the extraction connection. No PDF/OCR or browser dependency was added without a working source to validate.

ICA's plain HTTP requests returned 403, but ordinary Playwright Chromium successfully reads all five offer pages. The adapter checks robots.txt, the exact branch/account identifiers, page totals, unique offer IDs and eligibility. It preserves Stammis membership, restrictions, multi-buy wording, variants and expiry dates. Start dates are absent, so ICA cards say “Gäller till”; undated-start offers are only eligible on their observed date. No login, personal coupons, stealth configuration, proxies or challenge solving are used. Tempo remains unchanged.

Each ICA selector option retains “Officiell sida” until its product cards actually render for that visitor. Failed extraction shows the official link and restores the label. Successful card rendering removes it; other stores' fallback labels are retained.

Focused automated tests cover leaflet branch/date/URL/schema validation, duplicate removal, warm-cache reuse and sanitized errors. Existing API/cache and DOM checks remain applicable. A local single-sample live check found the week 47 Halmstad leaflet (16–22 November 2026): 0.109 seconds cold, 0.012 milliseconds warm, 51.2 MiB peak process RSS. These measure leaflet metadata only; product extraction and Render source access remain unverified.


## ICA integration checks

Five local live API extractions succeeded with 261 Högskolan, 301 Flygstaden, 80 Vallås, 51 Söndrum and 54 Frösakull offers on 16 November 2026. Cold API requests took 0.48–1.17 seconds, warm membership/sort filters 2.2–4.8 milliseconds. These include card generation, not image download or browser rendering.

Automated fixture checks cover branch identity, complete counts, expiry and observation dates, membership, malformed data, variants, multi-buy and percentage pricing, incompatible units/ranges, full search, pagination and sorting. JavaScript tests cover label removal only after cards render, failure fallback, recovery, theme and switching stores. Visual browser checks are left to the user.

The local sampled peak sum of the API process plus Chromium descendants was 460.6 MiB, including shared pages potentially counted multiple times. This is not a Render container-memory measurement. Linux container checks and actual Render checks must be distinguished in any deployment report.


The Docker image was built locally. In a Linux arm64 container limited to **512 MiB RAM, no swap and 0.1 CPU**, all five ICA APIs succeeded: cold requests took **38–55 seconds**, cached requests 0.1–0.5 seconds, and sampled peak cgroup memory was about **222 MiB** with no OOM. With page JavaScript disabled, two representative checks took 39–49 seconds and peaked at about 216 MiB. These small samples are not a controlled speedup claim; CPU limits make cold browser work slow even when memory fits. The final independent ICA queue is covered by a separate automated concurrency test. Actual Render source access and architecture/performance remain unverified.

Your supplied [live Render URL](https://foodsale-mycity.onrender.com/) timed out on both health and ICA checks during this session. No deployment, commit or push was performed. After deploying the Docker configuration, check each ICA store on that URL before treating hosted integration as complete.


A final queue-enabled Linux smoke test with 512 MiB and 0.5 CPU returned 261 Högskolan offers in 2.29 seconds, with a 7 ms warm response and about 219 MiB peak cgroup memory. This is one local container sample, not a Render guarantee. Your existing local `.venv` now has the required Playwright package and its standard Chromium shell installed; restart the app to use them.
