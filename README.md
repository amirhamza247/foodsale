# Willys Halmstad Öster · Streamlit

One Python application file with live, publicly available store offers. Includes a visible Ljust/Mörkt appearance selector, search, member filters, prices, multi-buy conditions, purchase limits, and validity dates. All results pages are fetched and checked. Personal coupons are excluded. Refreshes every 30 minutes while a browser session is active; shared data cache lasts five minutes. The refresh button clears the cache.

## Run locally

Use Python 3.15 (the version tested), or a compatible Python version supported by Streamlit. In this folder run:

```sh
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py
```

Stop with Ctrl+C in the terminal. Internet access is required. No Willys login is needed. Image requests go to Willys' image providers.

## Publish on Streamlit Community Cloud

1. Put this folder's contents in the root of a GitHub repository, including `.streamlit/config.toml`.
2. Sign into https://share.streamlit.io and connect that repository.
3. Create an app, select the repository and branch, and set the entrypoint to `app.py`.
4. Select Python 3.15 if offered. Otherwise use a supported Python version compatible with the pinned dependencies and verify deployment.
5. Deploy and open the assigned link. Confirm offers load from the hosting environment before sharing it.

The app has been tested locally against Willys with 257 current offers, including search and member filtering. It has not yet been deployed to Community Cloud. Willys may change its undocumented website data endpoints or restrict access from hosting providers; that requires retesting or updates.

Official deployment documentation: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app

## Appearance

Use Ljust (light) or Mörkt (dark) at the top of the page. Each visitor has their own choice. The URL preserves the selected mode across reloads. Both modes and reload persistence were verified in a browser.
