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
import unicodedata
import re
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
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



def require_mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'Unexpected {label} format from Willys.')
    return value


def validate_product(product):
    product = require_mapping(product, 'product')
    for field in ('name', 'manufacturer', 'displayVolume', 'price', 'priceUnit'):
        if product.get(field) is not None and not isinstance(product[field], str):
            raise ValueError(f'Unexpected product {field} format from Willys.')
    for field in ('image', 'thumbnail'):
        image = product.get(field)
        if image is not None:
            require_mapping(image, 'image')
            if image.get('url') is not None and not isinstance(image['url'], str):
                raise ValueError('Unexpected image URL format from Willys.')
    promotions = product.get('potentialPromotions')
    if promotions is None:
        return []
    if not isinstance(promotions, list):
        raise ValueError('Unexpected promotions format from Willys.')
    for promotion in promotions:
        require_mapping(promotion, 'promotion')
        for field in ('startDate', 'endDate', 'code', 'name', 'rewardLabel', 'cartLabel',
                      'conditionLabel', 'description', 'campaignType', 'weightVolume',
                      'textLabelManual', 'textLabel', 'textLabelGenerated',
                      'splashTitleText', 'redeemLimitLabel', 'comparePrice'):
            if promotion.get(field) is not None and not isinstance(promotion[field], str):
                raise ValueError(f'Unexpected promotion {field} format from Willys.')
    return promotions

@lru_cache(maxsize=512)
def parse_date(value):
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, '%d/%m-%Y').date()
    except (ValueError, TypeError):
        raise ValueError('Willys changed its offer date format; update this viewer.')


def clean_text(value):
    return str(value).strip() if value is not None else ''



