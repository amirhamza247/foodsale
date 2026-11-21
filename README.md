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

19 selectable stores; **18 product integrations**, plus a Tempo official-page link. ICA falls back to the official page if browser access or extraction fails.

| Stores | Support |
|---|---|
| Willys Öster and Flygstaden | Product cards, search, filters, comparable-price savings |
| Hemköp Halmstad C | Product cards, search, filters, Klubb Hemköp labels and comparable-price savings |
| Stora Coop, Gamletull, Flygaregatan, Furuvägen, Fyllinge, X:-tra Snöstorpsvägen | Product cards, search and filters. Source supplies no ordinary prices, so calculated savings remain unknown. |
| Lidl Torpaledsvägen, Gesällgatan, Tegelbruket | Shared published campaigns; prices must agree across all three Halmstad regions. Branch identity is checked. Reference-price savings appear only when supported by the data. |
| Maxi ICA Högskolan, Maxi ICA Flygstaden, ICA Supermarket Vallås, ICA Supermarket Söndrum, ICA Nära Frösakull | Playwright product cards, search, filters and safe discount calculations; verified locally for all five branches. Source supplies expiry dates but no start dates. |
| Tempo Halmstad Bäckagård | Official-page link; publication provider refused automated extraction. |
| City Gross Halmstad | Store-specific website offers, PRIO labels, dates, bundle/kg prices, pant, limits and safe savings. Public product feed discovered through Playwright; fetched directly with HTTP. |

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

Automated API checks cover all 19 store states, 18 product views using recorded fixtures, full-dataset search, membership, pagination, input validation, escaped source content, refresh-origin checks, stale warnings, and graceful source errors. Cache checks cover simultaneous cold reads, one background refresh, replacement, failure, branch isolation, and the hard age cap. Pricing regression checks cover multi-buy totals, incompatible units, percentage/saving sorts, and the newly encountered Coop deal formats.

JavaScript interactions were tested with an emulated DOM against a real local HTTP API: initial/all cards, empty search, reset, store/member changes, persistent theme, official-page fallback, and manual refresh. This was not visual browser testing; the user handles visual checks.

Final local live checks passed for all 12 integrated branches. There were 12 successful extraction attempts out of 13 including the initial Stora Coop failure that prompted the format fix. Peak macOS process RSS during the extraction run was 95.3 MiB. One real HTTP smoke test measured the shell at 0.93 ms, cold Willys offers at 0.970 seconds, and a warm filtered response at 4.91 ms. These are local observations, not end-to-end page-load or hosted performance guarantees. Detailed benchmark evidence is supplied with the migration report.


## City Gross, ICA and Tempo sources

City Gross now shows the Halmstad weekly product view, not leaflet links. Playwright verified ordinary store selection and revealed the public product endpoint used by its site. Lightweight HTTP can read it directly with `store=3210` and `currentWeekDiscountOnly=true`, so City Gross does not need a Chromium instance for each refresh. Its robots rules, Halmstad store mapping and weekly category are checked and cached. A large, explicitly sorted page avoids the unstable relevance pagination observed during discovery; if further pages are needed, totals, page numbers and unique IDs must agree or extraction fails.

The source's active promotion determines the displayed deal, preserving PRIO membership, dates, multi-buy, kg prices, pant, receipt limits and comparison prices. Other longer-running promotions are not substituted for the site's active deal. These are **City Gross website prices for Halmstad**; the app does not claim to verify physical-store stock or separate leaflet-only offers. The earlier leaflet API is retained for compatibility, but the normal City Gross product view does not fetch it. Source failure retains an official-page fallback.

ICA's plain HTTP requests returned 403, but ordinary Playwright Chromium successfully reads all five offer pages. The adapter checks robots.txt, exact branch/account identifiers, page totals, unique offer IDs and eligibility. It preserves Stammis membership, restrictions, multi-buy wording, variants and expiry dates. Start dates are absent, so ICA cards say “Gäller till”; undated-start offers are only eligible on their observed date. No login, personal coupons, stealth configuration, proxies or challenge solving are used.

