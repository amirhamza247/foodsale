"""Streamlit viewer for Willys Halmstad Öster.
Run: streamlit run app.py
All application code lives in this file. Internet access is required.
"""
import datetime as dt
import html
import json
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
import streamlit as st

BASE = "https://www.willys.se"
STORE = "2236"
SOURCE = BASE + "/erbjudanden/butik?StoreID=" + STORE
STOCKHOLM = ZoneInfo("Europe/Stockholm")

def get_json(path, params=None):
    url = BASE + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        'Accept': 'application/json', 'User-Agent': 'WillysLocalOffersViewer/1.0'})
    with urllib.request.urlopen(request, timeout=25) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError('Willys returned an unexpected response format.')
    return value


def parse_date(value):
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, '%d/%m-%Y').date()
    except (ValueError, TypeError):
        raise ValueError('Willys changed its offer date format; update this viewer.')


def clean_text(value):
    return str(value).strip() if value is not None else ''


def normalize(results, today):
    offers = []
    seen = set()
    for product in results:
        for promo in product.get('potentialPromotions') or []:
            start, end = parse_date(promo.get('startDate')), parse_date(promo.get('endDate'))
            # Do not silently present undated promotions as verified current deals.
            if not start or not end:
                raise ValueError('An offer has no validity dates; current offers cannot be verified.')
            if today < start or today > end:
                continue
            name = clean_text(product.get('name') or promo.get('name'))
            identity = (promo.get('code'), name, product.get('manufacturer'), product.get('displayVolume'))
            if identity in seen:
                continue
            seen.add(identity)
            image = (product.get('image') or product.get('thumbnail') or {}).get('url', '')
            if urllib.parse.urlsplit(image).scheme != 'https':
                image = ''
            price = clean_text(promo.get('rewardLabel') or promo.get('cartLabel'))
            if not price:
                raise ValueError('An offer has no display price; update this viewer.')
            condition = clean_text(promo.get('conditionLabel'))
            if condition:
                price = condition + ' ' + price
            ordinary = clean_text(product.get('price'))
            unit = clean_text(product.get('priceUnit'))
            if ordinary and '/' not in ordinary and '/' in unit:
                ordinary += '/' + unit.split('/', 1)[1]
            # Keep multi-buy wording, conditions, units, and eligibility from the source.
            details = []
            for key in ('textLabelManual', 'textLabel',
                        'textLabelGenerated', 'splashTitleText', 'redeemLimitLabel', 'comparePrice'):
                text = clean_text(promo.get(key))
                if text and text not in details and text != price:
                    details.append(text)
            offers.append(dict(
                name=name, brand=clean_text(product.get('manufacturer')),
                volume=clean_text(product.get('displayVolume') or promo.get('weightVolume')),
                description=clean_text(promo.get('description')), image=image,
                price=price, ordinary=ordinary,
                member=promo.get('campaignType') == 'LOYALTY',
                eligibility=clean_text(promo.get('campaignType')),
                conditions=details, start=start.isoformat(), end=end.isoformat()))
    return offers


def fetch_offers():
    store = get_json('/axfood/rest/v2/store/' + STORE)
    if store.get('storeId') != STORE or store.get('name') != 'Willys Halmstad Öster':
        raise ValueError('Store identity could not be confirmed; no offers displayed.')
    results = []
    expected_total = None
    for page in range(100):
        data = get_json('/axfood/rest/v2/search/campaigns/offline',
                        dict(q=STORE, type='PERSONAL_GENERAL', page=page, size=200))
        batch, pagination = data.get('results'), data.get('pagination') or {}
        if not isinstance(batch, list) or not isinstance(pagination.get('numberOfPages'), int):
            raise ValueError('Willys changed the offers response; update this viewer.')
        if pagination.get('currentPage') != page:
            raise ValueError('Willys returned the wrong results page.')
        if expected_total is None:
            expected_total = pagination.get('totalNumberOfResults')
        elif pagination.get('totalNumberOfResults') != expected_total:
            raise ValueError('Offers changed during retrieval. Please retry in a few minutes.')
        results.extend(batch)
        if page + 1 >= pagination['numberOfPages']:
            break
    else:
        raise ValueError('Too many results pages; retrieval stopped.')
    if expected_total != len(results):
        raise ValueError('Incomplete offers response; please retry.')
    now = dt.datetime.now(STOCKHOLM)
    return dict(store=store['name'], address=store.get('address', {}).get('formattedAddress', ''),
                source=SOURCE, fetchedAt=now.isoformat(), today=now.date().isoformat(),
                sourceResults=len(results), offers=normalize(results, now.date()))