def price_amount(value):
    """Accept a single Swedish amount, never ranges, percentages, or bundle text."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).replace(' ', '').replace('\xa0', '')
    if not re.fullmatch(r'\d+(?:[.,:]\d{1,2})?', text):
        return None
    try:
        return Decimal(text.replace(',', '.').replace(':', '.'))
    except InvalidOperation:
        return None


def discount_details(product, promotion):
    """Compare against the displayed ordinary price on the same priced unit.

    Grouped varieties may have different prices; this is a calculation for the
    source's displayed example, not a promise of identical savings on all variants.
    """
    ordinary = price_amount(product.get('priceNoUnit'))
    unit_match = re.fullmatch(r'kr/(st|kg|l)', clean_text(product.get('priceUnit')))
    if not unit_match or ordinary is None or ordinary <= 0:
        return dict(discount_percent=None, saving=None, saving_unit='')
    unit = unit_match.group(1)
    # Numeric fields cannot override explicit contradictory display units.
    labels = ' '.join(clean_text(promotion.get(k)) for k in ('rewardLabel', 'cartLabel'))
    displayed_units = re.findall(r'/\s*([a-zåäö]+)', labels.casefold())
    if any(display_unit != unit for display_unit in displayed_units) or '%' in labels:
        return dict(discount_percent=None, saving=None, saving_unit=unit)
    amount = price_amount(promotion.get('price'))
    condition = clean_text(promotion.get('conditionLabel'))
    quantity = price_amount(promotion.get('qualifyingCount'))
    if condition:
        bundle = re.fullmatch(r'(\d+)\s+för', condition)
        if not bundle or quantity is None or quantity <= 0 or quantity != Decimal(bundle.group(1)) or unit != 'st':
            amount = None
        elif amount is not None and quantity > 0:
            amount /= quantity
    elif quantity is not None and quantity != 1:
        amount = None
    # Weight-based offers often have no numeric promotion price.
    if amount is None and not condition:
        label = clean_text(promotion.get('rewardLabel'))
        match = re.fullmatch(r'(\d+(?:[.,:]\d{1,2})?)/(st|kg|l)(?:\s*\+pant)?', label)
        if match and match.group(2) == unit:
            amount = price_amount(match.group(1))
    if amount is None or amount < 0:
        return dict(discount_percent=None, saving=None, saving_unit=unit)
    saving = max(Decimal(0), ordinary - amount)
    return dict(discount_percent=float(saving / ordinary * 100), saving=float(saving), saving_unit=unit)

def normalize(results, today):
    if not isinstance(results, list):
        raise ValueError('Unexpected results format from Willys.')
    offers = []
    seen = set()
    for product in results:
        for promo in validate_product(product):
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
            cart_label = clean_text(promo.get('cartLabel'))
            if cart_label and cart_label != price:
                # Keep additional wording without repeating the displayed bundle price.
                extra = cart_label[:-len(price)].strip() if cart_label.endswith(price) else cart_label
                if extra:
                    details.append(extra)
            if promo.get('realMixAndMatch') is True and not any('välj & blanda' in t.casefold() for t in details):
                details.append('Välj & blanda!')
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
                conditions=details, start=start.isoformat(), end=end.isoformat(),
                **discount_details(product, promo)))
    return offers


@st.cache_data(ttl=86400, max_entries=1, show_spinner=False)
def store_information():
    return get_json('/axfood/rest/v2/store/' + STORE)


def fetch_offers():
    # Store identity and the first offers page are independent; overlap their I/O.
    params = dict(q=STORE, type='PERSONAL_GENERAL', page=0, size=200)
    with ThreadPoolExecutor(max_workers=2) as pool:
        store_future = pool.submit(store_information)
        page_future = pool.submit(get_json, '/axfood/rest/v2/search/campaigns/offline', params)
        store, first_page = store_future.result(), page_future.result()
    require_mapping(store, 'store')
    address = require_mapping(store['address'] if store.get('address') is not None else {}, 'store address')
    if store.get('storeId') != STORE or store.get('name') != 'Willys Halmstad Öster':
        raise ValueError('Store identity could not be confirmed; no offers displayed.')
    results = []
    expected_total = None
    expected_pages = None
    for page in range(100):
        data = first_page if page == 0 else get_json('/axfood/rest/v2/search/campaigns/offline',
                        dict(q=STORE, type='PERSONAL_GENERAL', page=page, size=200))
        require_mapping(data, 'offers response')
        batch = data.get('results')
        pagination = require_mapping(data.get('pagination'), 'pagination')
        pages = pagination.get('numberOfPages')
        total = pagination.get('totalNumberOfResults')
        current_page = pagination.get('currentPage')
        if (not isinstance(batch, list) or type(pages) is not int or not 0 <= pages <= 100
                or type(total) is not int or total < 0 or type(current_page) is not int
                or (pages == 0 and (batch or total))):
            raise ValueError('Willys changed the offers response; update this viewer.')
        if pagination.get('currentPage') != page:
            raise ValueError('Willys returned the wrong results page.')
        if expected_total is None:
            expected_total = total
            expected_pages = pages
        elif pages != expected_pages:
            raise ValueError('Offers changed during retrieval. Please retry in a few minutes.')
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
    return dict(store=store['name'], address=clean_text(address.get('formattedAddress')),
                source=SOURCE, fetchedAt=now.isoformat(), today=now.date().isoformat(),
                sourceResults=len(results), offers=normalize(results, now.date()))


@st.cache_data(ttl=300, max_entries=2, show_spinner=False, refresh_mode="background")
def cached_offers(stockholm_day, schema_version=3):
    # Global public-data cache: return a recent snapshot while refreshing after 5 minutes.
    # Streamlit bounds stale serving to one additional TTL by default.
    # Date key prevents yesterday's cache being reused after midnight.
    return fetch_offers()


def escape(value):
    return html.escape(str(value), quote=True)


def display_date(value):
    return dt.date.fromisoformat(value).strftime('%d/%m/%Y')


def search_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c))


def select_offers(offers, query='', mode='Alla erbjudanden', expiry='Alla slutdatum',
                  order='Willys ordning', today=None):
    today = today or dt.datetime.now(STOCKHOLM).date()
    terms = search_key(query).split()
    selected = []
    for offer in offers:
        end = dt.date.fromisoformat(offer['end'])
        start = dt.date.fromisoformat(offer['start'])
        if not start <= today <= end:
            continue
        if mode == 'Willys Plus' and not offer['member']:
            continue
        if mode == 'Utan medlemskrav' and offer['eligibility'] != 'GENERAL':
            continue
        days = (end - today).days
        if expiry == 'Slutar idag' and days != 0:
            continue
        if expiry == 'Inom 3 dagar' and days > 3:
            continue
        haystack = search_key(' '.join(offer[k] for k in ('name', 'brand', 'description', 'volume')))
        if all(term in haystack for term in terms):
            selected.append(offer)
    if order in ('Störst prissänkning (%)', 'Störst besparing (kr/enhet)'):
        metric = 'discount_percent' if order == 'Störst prissänkning (%)' else 'saving'
        selected.sort(key=lambda o: (o.get(metric) is None, -(o.get(metric) or 0), search_key(o['name'])))
    elif order == 'Slutar snart':
        selected.sort(key=lambda o: (o['end'], search_key(o['name'])))
    elif order == 'Namn A–Ö':
        selected.sort(key=lambda o: o['name'].casefold().translate(str.maketrans({'å':'{','ä':'|','ö':'}'})))
    return selected


def card(offer, today=None):
    today = today or dt.datetime.now(STOCKHOLM).date()
    remaining = (dt.date.fromisoformat(offer['end']) - today).days
    urgency = '<span class="badge ending">Sista dagen</span>' if remaining == 0 else ''
    membership = '<span class="badge plus">Willys Plus</span>' if offer['member'] else '<span class="badge general">Alla kunder</span>'
    if offer['eligibility'] not in ('LOYALTY', 'GENERAL'):
        membership = '<span class="badge">' + escape(offer['eligibility'] or 'Se villkor') + '</span>'
    discount = offer.get('discount_percent')
    saving = offer.get('saving')
    saving_label = ''
    if discount is not None and discount > 0 and saving is not None:
        membership += f'<span class="badge">−{discount:.0f} %</span>'
        saving_text = f'{saving:.2f}'.replace('.', ',')
        saving_label = f'<p class="ordinary">Besparing {saving_text} kr/{escape(offer["saving_unit"])}</p>'
    image = ('<img loading="lazy" referrerpolicy="no-referrer" src="' + escape(offer['image']) +
             '" alt="' + escape(offer['name']) + '">') if offer['image'] else '<span>Bild saknas</span>'
    ordinary = '<p class="ordinary">Ordinarie ' + escape(offer['ordinary']) + '</p>' if offer['ordinary'] else ''
    conditions = ''.join('<div>' + escape(v) + '</div>' for v in offer['conditions'])
    brand = ' · '.join(filter(None, [offer['brand'], offer['volume']]))
    return f'''<article class="offer-card"><div class="badges">{membership}{urgency}</div>
<div class="offer-picture">{image}</div><div class="offer-brand">{escape(brand)}</div>
<h2>{escape(offer['name'])}</h2><p class="offer-description">{escape(offer['description'])}</p>
<div class="price-block"><div class="offer-price">{escape(offer['price'])}</div>{ordinary}{saving_label}</div>
<div class="conditions">{conditions}</div><div class="offer-dates">{display_date(offer['start'])} – {display_date(offer['end'])}</div></article>'''


CSS = '''<style>
.stApp{background:var(--viewer-bg);color:var(--viewer-text)}
.block-container{max-width:1390px;padding:5rem 3rem 3rem}
.hero{padding:0 0 16px;border-bottom:1px solid var(--viewer-border);margin-bottom:4px}
.eyebrow{display:flex;align-items:center;gap:10px;font-size:11px;letter-spacing:.17em;font-weight:800;text-transform:uppercase;color:var(--viewer-accent)}
.brand-mark{background:#a12d26;color:white;border-radius:8px;letter-spacing:-.04em;padding:7px 10px;font-size:17px}
.hero h1{font-size:clamp(28px,3.5vw,44px);font-weight:750;letter-spacing:-.055em;line-height:1.15;margin:8px 0 10px;color:var(--viewer-text)}
.hero p{font-size:14px;color:var(--viewer-muted);margin:0}
.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:0 0 4px}
.summary-item{padding:10px 16px;border:1px solid var(--viewer-border);background:var(--viewer-card);border-radius:12px;display:flex;align-items:baseline;gap:10px}
.summary-item strong{font-size:25px;letter-spacing:-.04em;color:var(--viewer-text)}
.summary-item span{font-size:12px;color:var(--viewer-muted)}
.results-bar{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:20px 0 16px;font-size:12px;color:var(--viewer-muted)}
.results-bar strong{color:var(--viewer-text);font-size:14px}
.offer-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(235px,1fr));gap:18px}
.offer-card{background:var(--viewer-card);border:1px solid var(--viewer-border);border-radius:16px;padding:18px;display:flex;flex-direction:column;min-width:0;transition:border-color .15s}
.offer-card:hover{border-color:var(--viewer-accent)}
.badges{display:flex;align-items:flex-start;flex-wrap:wrap;gap:5px;min-height:24px}.badge{padding:4px 7px;border-radius:5px;font-size:10px;font-weight:750;letter-spacing:.02em;color:var(--viewer-muted);background:var(--viewer-bg)}
.badge.plus{color:var(--viewer-accent);border:1px solid var(--viewer-border)}.badge.ending{background:var(--viewer-accent);color:var(--viewer-bg)}
.offer-picture{height:165px;display:flex;align-items:center;justify-content:center;background:white;border-radius:10px;margin:14px 0 16px;color:#62625d;font-size:12px;padding:10px}
.offer-picture img{max-width:100%;max-height:145px;object-fit:contain}
.offer-brand{font-size:10px;letter-spacing:.03em;text-transform:uppercase;color:var(--viewer-muted);line-height:1.6}
.offer-card h2{font-size:19px;font-weight:700;margin:6px 0 8px;line-height:1.3;color:var(--viewer-text);letter-spacing:-.025em;overflow-wrap:anywhere}
.offer-description{font-size:12px;color:var(--viewer-muted);line-height:1.6;margin:0 0 18px}
.price-block{margin-top:auto}.offer-price{font-size:28px;font-weight:800;letter-spacing:-.045em;color:var(--viewer-accent);line-height:1.3;overflow-wrap:anywhere}
.ordinary{font-size:11px;color:var(--viewer-muted);margin:6px 0 12px}
.conditions{font-size:11px;line-height:1.7;color:var(--viewer-muted)}
.offer-dates{padding-top:12px;margin-top:14px;border-top:1px solid var(--viewer-border);font-size:10px;color:var(--viewer-muted);line-height:1.5}
.footnote{font-size:11px;color:var(--viewer-muted);line-height:1.8;padding-top:24px;margin-top:24px;border-top:1px solid var(--viewer-border)}
@media(max-width:640px){.block-container{padding:4.5rem 1rem 2rem}.hero{padding-bottom:20px}.hero h1{font-size:34px}.summary{gap:6px}.summary-item{padding:10px;display:block}.summary-item strong{font-size:22px;display:block}.summary-item span{font-size:10px}.offer-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.offer-card{padding:12px}.offer-picture{height:125px;padding:6px}.offer-picture img{max-height:113px}.offer-card h2{font-size:16px}.offer-price{font-size:23px}.badges{min-height:40px}.badge{font-size:9px}}
@media(max-width:340px){.offer-grid{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){.offer-card{transition:none}}
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
    [data-baseweb="select"]>div, [data-baseweb="select"]>div>div {background:var(--viewer-input)!important;color:var(--viewer-text)!important;border-color:var(--viewer-border)!important}
    [data-testid="stTextInput"] input::placeholder {color:var(--viewer-muted)}
    [data-baseweb="popover"], [role="listbox"], [role="option"] {background:var(--viewer-card)!important;color:var(--viewer-text)!important}
    [role="option"]:hover {background:var(--viewer-border)!important}
    [data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"] {background:var(--viewer-card);color:var(--viewer-text);border-color:var(--viewer-border)}
    [data-testid="stBaseButton-primary"] {background:#a12d26;color:white;border-color:#a12d26}
    .offer-picture {background:white;border-radius:10px}
    </style>""".replace('__SCHEME__', 'dark' if dark else 'light')