ICA and City Gross selector labels retain “Officiell sida” until their product cards actually render for that visitor. Failed extraction shows the official link and restores the label. Successful recovery removes it again. Tempo remains an official link: its own public store page exposes a Tjek widget rather than product data, and the publication provider previously explicitly refused automated extraction. The browser check blocked that provider and did not try to bypass its restriction.

## ICA integration checks

Five local live API extractions succeeded with 261 Högskolan, 301 Flygstaden, 80 Vallås, 51 Söndrum and 54 Frösakull offers on 16 November 2026. Cold API requests took 0.48–1.17 seconds, warm membership/sort filters 2.2–4.8 milliseconds. These include card generation, not image download or browser rendering.

Automated fixture checks cover branch identity, complete counts, expiry and observation dates, membership, malformed data, variants, multi-buy and percentage pricing, incompatible units/ranges, full search, pagination and sorting. JavaScript tests cover label removal only after cards render, failure fallback, recovery, theme and switching stores. Visual browser checks are left to the user.

The local sampled peak sum of the API process plus Chromium descendants was 460.6 MiB, including shared pages potentially counted multiple times. This is not a Render container-memory measurement. Linux container checks and actual Render checks must be distinguished in any deployment report.


The Docker image was built locally. In a Linux arm64 container limited to **512 MiB RAM, no swap and 0.1 CPU**, all five ICA APIs succeeded: cold requests took **38–55 seconds**, cached requests 0.1–0.5 seconds, and sampled peak cgroup memory was about **222 MiB** with no OOM. With page JavaScript disabled, two representative checks took 39–49 seconds and peaked at about 216 MiB. These small samples are not a controlled speedup claim; CPU limits make cold browser work slow even when memory fits. The final independent ICA queue is covered by a separate automated concurrency test. Actual Render source access and architecture/performance remain unverified.

