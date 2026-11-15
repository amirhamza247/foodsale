# Food Sale · Willys Halmstad Öster

One Python application file displaying current public offers from Willys Halmstad Öster, Laholmsvägen 37. No Willys login is required. Personal coupons are excluded.

- Light/dark appearance, retained in the URL across reloads.
- Overview of active offers, Willys Plus deals, and offers ending today.
- Search across products, brands, sizes, and descriptions; accents and word order are handled.
- Membership and expiry filters, plus expiry/name sorting.
- Product images, multi-buy wording, units, purchase limits, and validity dates.
- All results pages are checked. Cached prices are rechecked for date validity before display.
- Shared five-minute data cache; refresh button; automatic updates every 30 minutes while the session is active. Cache does not persist across server restarts.

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