@st.cache_data(ttl=300, show_spinner=False)
def cached_offers(stockholm_day):
    # Date key prevents yesterday's cache being reused after midnight.
    return fetch_offers()


def escape(value):
    return html.escape(str(value), quote=True)


def display_date(value):
    return dt.date.fromisoformat(value).strftime('%d/%m/%Y')


def card(offer):
    tag = 'WILLYS PLUS' if offer['member'] else ('ERBJUDANDE' if offer['eligibility'] == 'GENERAL' else offer['eligibility'])
    image = ''
    if offer['image']:
        image = f'<img loading="lazy" referrerpolicy="no-referrer" src="{escape(offer["image"])}" alt="{escape(offer["name"])}">'
    ordinary = f'<p class="ordinary">Ordinarie: {escape(offer["ordinary"])}</p>' if offer['ordinary'] else ''
    conditions = ''.join(f'<div class="conditions">{escape(value)}</div>' for value in offer['conditions'])
    brand = ' · '.join(filter(None, [offer['brand'], offer['volume']]))
    return f'''<article class="offer-card"><div class="offer-tag">{escape(tag)}</div>
<div class="offer-picture">{image}</div><div class="offer-brand">{escape(brand)}</div>
<h2>{escape(offer['name'])}</h2><p class="offer-description">{escape(offer['description'])}</p>
<div class="offer-price">{escape(offer['price'])}</div>{ordinary}{conditions}
<div class="offer-dates">Gäller {display_date(offer['start'])}–{display_date(offer['end'])}</div></article>'''


CSS = '''<style>
.stApp {background:var(--viewer-bg);color:var(--viewer-text)} .block-container{max-width:1320px;padding-top:2.5rem}
.offer-eyebrow{color:var(--viewer-accent);font-size:12px;font-weight:750;letter-spacing:.12em;text-transform:uppercase}
.offer-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(235px,1fr));gap:16px;margin-top:16px}
.offer-card{background:var(--viewer-card);border:1px solid var(--viewer-border);border-radius:16px;padding:20px;display:flex;flex-direction:column}
.offer-picture{height:160px;display:flex;align-items:center;justify-content:center;margin:10px 0 14px}
.offer-picture img{max-width:100%;max-height:160px;object-fit:contain}
.offer-tag{font-size:11px;font-weight:750;color:var(--viewer-accent);min-height:20px}
.offer-brand{font-size:12px;color:var(--viewer-muted);min-height:16px}
.offer-card h2{font-size:19px!important;margin:6px 0!important;padding:0!important;line-height:1.3!important;color:var(--viewer-text)!important}
.offer-description{font-size:13px!important;color:var(--viewer-muted);line-height:1.5;margin:0 0 12px}
.offer-price{font-size:29px;font-weight:800;letter-spacing:-.04em;color:var(--viewer-accent);margin-top:auto}
.ordinary{font-size:12px!important;color:var(--viewer-muted);margin:6px 0}.conditions,.offer-dates{font-size:12px;line-height:1.6;color:var(--viewer-muted)}
.offer-dates{padding-top:12px;margin-top:12px;border-top:1px solid var(--viewer-border)}
@media(max-width:540px){.offer-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.offer-card{padding:12px}.offer-picture{height:110px}.offer-picture img{max-height:110px}.offer-price{font-size:23px}.offer-card h2{font-size:16px!important}}
</style>'''



