"""Streamlit viewer for public Halmstad grocery offers.
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
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
import streamlit as st

BASE = "https://www.willys.se"
STORE = "2236"
SOURCE = BASE + "/erbjudanden/butik?StoreID=" + STORE
STOCKHOLM = ZoneInfo("Europe/Stockholm")


# Public store catalogue. Unsupported product feeds have an explicit official-page view.
STORES = {
    'willys-oster': dict(name='Willys Halmstad Öster', provider='axfood', id='2236', base='https://www.willys.se', address='Laholmsvägen 37', member='Willys Plus', source=SOURCE),
    'willys-flygstaden': dict(name='Willys Halmstad Flygstaden', provider='axfood', id='2137', base='https://www.willys.se', address='Kundvägen 4', member='Willys Plus', source='https://www.willys.se/erbjudanden/butik?StoreID=2137'),
    'hemkop-c': dict(name='Hemköp Halmstad C', provider='axfood', id='4245', base='https://www.hemkop.se', address='Brogatan 14–16', member='Klubb Hemköp', source='https://www.hemkop.se/butik/4245'),
    'coop-stora': dict(name='Stora Coop Halmstad', provider='coop', id='105610', address='Prästvägen 1', member='Coop-medlem', source='https://www.coop.se/butiker-erbjudanden/stora-coop/stora-coop-halmstad/'),
    'coop-gamletull': dict(name='Coop Gamletull Halmstad', provider='coop', id='106112', address='Gamletullsgatan 20', member='Coop-medlem', source='https://www.coop.se/butiker-erbjudanden/coop/coop-gamletull-halmstad/'),
    'coop-flygaregatan': dict(name='Coop Flygaregatan', provider='coop', id='106117', address='Flygaregatan 12', member='Coop-medlem', source='https://www.coop.se/butiker-erbjudanden/coop/coop-flygaregatan/'),
    'coop-furuvagen': dict(name='Coop Furuvägen', provider='coop', id='106111', address='Furuvägen 63', member='Coop-medlem', source='https://www.coop.se/butiker-erbjudanden/coop/coop-furuvagen/'),
    'coop-fyllinge': dict(name='Coop Fyllinge', provider='coop', id='106119', address='Fyllingevägen 2', member='Coop-medlem', source='https://www.coop.se/butiker-erbjudanden/coop/coop-fyllinge/'),
    'xtra-snostorp': dict(name='X:-Tra Snöstorpsvägen', provider='coop', id='106118', address='Snöstorpsvägen 109', member='Coop-medlem', source='https://x-tra.se/butiker/snoestorpsvaegen'),
    'lidl-torpaledsvagen': dict(name='Lidl Torpaledsvägen', provider='lidl', address='Torpaledsvägen 4', member='Lidl Plus', source='https://www.lidl.se/s/sv-SE/butiker/halmstad/torpaledsvaegen-4/'),
    'lidl-gesallgatan': dict(name='Lidl Gesällgatan', provider='lidl', address='Gesällgatan 3', member='Lidl Plus', source='https://www.lidl.se/s/sv-SE/butiker/halmstad/gesaellgatan-3/'),
    'lidl-tegelbruket': dict(name='Lidl Tegelbruket', provider='lidl', address='Tegelbruket 1', member='Lidl Plus', source='https://www.lidl.se/s/sv-SE/butiker/halmstad/tegelbruket-1/'),
    'ica-hogskolan': dict(name='Maxi ICA Stormarknad Högskolan', provider='official', address='Halmstad', source='https://www.ica.se/erbjudanden/maxi-ica-stormarknad-hogskolan-1003925/'),
    'ica-flygstaden': dict(name='Maxi ICA Stormarknad Flygstaden', provider='official', address='Halmstad', source='https://www.ica.se/erbjudanden/maxi-ica-stormarknad-flygstaden-1003781/'),
    'ica-vallas': dict(name='ICA Supermarket Vallås', provider='official', address='Halmstad', source='https://www.ica.se/erbjudanden/ica-supermarket-vallas-1003811/'),
    'ica-sondrum': dict(name='ICA Supermarket Söndrum', provider='official', address='Halmstad', source='https://www.ica.se/erbjudanden/ica-supermarket-sondrum-1003648/'),
    'ica-frosakull': dict(name='ICA Nära Frösakull', provider='official', address='Halmstad', source='https://www.ica.se/erbjudanden/ica-nara-frosakull-1004064/'),
    'citygross': dict(name='City Gross Halmstad', provider='official', address='Stormgatan 14', source='https://www.citygross.se/butiker/halmstad'),
    'tempo-backagard': dict(name='Tempo Halmstad Bäckagård', provider='official', address='Akvarellgränd 1', source='https://www.tempo.se/butiker/tempo-halmstad-backagard/'),
}


def read_public(url, headers=None):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'FoodSale/2.0 (public offers viewer)',
        'Accept': 'application/json,text/html', **(headers or {})})
    with urllib.request.urlopen(request, timeout=25) as response:
        # Bound responses so an unexpected source cannot exhaust server memory.
        body = response.read(12_000_001)
        if len(body) > 12_000_000:
            raise ValueError('Butikens svar är för stort.')
        return body.decode('utf-8')


def public_json(url, headers=None):
    return json.loads(read_public(url, headers))


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('Oväntat textformat från butiken.')
    parser = TextOnly()
    parser.feed(value)
    return ' '.join(' '.join(parser.parts).split())


def iso_day(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Erbjudandet saknar giltiga datum.')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(STOCKHOLM)
    return parsed.date()


def safe_image(value):
    if not isinstance(value, str):
        raise ValueError('Oväntat bildformat från butiken.')
    if value.startswith('//'):
        value = 'https:' + value
    return value if urllib.parse.urlsplit(value).scheme == 'https' else ''


def money(value):
    amount = price_amount(value)
    if amount is None:
        raise ValueError('Erbjudandet saknar ett giltigt pris.')
    return format(amount, '.2f').replace('.', ',')


@st.cache_data(ttl=86400, max_entries=1, show_spinner=False)
def coop_access():
    # Read the same anonymous-client configuration used by Coop's public store page.
    page = read_public('https://www.coop.se/butiker-erbjudanden/coop/coop-fyllinge/')
    marker = 'window.coopSettings = '
    if marker not in page:
        raise ValueError('Coops offentliga butikssida har ändrats.')
    settings = json.JSONDecoder().raw_decode(page.split(marker, 1)[1])[0]
    access = require_mapping(settings.get('serviceAccess'), 'Coop configuration')
    result = {k: access[k] for k in ('storeApiUrl', 'storeApiSubscriptionKey', 'dkeUrl', 'dkeKey')}
    for key in ('storeApiUrl', 'dkeUrl'):
        url = urllib.parse.urlsplit(result[key])
        if url.scheme != 'https' or url.hostname not in ('proxy.api.coop.se', 'external.api.coop.se'):
            raise ValueError('Coops datakälla kunde inte bekräftas.')
    return result


@st.cache_data(ttl=86400, max_entries=6, show_spinner=False)
def coop_store(store_key):
    config = STORES[store_key]
    access = coop_access()
    url = access['storeApiUrl'].rstrip('/') + '/stores/' + config['id'] + '?api-version=v7&includeFlyers=true'
    data = require_mapping(public_json(url, {'Ocp-Apim-Subscription-Key': access['storeApiSubscriptionKey']}), 'Coop store')
    if (str(data.get('ledgerAccountNumber')) != config['id'] or data.get('name') != config['name']
            or clean_text(data.get('city')).casefold() != 'halmstad'):
        raise ValueError('Butikens identitet kunde inte bekräftas.')
    return data


def normalize_coop(data, config, today):
    groups = require_mapping(data, 'Coop response').get('sortingGroups')
    if not isinstance(groups, list):
        raise ValueError('Coops erbjudandeformat har ändrats.')
    matches = [require_mapping(g, 'Coop group') for g in groups if isinstance(g, dict) and g.get('id') == 'alla']
    if len(matches) != 1:
        raise ValueError('Coops fullständiga erbjudandelista saknas.')
    group = matches[0]
    records = group.get('offers')
    if not isinstance(records, list) or type(group.get('offerAmount')) is not int or group['offerAmount'] != len(records):
        raise ValueError('Coops erbjudandelista är ofullständig.')
    offers, seen = [], set()
    for raw in records:
        raw = require_mapping(raw, 'Coop offer')
        if str(raw.get('storeLedgerAccountNumber')) != config['id']:
            raise ValueError('Erbjudandet tillhör en annan butik.')
        start, end = iso_day(raw.get('campaignStartDate')), iso_day(raw.get('campaignEndDate'))
        if not start <= today <= end:
            continue
        content = require_mapping(raw.get('content'), 'Coop offer content')
        price = require_mapping(raw.get('priceInformation'), 'Coop price')
        member = price.get('isMemberPrice') is True
        unit = plain_text(price.get('unit'))
        amount = money(price.get('discountValue'))
        quantity = price_amount(price.get('quantity'))
        if price.get('termStyle') == 'Percentage Off':
            label = amount + ' % rabatt'
        elif price.get('termStyle') != 'Discount Price':
            raise ValueError('Okänd pristyp från Coop.')
        elif quantity is not None and quantity > 1:
            label = f'{quantity:g} för {amount} kr'
        else:
            label = amount + ' kr' + ('/' + unit if unit else '')
        conditions = [plain_text(content.get(k)) for k in
                      ('dealOfferLimitText', 'recyclingInformationText', 'formattedComparativePriceText')]
        variants = raw.get('clusterInteriorOffers')
        if not isinstance(variants, list):
            raise ValueError('Oväntat variantformat från Coop.')
        variant_text = []
        for variant in variants:
            variant = require_mapping(variant, 'Coop variant')
            if (variant.get('priceInformation') != price or
                    str(variant.get('storeLedgerAccountNumber')) != config['id'] or
                    iso_day(variant.get('campaignStartDate')) != start or
                    iso_day(variant.get('campaignEndDate')) != end):
                raise ValueError('Coops grupperade sorter har olika villkor. Kontrollera butikens officiella sida.')
            details = require_mapping(variant.get('content'), 'Coop variant content')
            conditions.extend(plain_text(details.get(k)) for k in ('dealOfferLimitText', 'recyclingInformationText'))
            variant_text.append(' '.join(filter(None, [plain_text(details.get(k)) for k in ('title', 'brand', 'amountInformation')])))
        description = plain_text(content.get('description'))
        name = plain_text(content.get('title'))
        identity = raw.get('id')
        if not isinstance(identity, str) or not identity or identity in seen or not name:
            raise ValueError('Ogiltig eller upprepad erbjudandeidentitet från Coop.')
        seen.add(identity)
        offers.append(dict(name=name, brand=plain_text(content.get('brand')),
            volume=plain_text(content.get('amountInformation')), description=description,
            variants=list(dict.fromkeys(filter(None, variant_text))), search_extra=' '.join(variant_text),
            image=safe_image(content.get('imageUrl') or ''), price=label, ordinary='',
            member=member, member_label=config['member'], eligibility='LOYALTY' if member else 'GENERAL',
            conditions=list(dict.fromkeys(filter(None, conditions))), start=start.isoformat(), end=end.isoformat(),
            discount_percent=None, saving=None, saving_unit=''))
    return offers, len(records)


def fetch_coop(store_key):
    config, access = STORES[store_key], coop_access()
    url = access['dkeUrl'].rstrip('/') + '/sorting-groups/' + config['id'] + '?api-version=v3&clustered=true&grouped=true'
    with ThreadPoolExecutor(max_workers=2) as pool:
        identity = pool.submit(coop_store, store_key)
        response = pool.submit(public_json, url, {'Ocp-Apim-Subscription-Key': access['dkeKey']})
        store, data = identity.result(), response.result()
    now = dt.datetime.now(STOCKHOLM)
    offers, total = normalize_coop(data, config, now.date())
    return dict(store=config['name'], address=store['address'], source=config['source'],
        fetchedAt=now.isoformat(), today=now.date().isoformat(), sourceResults=total, offers=offers,
        note='Coop anger inget ordinarie pris i denna källa. Besparing och prissänkning beräknas därför inte. Grupperade sorter ingår i sökningen.')


@st.cache_data(ttl=300, max_entries=2, show_spinner=False, refresh_mode='background')
def lidl_campaigns(stockholm_day):
    # All three local branches share this public regional dataset, fetched once.
    homepage = read_public('https://www.lidl.se/')
    paths = list(dict.fromkeys(re.findall(r'href="(/c/store/[^"#?]+)"', homepage)))
    if not 1 <= len(paths) <= 60:
        raise ValueError('Lidls kampanjlista har ändrats.')
    for path in paths:
        if not re.fullmatch(r'/c/store/[a-z0-9-]+/a\d+', path):
            raise ValueError('Okänd kampanjadress från Lidl.')
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(read_public, ['https://www.lidl.se' + path for path in paths]))
    products = []
    for page in pages:
        items = re.findall(r'data-grid-data="([^"]+)"', page)
        if not items:
            raise ValueError('En Lidl-kampanj saknar läsbara produkter. Ingen delvis lista visas.')
        products.extend(json.loads(html.unescape(item)) for item in items)
    return dict(products=products, fetchedAt=dt.datetime.now(STOCKHOLM).isoformat())


def normalize_lidl(products, today):
    if not isinstance(products, list):
        raise ValueError('Lidls produktformat har ändrats.')
    offers, seen = [], set()
    for raw in products:
        raw = require_mapping(raw, 'Lidl product')
        # Store dates, not publication/price editing dates, determine availability.
        if type(raw.get('storeStartDate')) is not int or type(raw.get('storeEndDate')) is not int:
            raise ValueError('Lidls erbjudande saknar giltiga butiksdatum.')
        start = dt.datetime.fromtimestamp(raw['storeStartDate'], STOCKHOLM).date()
        end = dt.datetime.fromtimestamp(raw['storeEndDate'], STOCKHOLM).date()
        if not start <= today <= end:
            continue
        regions = require_mapping(raw.get('regionsV2'), 'Lidl regions')
        local = [require_mapping(v, 'Lidl region') for v in regions.values()
                 if isinstance(v, dict) and clean_text(v.get('regionName')).startswith('Halmstad ')]
        prices = require_mapping(raw.get('regionsPrices'), 'Lidl regional prices')
        if ({r.get('regionName') for r in local} != {'Halmstad 1', 'Halmstad 2', 'Halmstad 3'}
                or any(r.get('status') != 'ONLINE' for r in local)):
            raise ValueError('Lidls lokala utbud skiljer sig. Kontrollera butikens officiella sida.')
        regional = [require_mapping(prices.get(r.get('regionPriceId')), 'Lidl price') for r in local]
        if len({json.dumps(p, sort_keys=True) for p in regional}) != 1:
            # Never choose an arbitrary regional price for a branch.
            raise ValueError('Lidls priser skiljer sig mellan Halmstads regioner. Kontrollera butikens officiella sida.')
        region = regional[0]
        plus = region.get('currentLidlPlusPrice')
        price = require_mapping(require_mapping(plus, 'Lidl Plus').get('price') if plus is not None
                                else region.get('currentPrice'), 'Lidl price details')
        amount = price_amount(price.get('price'))
        if amount is None:
            raise ValueError('Lidl-erbjudandet saknar ett entydigt pris.')
        if type(raw.get('productId')) is not int or raw['productId'] <= 0:
            raise ValueError('Lidl-erbjudandet saknar produktidentitet.')
        identity = (raw['productId'], start, end, plus is not None, amount)
        if identity in seen:
            continue
        seen.add(identity)
        facts = require_mapping(raw.get('keyfacts'), 'Lidl product details')
        packaging = require_mapping(price.get('packaging') or {}, 'Lidl packaging')
        comparison = require_mapping(price.get('basePrice') or {}, 'Lidl comparison price')
        discount = require_mapping(price.get('discount') or {}, 'Lidl discount')
        volume = plain_text(packaging.get('text'))
        unit = 'kg' if re.fullmatch(r'(?:per\s*|/)kg', volume.casefold()) else 'prisenhet'
        old = price_amount(price.get('oldPrice'))
        saving, percent = None, None
        if (old is not None and old > amount and not price.get('variantsHaveDifferentPrices')
                and not discount.get('fromRecommendedPrice') and not discount.get('showUpTo')):
            saving, percent = float(old - amount), float((old - amount) / old * 100)
        conditions = [plain_text(price.get('priceLegendText')), plain_text(comparison.get('text')),
                      plain_text(facts.get('supplementalDescription'))]
        if plus is not None:
            conditions.append(plain_text(plus.get('lidlPlusText')))
        if price.get('variantsHaveDifferentPrices'):
            conditions.append('Priset kan variera mellan sorter.')
        offers.append(dict(name=plain_text(raw.get('fullTitle') or raw.get('title')), brand='', volume=volume,
            description=plain_text(facts.get('description')), image=safe_image(raw.get('image') or ''),
            price=money(amount) + ' kr' + ('/kg' if unit == 'kg' else ''),
            ordinary=money(old) + ' kr' if old is not None else '', ordinary_label='Visat referenspris',
            member=plus is not None, member_label='Lidl Plus', eligibility='LOYALTY' if plus is not None else 'GENERAL',
            conditions=list(dict.fromkeys(filter(None, conditions))), start=start.isoformat(), end=end.isoformat(),
            discount_percent=percent, saving=saving, saving_unit=unit))
    return offers


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def lidl_store(store_key):
    config = STORES[store_key]
    page = read_public(config['source'])
    stores = []
    for text in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        data = json.loads(text)
        if isinstance(data, dict) and data.get('@type') == 'GroceryStore':
            stores.append(data)
    if len(stores) != 1:
        raise ValueError('Lidl-butikens identitet kunde inte bekräftas.')
    address = require_mapping(stores[0].get('address'), 'Lidl store address')
    if (search_key(address.get('streetAddress', '')) != search_key(config['address'])
            or search_key(address.get('addressLocality', '')) != 'halmstad'):
        raise ValueError('Lidls butikssida visar en annan butik.')
    return config['name']


def fetch_lidl(store_key):
    config = STORES[store_key]
    now = dt.datetime.now(STOCKHOLM)
    with ThreadPoolExecutor(max_workers=2) as pool:
        store_future = pool.submit(lidl_store, store_key)
        offers_future = pool.submit(lidl_campaigns, now.date().isoformat())
        store_future.result()
        data = offers_future.result()
    return dict(store=config['name'], address=config['address'], source=config['source'],
        fetchedAt=data['fetchedAt'], today=now.date().isoformat(), sourceResults=len(data['products']),
        offers=normalize_lidl(data['products'], now.date()),
        note='Lidls publicerade kampanjer med samma pris i samtliga tre Halmstadsregioner. Lokalt lagersaldo ingår inte. Referenspriser visas enligt Lidl; alla kampanjvaror har inte ett tidigare pris.')

def get_json(path, params=None, base=BASE):
    url = base + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        'Accept': 'application/json', 'User-Agent': 'FoodSale/2.0 (public offers viewer)'})
    with urllib.request.urlopen(request, timeout=25) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError('The store returned an unexpected response format.')
    return value



def require_mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'Unexpected {label} format from the store.')
    return value


def validate_product(product):
    product = require_mapping(product, 'product')
    for field in ('name', 'manufacturer', 'displayVolume', 'price', 'priceUnit'):
        if product.get(field) is not None and not isinstance(product[field], str):
            raise ValueError(f'Unexpected product {field} format from the store.')
    for field in ('image', 'thumbnail'):
        image = product.get(field)
        if image is not None:
            require_mapping(image, 'image')
            if image.get('url') is not None and not isinstance(image['url'], str):
                raise ValueError('Unexpected image URL format from the store.')
    promotions = product.get('potentialPromotions')
    if promotions is None:
        return []
    if not isinstance(promotions, list):
        raise ValueError('Unexpected promotions format from the store.')
    for promotion in promotions:
        require_mapping(promotion, 'promotion')
        for field in ('startDate', 'endDate', 'code', 'name', 'rewardLabel', 'cartLabel',
                      'conditionLabel', 'description', 'campaignType', 'weightVolume',
                      'textLabelManual', 'textLabel', 'textLabelGenerated',
                      'splashTitleText', 'redeemLimitLabel', 'comparePrice'):
            if promotion.get(field) is not None and not isinstance(promotion[field], str):
                raise ValueError(f'Unexpected promotion {field} format from the store.')
    return promotions

@lru_cache(maxsize=512)
def parse_date(value):
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, '%d/%m-%Y').date()
    except (ValueError, TypeError):
        raise ValueError('The store changed its offer date format; update this viewer.')


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
        raise ValueError('Unexpected results format from the store.')
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


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def store_information(store_key='willys-oster'):
    config = STORES[store_key]
    return get_json('/axfood/rest/v2/store/' + config['id'], base=config['base'])


def fetch_offers(store_key='willys-oster'):
    config = STORES[store_key]
    store_id, base = config['id'], config['base']
    # Store identity and the first offers page are independent; overlap their I/O.
    params = dict(q=store_id, type='PERSONAL_GENERAL', page=0, size=200)
    with ThreadPoolExecutor(max_workers=2) as pool:
        store_future = pool.submit(store_information, store_key)
        page_future = pool.submit(get_json, '/axfood/rest/v2/search/campaigns/offline', params, base)
        store, first_page = store_future.result(), page_future.result()
    require_mapping(store, 'store')
    address = require_mapping(store['address'] if store.get('address') is not None else {}, 'store address')
    if store.get('storeId') != store_id or store.get('name') != config['name']:
        raise ValueError('Store identity could not be confirmed; no offers displayed.')
    results = []
    expected_total = None
    expected_pages = None
    for page in range(100):
        data = first_page if page == 0 else get_json('/axfood/rest/v2/search/campaigns/offline',
                        dict(q=store_id, type='PERSONAL_GENERAL', page=page, size=200), base=base)
        require_mapping(data, 'offers response')
        batch = data.get('results')
        pagination = require_mapping(data.get('pagination'), 'pagination')
        pages = pagination.get('numberOfPages')
        total = pagination.get('totalNumberOfResults')
        current_page = pagination.get('currentPage')
        if (not isinstance(batch, list) or type(pages) is not int or not 0 <= pages <= 100
                or type(total) is not int or total < 0 or type(current_page) is not int
                or (pages == 0 and (batch or total))):
            raise ValueError('The store changed the offers response; update this viewer.')
        if pagination.get('currentPage') != page:
            raise ValueError('The store returned the wrong results page.')
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
                source=config['source'], fetchedAt=now.isoformat(), today=now.date().isoformat(),
                sourceResults=len(results), offers=[dict(offer, member_label=config['member']) for offer in normalize(results, now.date())])


@st.cache_data(ttl=300, max_entries=24, show_spinner=False, refresh_mode="background")
def cached_offers(stockholm_day, store_key='willys-oster', schema_version=4):
    # Public-data cache isolates providers and branches. The date bounds rollover.
    provider = STORES[store_key]['provider']
    if provider == 'axfood':
        return fetch_offers(store_key)
    if provider == 'coop':
        return fetch_coop(store_key)
    raise ValueError('Produktlistan är inte tillgänglig för denna butik.')


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
        if mode in ('Willys Plus', 'Medlemspriser') and not offer['member']:
            continue
        if mode == 'Utan medlemskrav' and offer['eligibility'] != 'GENERAL':
            continue
        days = (end - today).days
        if expiry == 'Slutar idag' and days != 0:
            continue
        if expiry == 'Inom 3 dagar' and days > 3:
            continue
        haystack = search_key(' '.join(offer[k] for k in ('name', 'brand', 'description', 'volume')) + ' ' + offer.get('search_extra', ''))
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
    membership = '<span class="badge plus">' + escape(offer.get('member_label', 'Willys Plus')) + '</span>' if offer['member'] else '<span class="badge general">Alla kunder</span>'
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
    ordinary = '<p class="ordinary">' + escape(offer.get('ordinary_label', 'Ordinarie')) + ' ' + escape(offer['ordinary']) + '</p>' if offer['ordinary'] else ''
    conditions = ''.join('<div>' + escape(v) + '</div>' for v in offer['conditions'])
    variants = offer.get('variants', [])
    if variants:
        conditions += '<details><summary>Visa ' + str(len(variants)) + ' sorter</summary>' + ''.join(
            '<div>' + escape(v) + '</div>' for v in variants) + '</details>'
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
    st.set_page_config(page_title='Food Sale · Halmstad', page_icon='🛒', layout='wide')
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
    default_store = st.query_params.get('store', 'willys-oster')
    if default_store not in STORES:
        default_store = 'willys-oster'
    choices = list(STORES)
    store_key = st.selectbox('Välj butik', choices, index=choices.index(default_store),
        format_func=lambda key: STORES[key]['name'] + (' · Officiell sida' if STORES[key]['provider'] == 'official' else ''),
        key='store', help='12 butiker har produktlistor här. ICA, City Gross och Tempo öppnas på sina officiella erbjudandesidor.')
    if st.query_params.get('store') != store_key:
        st.query_params['store'] = store_key
    config = STORES[store_key]
    st.html('<section class="hero"><h1>Veckans fynd, nära dig.</h1><p>' +
            escape(config['name']) + ' · ' + escape(config['address']) + '</p></section>')
    st.caption('12 butiker med produktvy · 7 butiker via officiell erbjudandesida')
    if config['provider'] == 'official':
        st.info('Den här butikens erbjudanden kan ännu inte visas som en produktlista här. '
                'Öppna butikens officiella erbjudanden nedan. Sökning och sortering i Food Sale är inte tillgängliga för denna butik.')
    else:
        offers_view(store_key)
    st.html('<div class="footnote">Offentliga butikserbjudanden. Medlemspriser kräver medlemskap hos respektive kedja. '
            'Personliga kuponger och lokalt lagersaldo ingår inte. Bilder hämtas från butikernas bildleverantörer. Fristående visare.</div>')
    st.link_button('Se butikens officiella erbjudanden ↗', config['source'])


@st.fragment(run_every=60)
def offers_view(store_key='willys-oster'):
    config = STORES[store_key]
    today = dt.datetime.now(STOCKHOLM).date()
    try:
        with st.spinner('Hämtar butikens erbjudanden…'):
            result = fetch_lidl(store_key) if config['provider'] == 'lidl' else cached_offers(today.isoformat(), store_key)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, OverflowError):
        st.error('Erbjudandena kunde inte hämtas. Kontrollera anslutningen eller försök igen. Inga gamla priser visas.')
        if st.button('Försök igen'):
            cached_offers.clear(today.isoformat(), store_key)
            if config['provider'] == 'lidl':
                lidl_campaigns.clear(today.isoformat())
            st.rerun()
        return
    fetched_at = dt.datetime.fromisoformat(result['fetchedAt']).astimezone(STOCKHOLM)
    age_seconds = max(0, (dt.datetime.now(STOCKHOLM) - fetched_at).total_seconds())
    if age_seconds > 300:
        st.warning('Visar sparade erbjudanden från ' + fetched_at.strftime('%H:%M') +
                   '. Hämtar aktuella priser i bakgrunden. Om uppdateringen misslyckas kan priserna ha ändrats.')
    if result.get('note'):
        st.caption(result['note'])
    # Cache age and validity are separate: discard expired offers even within a cached response.
    current = select_offers(result['offers'], today=today)
    members = sum(o['member'] for o in current)
    ending = sum(o['end'] == today.isoformat() for o in current)
    st.html('<div class="summary">' + ''.join(
        f'<div class="summary-item"><strong>{number}</strong><span>{label}</span></div>'
        for number, label in [(len(current), 'aktuella erbjudanden'), (members, 'medlemspriser'), (ending, 'slutar idag')]) + '</div>')
    search_col, member_col = st.columns([2, 1], vertical_alignment='bottom')
    with search_col:
        query = st.text_input('Sök produkter', placeholder='Sök vara, varumärke eller storlek…', key='search')
    with member_col:
        mode = st.selectbox('Medlemskrav', ['Alla erbjudanden', 'Utan medlemskrav', 'Medlemspriser'], key='membership')
    expiry_col, order_col, refresh_col = st.columns([1.3, 1.3, 1], vertical_alignment='bottom')
    with expiry_col:
        expiry = st.selectbox('Giltighet', ['Alla slutdatum', 'Slutar idag', 'Inom 3 dagar'], key='expiry')
    with order_col:
        order = st.selectbox('Sortera', ['Butikens ordning', 'Störst prissänkning (%)', 'Störst besparing (kr/enhet)', 'Slutar snart', 'Namn A–Ö'], key='sort',
                             help='Rabatten beräknas mot visat ordinarie pris. Flerköp räknas per vara; viktpriser per kg. Sorter kan skilja sig. Okända jämförelser visas sist.')
    with refresh_col:
        if st.button('↻ Uppdatera', use_container_width=True):
            cached_offers.clear(today.isoformat(), store_key)
            if config['provider'] == 'lidl':
                lidl_campaigns.clear(today.isoformat())
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
    filter_signature = (store_key, query, mode, expiry, order)
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
