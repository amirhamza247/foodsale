# Food Sale · Willys Halmstad Öster

One Python application file displaying current public offers from Willys Halmstad Öster, Laholmsvägen 37. No Willys login is required. Personal coupons are excluded.

- Light/dark appearance, retained in the URL across reloads.
- Overview of active offers, Willys Plus deals, and offers ending today.
- Search across products, brands, sizes, and descriptions; accents and word order are handled.
- Membership and expiry filters, plus expiry/name sorting.
- Product images, multi-buy wording, units, purchase limits, and validity dates.
- All results pages are checked. Cached prices are rechecked for date validity before display.
- Shared five-minute data cache with background refresh. A recent snapshot is returned immediately while refreshing; a visible warning labels data older than five minutes. Streamlit limits this stale window to one extra five-minute TTL by default, then requires a successful fetch. The page checks once a minute while active. Expired offers are always filtered out.
- Store metadata is cached for 24 hours, independent initial requests overlap, and date parsing is memoized.
- Initially renders 48 cards; search and filters cover the entire dataset. Use “Visa alla” to display all matches on the same page.
- Manual refresh fetches current data. Caches are memory-only and do not survive restarts or hosting sleep.

## Run

From this repository, with a Python version supported by Streamlit:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Python 3.15 and Streamlit 1.67.0 were used for local verification. Internet access is required. Stop with Ctrl+C. Images are requested from Willys' image providers.

## Streamlit Community Cloud

Push `app.py`, `requirements.txt`, and `.streamlit/config.toml` to GitHub. Keep the config in that subdirectory: a root-level `config.toml` is not loaded by Streamlit. In Community Cloud choose the repository, branch, and `app.py` entrypoint. Select a compatible Python version and deploy. Verify that offers load from the hosting environment before sharing its URL.

The Willys website endpoints are undocumented and may change or restrict cloud access. Local testing does not establish hosted reliability. This is an independent offers viewer.

## Verification

The improvement pass checked 257 live offers, membership and expiry filtering, accent/multiword search, sorting, empty results, expired records, escaped HTML, incomplete source responses, and network failures. Browser checks covered light/dark appearance, search, expiry filtering, theme persistence, and a 390-pixel phone layout. No deployment or performance improvement is claimed from those checks.

## Caching verification

Verified background refresh returns a stale snapshot without blocking, concurrent readers share one refresh, completed refreshes replace cached values, and over-age data is refused if the source fails. UI checks cover the initial 48 cards, showing all 257, full-dataset search, and stale-data warnings. One local run measured the uncached fetch at 1.73 seconds before and 1.28 seconds after; a warm cache read was about 0.0009 seconds. These measure server data access, not total page load, and are not a hosted performance guarantee. Hosting cold starts and image downloads can still add latency.

## Biggest price drops

Under Sortera, choose `Störst prissänkning (%)` for descending relative reductions, or `Störst besparing (kr/enhet)` for descending kronor saved per source-priced unit (item, kg, or litre). Multi-buy totals are divided by the required item count. Missing, ranged, or incompatible price comparisons go last. Cards show the calculated reduction and saving. Calculations use the displayed ordinary price; grouped varieties can differ. These are offer-versus-ordinary comparisons, not historical price tracking. Verified against 257 live offers and focused tests for bundles, weight prices, unknown prices, and descending order.

Audit fixes preserve source mix-and-match qualifiers, reject conflicting display units even when a numeric promotion price exists, and validate nested source shapes before parsing. Automated regression checks cover these cases and the friendly error state.