def theme_css(dark):
    colors = dict(bg='#17191d', card='#22262c', text='#f1f2f5', muted='#b8bfc9',
                  border='#414854', accent='#ffaaa3', input='#252a32') if dark else dict(
                  bg='#f6f5f1', card='#ffffff', text='#222222', muted='#62625d',
                  border='#d3d1c9', accent='#a12d26', input='#ffffff')
    variables = ';'.join(f'--viewer-{key}:{value}' for key, value in colors.items())
    return '<style>:root,.stApp{' + variables + '}'+ r"""
    .stApp {color-scheme:__SCHEME__}
    [data-testid="stHeader"] {background:var(--viewer-bg)}
    [data-testid="stHeading"], [data-testid="stWidgetLabel"],
    [data-testid="stCaptionContainer"], [data-testid="stRadio"] label,
    [data-testid="stMarkdownContainer"] {color:var(--viewer-text)!important}
    [data-testid="stTextInput"] input, [role="combobox"],
    [data-baseweb="select"]>div {background:var(--viewer-input)!important;color:var(--viewer-text)!important;border-color:var(--viewer-border)!important}
    [data-testid="stTextInput"] input::placeholder {color:var(--viewer-muted)}
    [data-baseweb="popover"], [role="listbox"], [role="option"] {background:var(--viewer-card)!important;color:var(--viewer-text)!important}
    [role="option"]:hover {background:var(--viewer-border)!important}
    [data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"] {background:var(--viewer-card);color:var(--viewer-text);border-color:var(--viewer-border)}
    [data-testid="stBaseButton-primary"] {background:#a12d26;color:white;border-color:#a12d26}
    .offer-picture {background:white;border-radius:10px}
    </style>""".replace('__SCHEME__', 'dark' if dark else 'light')

def main():
    st.set_page_config(page_title='Willys Öster · Erbjudanden', page_icon='🛒', layout='wide')
    initial = 1 if st.query_params.get('theme') == 'dark' else 0
    appearance = st.radio('Utseende', ['Ljust', 'Mörkt'], index=initial, horizontal=True, key='appearance')
    dark = appearance == 'Mörkt'
    selected = 'dark' if dark else 'light'
    if st.query_params.get('theme') != selected:
        st.query_params['theme'] = selected
    st.markdown(theme_css(dark) + CSS, unsafe_allow_html=True)
    st.markdown('<div class="offer-eyebrow">Din lokala erbjudandesida</div>', unsafe_allow_html=True)
    st.title('Willys Halmstad Öster')
    st.caption('Laholmsvägen 37, Halmstad · Aktuella butikserbjudanden')
    offers_view()
    st.caption('Visar offentliga butikserbjudanden som gäller idag, inklusive vecko- och längre kampanjer. '
               'Personliga kuponger ingår inte. Bilder hämtas från Willys bildleverantörer. Fristående visare.')
    st.link_button('Kontrollera hos Willys', SOURCE)


@st.fragment(run_every=1800)
def offers_view():
    query = st.text_input('Sök produkter', placeholder='Sök bland erbjudanden…')
    mode = st.selectbox('Medlemskrav', ['Alla erbjudanden', 'Utan medlemskrav', 'Willys Plus'])
    refresh = st.button('Uppdatera', type='primary')
    if refresh:
        cached_offers.clear()
    try:
        with st.spinner('Hämtar aktuella erbjudanden från Willys…'):
            result = cached_offers(dt.datetime.now(STOCKHOLM).date().isoformat())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError):
        st.error('Kunde inte hämta verifierade erbjudanden. Kontrollera internetanslutningen och försök igen. '
                 'Willys kan ha ändrat sin webbplats. Inga gamla priser visas.')
        return
    query = query.strip().casefold()
    offers = [offer for offer in result['offers']
              if (mode != 'Willys Plus' or offer['member'])
              and (mode != 'Utan medlemskrav' or offer['eligibility'] == 'GENERAL')
              and query in ' '.join(offer[k] for k in ('name', 'brand', 'description', 'volume')).casefold()]
    fetched = dt.datetime.fromisoformat(result['fetchedAt']).astimezone(STOCKHOLM).strftime('%d/%m/%Y %H:%M')
    st.caption(f"{len(offers)} av {len(result['offers'])} aktuella erbjudanden · Hämtat {fetched} (svensk tid) · Automatisk uppdatering var 30:e minut när sidan är öppen")
    if not offers:
        st.info('Inga erbjudanden matchar ditt val.')
        return
    st.markdown('<div class="offer-grid">' + ''.join(card(offer) for offer in offers) + '</div>', unsafe_allow_html=True)


if __name__ == '__main__':
    main()