def main():
    st.set_page_config(page_title='Food Sale · Willys Öster', page_icon='🛒', layout='wide')
    initial = 1 if st.query_params.get('theme') == 'dark' else 0
    left, right = st.columns([4, 1], vertical_alignment='center')
    with left:
        st.html('<div class="eyebrow"><span class="brand-mark">fs.</span> Food Sale / Halmstad</div>')
    with right:
        appearance = st.radio('Utseende', ['Ljust', 'Mörkt'], index=initial, horizontal=True, key='appearance', label_visibility='collapsed')
    dark = appearance == 'Mörkt'
    selected = 'dark' if dark else 'light'
    if st.query_params.get('theme') != selected:
        st.query_params['theme'] = selected
    st.html(theme_css(dark) + CSS)
    st.html('<section class="hero"><h1>Veckans fynd, nära dig.</h1><p>Willys Halmstad Öster · Laholmsvägen 37</p></section>')
    offers_view()
    st.html('<div class="footnote">Aktuella offentliga butikserbjudanden från Willys. Medlemspriser kräver Willys Plus. '
            'Personliga kuponger ingår inte. Bilder hämtas från Willys bildleverantörer. Fristående visare.</div>')
    st.link_button('Se erbjudanden hos Willys ↗', SOURCE)


@st.fragment(run_every=60)
def offers_view():
    today = dt.datetime.now(STOCKHOLM).date()
    try:
        with st.spinner('Hämtar butikens erbjudanden…'):
            result = cached_offers(today.isoformat())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError):
        st.error('Erbjudandena kunde inte hämtas. Kontrollera anslutningen eller försök igen. Inga gamla priser visas.')
        if st.button('Försök igen'):
            cached_offers.clear()
            st.rerun()
        return
    fetched_at = dt.datetime.fromisoformat(result['fetchedAt']).astimezone(STOCKHOLM)
    age_seconds = max(0, (dt.datetime.now(STOCKHOLM) - fetched_at).total_seconds())
    if age_seconds > 300:
        st.warning('Visar sparade erbjudanden från ' + fetched_at.strftime('%H:%M') +
                   '. Hämtar aktuella priser i bakgrunden. Om uppdateringen misslyckas kan priserna ha ändrats.')
    # Cache age and validity are separate: discard expired offers even within a cached response.
    current = select_offers(result['offers'], today=today)
    members = sum(o['member'] for o in current)
    ending = sum(o['end'] == today.isoformat() for o in current)
    st.html('<div class="summary">' + ''.join(
        f'<div class="summary-item"><strong>{number}</strong><span>{label}</span></div>'
        for number, label in [(len(current), 'aktuella erbjudanden'), (members, 'Willys Plus'), (ending, 'slutar idag')]) + '</div>')
    search_col, member_col = st.columns([2, 1], vertical_alignment='bottom')
    with search_col:
        query = st.text_input('Sök produkter', placeholder='Sök vara, varumärke eller storlek…', key='search')
    with member_col:
        mode = st.selectbox('Medlemskrav', ['Alla erbjudanden', 'Utan medlemskrav', 'Willys Plus'], key='membership')
    expiry_col, order_col, refresh_col = st.columns([1.3, 1.3, 1], vertical_alignment='bottom')
    with expiry_col:
        expiry = st.selectbox('Giltighet', ['Alla slutdatum', 'Slutar idag', 'Inom 3 dagar'], key='expiry')
    with order_col:
        order = st.selectbox('Sortera', ['Willys ordning', 'Störst prissänkning (%)', 'Störst besparing (kr/enhet)', 'Slutar snart', 'Namn A–Ö'], key='sort',
                             help='Rabatten beräknas mot visat ordinarie pris. Flerköp räknas per vara; viktpriser per kg. Sorter kan skilja sig. Okända jämförelser visas sist.')
    with refresh_col:
        if st.button('↻ Uppdatera', use_container_width=True):
            cached_offers.clear()
            st.rerun()
    offers = select_offers(current, query, mode, expiry, order, today)
    if order in ('Störst prissänkning (%)', 'Störst besparing (kr/enhet)'):
        st.caption('Beräknat från visat ordinarie pris. Flerköp räknas per vara, viktpriser per kg/l. '
                   'Priset kan variera mellan sorter. Erbjudanden utan jämförbara priser visas sist.')
    fetched = dt.datetime.fromisoformat(result['fetchedAt']).astimezone(STOCKHOLM).strftime('%H:%M')
    st.html(f'<div class="results-bar"><strong>{len(offers)} av {len(current)} erbjudanden</strong>'
            f'<span>Hämtat {fetched} · Svensk tid · Kontrolleras automatiskt</span></div>')
    if not offers:
        st.info('Inga matchningar. Prova ett annat sökord eller välj Alla slutdatum och Alla erbjudanden.')
        return
    # Filtering always uses the full dataset; only the initial DOM is kept small.
    filter_signature = (query, mode, expiry, order)
    if st.session_state.get('display_filter') != filter_signature:
        st.session_state['display_filter'] = filter_signature
        st.session_state['show_all_offers'] = False
    show_all = st.session_state.get('show_all_offers', False)
    visible = offers if show_all else offers[:48]
    st.html('<div class="offer-grid">' + ''.join(card(offer, today) for offer in visible) + '</div>')
    if len(visible) < len(offers):
        st.caption(f'Visar {len(visible)} av {len(offers)} matchningar. Sökningen omfattar alla erbjudanden.')
        if st.button(f'Visa alla {len(offers)} erbjudanden', use_container_width=True):
            st.session_state['show_all_offers'] = True
            st.rerun()


if __name__ == '__main__':
    main()