Your supplied [live Render URL](https://foodsale-mycity.onrender.com/) timed out on both health and ICA checks during this session. No deployment, commit or push was performed. After deploying the Docker configuration, check each ICA store on that URL before treating hosted integration as complete.


A final queue-enabled Linux smoke test with 512 MiB and 0.5 CPU returned 261 Högskolan offers in 2.29 seconds, with a 7 ms warm response and about 219 MiB peak cgroup memory. This is one local container sample, not a Render guarantee. Your existing local `.venv` now has the required Playwright package and its standard Chromium shell installed; restart the app to use them.


## Keyword search and typo tolerance

Search runs in memory over the selected store's complete current offers. It uses names, brands, sizes, descriptions and grouped varieties, preserving accent folding and word-order independence. Full name matches and name/brand keyword matches rank first in the default ordering; explicitly selected price/date/alphabetic sorts are retained.

If the current membership/expiry filters yield no exact keyword matches, RapidFuzz tries conservative word and compound-edge spelling corrections (one edit for 4–9 letters, up to two for longer words, with at least 80% similarity). Approximate cards say **Liknande träff**, and a message explains that there were no exact matches. Short words and dietary/negative terms are not fuzzy-corrected; candidate dietary words are protected too. Search is a text tool, not a verified dietary-suitability filter.

Numbers and known units stay literal. Measurements are bound as pairs: `450 g` cannot match `450 ml` plus an unrelated `20 g`. Unit aliases such as gram/grams/g and decimal comma/dot are normalized; different quantities or different units are not converted into each other. Long queries over eight tokens retain strict keyword behavior. Search does not infer recipes, synonyms or semantic intent.

A bounded cache holds up to 4,096 public product-text documents. It does not cache visitor queries, product eligibility or search responses. Text changes generate new entries; current dates, membership and expiry are filtered on every request. Search adds no upstream calls, embeddings, database or external service.

Automated tests cover typo fallback, exact ranking, protected words, number/unit pairs, source-text changes, expiry/membership, variants, explicit sorting, unchanged cached offers, full-result pagination and API labels. Local measurements on 301 recorded offers took about 0.2–7.2 ms median per query, an 8.8 ms cold text-index build, and about 0.37 MB additional Python traced allocations (measured separately from timings). These measure search selection only, not fetching, page rendering or Render performance. This change does not itself deploy the app; hosted verification is a separate step.


## City Gross verification

A real local API check retrieved all 252 Halmstad products, including 32 active PRIO deals and 38 offers ending on 18 November 2026. Cold retrieval plus metadata, normalization and 48-card generation took 1.89 seconds; serving all 252 cards from cache took 8.3 ms. Peak process RSS was about 70 MiB, without a browser process. This is one local sample, not a hosted performance guarantee.

Tests cover exact store/category and robots checks, complete paging, duplicate/missing/changed pages, invalid nested fields, dates, membership, multi-buy and kg calculations, mismatched units, deposits, limits, search, pagination and sorting. A raw ampersand at the end of a product name exposed an HTML text-parser buffering issue; closing the parser now preserves names such as “Dill&Gräslök”. Automated DOM tests confirm City Gross cards, PRIO filtering, source-failure recovery and conditional label removal. The agent did not commit, push or deploy these changes.


## Product and unit-price comparison

Click a product image or **Jämför priser** to flip that card. Grouped offers ask for an exact flavour/size first. Exact GTIN/shared-code matches are preferred. If the exact pack is unavailable, conservative full-text and spelling-tolerant matching can find the same brand, product type and flavour in other sizes. The flipped card shows a compact store list with one current comparison price per kilo, liter or styck. Pack prices, reference prices, discount percentages and difference percentages are removed from the visible comparison rows. Price details are available by activating the price itself with mouse, touch or keyboard. Different flavours, brands, dietary modifiers and unknown pack quantities are rejected.

Prices arrive progressively in the flipped card without replacing the grid. Comparison work starts only after opening a card, uses bounded workers and a five-minute in-memory result cache, and deduplicates repeated opens. Cached results disappear on restart or hosting sleep. Display-time age and expiry checks suppress out-of-date values. Escape/back restores focus; filtering closes comparisons without taking focus from controls. Reduced-motion preferences disable the flip animation.

Current offer feeds can supply verified matching sale prices. Regular catalogue prices are supported for City Gross, both Willys branches, Hemköp C and Stora Coop Halmstad, using anonymous verified store contexts. Other sources can have no verified regular price; **Pris saknas** means the price could not be verified, not that the store does not sell the item. Web prices, in-store offers, membership and multi-buy terms are labelled. A web reference price is not treated as a verified ordinary in-store price, and incompatible-scope percentage discounts are suppressed. Stock availability is not guaranteed.

One local live comparison of Svenskt Smör 500 g completed all 19 store statuses in 12.24 seconds and returned four verified prices: City Gross, Willys Öster, Willys Flygstaden and Hemköp C. Other rows were explicitly unavailable. This single sample is not a success-rate or Render performance guarantee. Automated checks cover exact identity, grouped variants, lazy start, cached reuse, age limits, API origin guards, progressive rows, scroll/focus preservation and existing search/filter behavior. Static design scanning had no findings. Visual browser checks are left to the user. No extra runtime packages were added.

Restart locally with `python app.py` after stopping the previous process in its Terminal with Ctrl+C. These comparison changes have not been deployed; actual Render source access, cold-start time and container memory remain unverified.


### Product-page identity and browser price fallback

Lidl cards now retain their official product path and ID. On the first comparison, the app reads that product's standard Product JSON-LD to recover a checksum-valid GTIN. Ordinary HTTP is preferred; Playwright is used if inline product metadata is missing. Metadata is bounded/cached for one day and only the opened product is resolved. The recovered identity can also match the same product across the three cached Halmstad Lidl regional offers. Text-based family matches are explicitly distinguished from exact products.

For ICA, a missing exact offer can now try a serialized anonymous Playwright lookup in the official branch-specific online shop. Only matching GTIN, SEK, explicit per-item price units and unambiguous, unexpired structured offers can supply a price. The browser shares the existing ICA concurrency lock; it does not launch once per store in parallel. Access refusals open a one-hour host-wide circuit breaker, with no stealth or challenge solving. No login, cart or purchase actions are involved. Other unsupported catalogues remain explicitly unavailable.

Live inspection on 17 November 2026 recovered GTIN 07340005407439 for Lidl's Chips Dill & Gräslök Maxipack 375 g. One HTTP metadata extraction succeeded in 0.62 seconds; the cached read took less than 0.01 ms. Sampled peak process-tree RSS was 70 MiB for that HTTP-only benchmark. Three branch comparison rows were checked against saved regional data, not three new live price requests. This does not establish prices at competing stores. Willys' sampled public search returned OLW dill chips in 275 g, which must not be substituted for this 375 g pack.

ICA's public storefront loaded through Playwright, but its product-search API returned 403 in the single live search attempt (zero successful price extractions from one attempt). Browser extraction memory was not measured, and the new fallback's successful price parsing is fixture-tested, not proven against a live ICA price. Actual Render behavior remains unverified. Focused tests cover recovered identity, HTTP-first/browser fallback, exact GTIN, units/currency/expiry, ambiguous prices, refusal cooldown and the Lidl comparison job. No new runtime dependency was added.


### Different-size comparison (17 November 2026)

The user now explicitly allows different pack sizes for the same brand/flavour/type. Unit prices use the actual amount and units, including multipacks and multibuy requirements. Mass/volume/piece dimensions are never mixed; ranges and unclear weights are not guessed. The selected store stays first; other verified comparable rows sort by unit price. The current compact interface does not display percentage differences or discount columns. Public web prices, membership and quantity requirements retain their labels.

Catalogue discovery uses public Axfood/City Gross product search and then validates the actual branch-scoped price. Shared Axfood product IDs may help discover Hemköp products through Willys search, but Hemköp prices must come from its own verified anonymous branch session. Search candidates are bounded (up to six matching catalogue products); an unavailable result is not proof of absence. Full-text matching preserves brand/flavour/type/numeric modifiers; spelling tolerance allows one edit in longer words without dropping product tokens. The user explicitly confirmed OLW for Lidl product 12178463, whose source omits brand; this mapping is retained in the normalizer.

Live checks found the 275 g OLW Dill & Gräslök pack at Willys Öster/Flygstaden for 25.90 kr (94.18 kr/kg), and Hemköp C for 33.07 kr (120.25 kr/kg). The selected Lidl 375 g offer was 29.90 kr (79.73 kr/kg). The Hemköp/City Gross lookup timing sample is recorded in the workspace's work/unit-comparison/live.json; City Gross returned no verified family candidate in that sample. These are individual observations, not exhaustive coverage or a lasting price guarantee. No additional browser process was needed for these successful HTTP lookups; a new full-process memory benchmark was not performed for this change.

Missing ICA exact offers can also attempt brand/flavour browser search for a different size, accepting only explicit structured public prices with validated units. ICA's previously observed product-search 403 remains unresolved; the host-wide refusal pause applies to both exact and family browser lookup. Playwright does not guarantee access or make all 19 store catalogues available. Coop relies on verifiable current offers; Tempo's refused source remains unsupported. Actual Render extraction is unverified, and no deployment was performed.

Automated checks cover unit conversion and multibuys, incompatible dimensions and uncertain weights, wrong-brand/flavour/type/dietary/numeric exclusions, grouped variant identity and selected-store baseline conversion, stale value removal, sorting, cached discovery and progressive UI unit/pack labels. Existing search/API/cache/theme checks pass. DOM interaction tests preserve focus, scroll, filters and independent flips. Visual browser checks were skipped per user preference. Static UI scanning returned no findings.


### ICA comparison source priority

Comparison now explicitly disables the separate ICA online-shop lookup during its initial exact-offer pass. It loads each branch through the existing working offer extractor/cache, searches exact identifiers and same-brand/flavour variants across pack sizes, and calculates comparable unit prices before trying the separate shop. A different-size grouped-variant regression verifies that a usable ICA offer never calls either online-shop price function. A second test verifies that the browser family search runs only after an offer miss. This change does not make the weekly-offer source a full ordinary-price catalogue.


### ICA/Coop comparison corrections

Source-enumerated family variants are retained independently of valid barcode availability. The matcher now inspects these even when the generic title matches but the group quantity is a range. Source-specific variant brand/amount is preferred over grouped metadata. Package parsing accepts Coop's trailing punctuation (`750 g.`, `1 liter.`, `15 st.`), while rejecting uncertain ranges. Manufacturer comparison ignores explicitly separated origin-country segments (`Sverige/Kronfågel`), but still rejects multiple competing brands (`OLW/Estrella`). Missing-price rows distinguish absent comparable offers from failed catalogue access rather than returning the earlier exact-match-only error.

On 17 November, fresh source extraction returned 262 ICA Högskolan offers and 55 Stora Coop offers. Neither listed OLW chips. Recorded fresh-feed matching checks verified five definite-size ICA offers and five Coop offers, including Coop Axa havregryn 750 g at 10 kr / 13.33 kr per kg. These tests validate available source prices, not complete regular-catalogue coverage. The user supplied the exact ICA product page for OLW 275 g, product1490341; a normal Playwright navigation to that page returned a CloudFront HTTP403 request-blocked response. ICA search returned the same refusal. No bypass was attempted, and that missing regular price remains unresolved. New matching tests plus existing ICA/API/Coop price regressions pass. Actual Render access remains unverified.


## Compact comparison and regular-price coverage

The comparison face now shows store name and a single Swedish unit quote (`kr/kilo`, `kr/liter`, `kr/styck`). Source metadata and package details are collapsed behind the price's native `details`/`summary` control, accessible with keyboard and touch. Only mandatory price terms such as membership, minimum quantity, coupons and deposits remain as short markers. Prices cached beyond five minutes have a visible **Sparat pris** marker; quotes older than ten minutes or past their source validity are suppressed. No raw pack price is used as a substitute for a missing unit quote.

ICA/Coop/City Gross feeds now preserve the source's explicit current comparison-price field. It can provide a verified unit quote when package quantity is missing or ambiguous. If a definite package calculation conflicts with the published unit amount or dimension, the quote is withheld. Regular City Gross/Axfood catalogue quotes likewise retain published comparison prices. The selected-store quote no longer fetches an unnecessary reference price when its current unit price is already known. Lidl comparisons can load the shared regional offer list on demand and resolve at most two relevant product-detail candidates; barcode or published brand must confirm the candidate.

### Stora Coop Halmstad regular catalogue

Coop's public website publishes an anonymous product API and pickup-location directory. The adapter discovers the Halmstad branch and requires exact ledger ID **105610**, name **Stora Coop Halmstad** and address **Prästvägen 1**. It sends that identifier to the official product API and validates product brand/flavour, barcode, online availability, quantity and published B2C unit price. Stockholm/default prices and Stora Coop prices are never assigned to other branches. Requests are bounded, five-minute cached and deduplicated; changed/duplicate/incomplete pages fail closed, and HTTP Retry-After opens a source-wide cooldown. Public client configuration is loaded at runtime; no personal accounts, login cookies or embedded secrets are used.

One live integrated HTTP extraction on **18 November 2026** returned two OLW dill-chip sizes for Stora Coop Halmstad, including 275 g at **26.50 kr / 96.36 kr per kilo**. The complete four-result source response took **0.74 seconds cold**, with a subsequent cache read below **0.01 ms**; sampled Python process-tree peak RSS was **56 MiB**. This is one successful extraction from one integrated attempt, not an uptime or deployment guarantee. Discovery browser probes were separate and are not included in that HTTP benchmark.

| Listed stores | Regular-price source status |
| --- | --- |
| Willys Öster and Flygstaden; Hemköp C; City Gross; Stora Coop Halmstad (5) | Verified anonymous store-scoped catalogue routes |
| Five ICA branches | Current offers work; regular-price product/search pages have returned 403 or human verification in standalone browsers. The successful Codex-browser session is not a verified app integration. |
| Five other Coop/X-tra branches | Current branch offers work; they were not returned as Halmstad pickup catalogues, so regular prices remain unverified. |
| Three Lidl branches | Published regional offers and detail metadata; no verified complete ordinary-price catalogue. |
| Tempo Bäckagård | Source provider refused automated extraction; official-page fallback remains. |

Full regular-price coverage for every item in all 19 stores is **not complete**. An unavailable lookup is never asserted to mean the item is absent from a physical shop. Source permission/access is still needed where a catalogue is blocked or not publicly published; no challenge solving or restrictions bypass was added. Actual Vercel/Render source access remains unverified.

Focused tests cover published comparison prices, ordinary catalogue quotes, matching/units, stale suppression, Coop branch/address isolation, pagination, failure cooldowns and cached reuse. DOM tests verify the compact unit-only face, on-demand details, independent flips and focus/scroll retention. Existing search, API, offer-pricing, cache, theme and exact-CSP checks pass. Static UI scanning returned no findings; code-only finish review approved its scored accessibility fix. Visual browser checks remain delegated to the user. No new runtime packages were required, and application source remains one `app.py`.

## Unit-price correctness (21 November 2026)

Every card shows the shop’s sale price first in large red text, then its Swedish comparison price (`kr/kilo`, `kr/liter`, `kr/styck`), ordinary price, ordinary comparison price and savings in kronor. Ordinary comparison prices are calculated separately without reusing the sale comparison price. Unavailable values are explicitly labelled. Front and comparison faces use the same server-side Decimal calculation and half-up rounding to öre. The browser displays the canonical value without recalculating it. Expand the price to inspect its basis and conditions.

Definite net quantities, explicit multiplication, item counts and multibuy totals can be calculated independently. Published comparison prices are retained and cross-checked where possible. Any disagreement in dimension or rounded cents suppresses the quote, including one-cent source discrepancies. Published quotes without independently usable quantities are labelled as source-published, not independently verified. Ambiguous count/yield notation such as `6p/3l` is never interpreted as six three-litre containers. Variable sizes can show an explicit range, never a single ranked price. Approximate weights and conflicting data remain unresolved. Explicit styck prices without net quantity mean **per purchase item**; those quotes cannot support fuzzy comparisons across package sizes.

Permanent offline regression tests: `python -m unittest discover -s tests -v`. Ten tests include 392 independent rational-arithmetic cases plus retailer formats, ranges, source conflicts, purchase-item scope, deposits, eligibility and identical front/back values. Additional source/cache/search/DOM checks passed. Five freshly extracted store datasets (863 offers total) were audited; not every offer provides an unambiguous unit price. Unknown values and source conflicts are deliberately visible rather than guessed. No guarantee of error-free retailer data or complete regular-price catalogue access is implied. Visual browser checks remain delegated to the user. These changes have not been deployed or verified on the production host.

## Comparison fallback reliability (21 November 2026)

Comparisons now keep offer-feed failures separate from regular-catalogue lookups. One failed product candidate no longer discards other usable candidates. An incompatible unit quote from an offer cannot suppress a compatible regular-price lookup. A validated exact GTIN can supply definite net quantity from the selected product when another source only publishes its per-purchase-item price; that provenance is retained in price details. Different-size family matching cannot borrow that quantity.

ICA exact-product browser lookup is now used after a current-offer miss, before the broader family browser search. Its structured result retains actual package size and explicit kg/litre/item units and passes through the shared decimal calculator. Existing access restrictions and host-wide refusal cooldowns remain in effect. Coop's validated pickup catalogue additionally supports exact-barcode lookup even when names differ or the selected brand is absent. This does not extend Stora Coop's prices to the other physical Coop branches.

City Gross family search now falls back from a narrow query to a bounded brand query (up to three 30-result pages per query). An omitted product-type word may be supplied by the retailer's explicit category, but the complete conservative brand/flavour/type match must still succeed. For example, `Dill&Gräslök` in category `Chips` can match OLW dill-and-chive chips; a dip, different brand or light variant cannot.

A fresh full comparison job for OLW Dill & Gräslök completed all 19 store rows in 19.17 seconds locally. It returned verified unit quotes for City Gross (89.09 kr/kg, buying two), Willys Öster and Flygstaden (94.18 kr/kg), and Stora Coop (96.36 kr/kg). Hemköp's displayed 33.07 kr/275g conflicts with its published 120.26 kr/kg (calculated 120.25), so that quote remains withheld. ICA ordinary-price access was refused by the source; other missing rows remain explicit. Four valid quotes out of 19 rows is **not** full store or product coverage. These are one product and one local run, not a general speed/success guarantee. Peak Python RSS was recorded separately in the local audit; browser-process peak memory and production-host behavior were not measured. No new packages, paid feeds, accounts, deployments or source-access bypasses were introduced.

The permanent offline suite now has 21 tests, including catalogue fallback isolation, exact quantity provenance, wrong-unit offer fallback, source-category matching and retained ICA package/unit fields. Existing source/cache/search/API tests also pass. User preference remains free official sources only.
