"""Food Sale · FastAPI. Run: python app.py (or uvicorn app:app).
All application code, styles and scripts live in this file.
"""
import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache, wraps
from html.parser import HTMLParser
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from rapidfuzz import process
from rapidfuzz.distance import OSA

# All caches contain public data only and are private to this server process.
# In-memory state disappears on restart; run one worker to avoid duplicate caches.
class ExtractionUnavailable(Exception):
    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def cached_metadata(ttl=86400, max_entries=8):
    """Single-flight metadata TTL cache, loaded on its caller's worker thread."""
    def decorate(fn):
        lock, values, pending, failures = threading.RLock(), OrderedDict(), {}, {}
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            with lock:
                if key in values and time.monotonic() - values[key][0] < ttl:
                    values.move_to_end(key)
                    return values[key][1]
                if failures.get(key, 0) > time.monotonic():
                    raise ExtractionUnavailable('Butikens uppgifter kunde inte hämtas. Försök igen om en stund.')
                owner = key not in pending
                future = pending.setdefault(key, Future())
            if not owner:
                return future.result(timeout=90)
            try:
                value = fn(*args, **kwargs)
                with lock:
                    failures.pop(key, None)
                    values[key] = (time.monotonic(), value)
                    values.move_to_end(key)
                    while len(values) > max_entries:
                        values.popitem(last=False)
                future.set_result(value)
                return value
            except Exception as exc:
                with lock:
                    failures[key] = time.monotonic() + max(30, getattr(exc, 'source_retry_after', 0))
                future.set_exception(exc)
                raise
            finally:
                with lock:
                    pending.pop(key, None)
        def clear(*args, **kwargs):
            with lock:
                if args or kwargs:
                    key = (args, tuple(sorted(kwargs.items())))
                    values.pop(key, None)
                    failures.pop(key, None)
                else:
                    values.clear()
                    failures.clear()
        wrapper.clear = clear
        return wrapper
    return decorate


@dataclass
class CacheEntry:
    value: dict | None = None
    saved: float = 0
    future: Future | None = None
    retry_at: float = 0
    failed: bool = False


@dataclass(frozen=True)
class Snapshot:
    value: dict
    stale: bool
    refreshing: bool
    failed: bool


class OfferCache:
    """Bounded, single-flight stale-while-revalidate cache with a hard age cap."""
    def __init__(self, ttl=300, stale_for=300, cooldown=30, workers=3):
        self.ttl, self.stale_for, self.cooldown = ttl, stale_for, cooldown
        self.lock = threading.RLock()
        self.entries = OrderedDict()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='offers')

    def _load(self, entry, loader):
        try:
            value = loader()
            with self.lock:
                entry.value, entry.saved = value, time.monotonic()
                entry.failed, entry.retry_at = False, 0
            return value
        except Exception as exc:
            # Neither failure nor retry advances the successful snapshot's timestamp.
            with self.lock:
                entry.failed, entry.retry_at = True, time.monotonic() + max(self.cooldown, getattr(exc, 'source_retry_after', 0))
            raise ExtractionUnavailable('Butikens erbjudanden kunde inte hämtas. Försök igen om en stund.', max(self.cooldown, entry.retry_at - time.monotonic())) from None

    def get(self, key, loader, force=False):
        with self.lock:
            now = time.monotonic()
            entry = self.entries.setdefault(key, CacheEntry())
            self.entries.move_to_end(key)
            # Keys come only from the fixed catalogue and the server's current date.
            while len(self.entries) > 32:
                evict = next((k for k, v in self.entries.items()
                              if k != key and (v.future is None or v.future.done())), None)
                if evict is None:
                    break
                self.entries.pop(evict)
            age = now - entry.saved
            running = entry.future is not None and not entry.future.done()
            allowed = entry.value is not None and age < self.ttl + self.stale_for
            if entry.value is not None and age < self.ttl and (not force or age < self.cooldown):
                return Snapshot(entry.value, False, running, entry.failed)
            if not running and now >= entry.retry_at:
                entry.future = self.pool.submit(self._load, entry, loader)
                running = True
            if allowed:
                return Snapshot(entry.value, age >= self.ttl, running, entry.failed)
            if not running:
                raise ExtractionUnavailable('Butikens erbjudanden kunde inte hämtas. Försök igen om en stund.', max(1, entry.retry_at - now))
            future = entry.future
        try:
            value = future.result(timeout=95)
            return Snapshot(value, False, False, False)
        except ExtractionUnavailable:
            raise
        except TimeoutError:
            raise ExtractionUnavailable('Butikens erbjudanden kunde inte hämtas. Försök igen om en stund.') from None

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


offer_cache = OfferCache()
# Slow Chromium work has its own queue so it cannot occupy the HTTP-source workers.
ica_offer_cache = OfferCache(workers=1)


BASE = "https://www.willys.se"


STORE = "2236"


SOURCE = BASE + "/erbjudanden/butik?StoreID=" + STORE


STOCKHOLM = ZoneInfo("Europe/Stockholm")


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
    'ica-hogskolan': dict(name='Maxi ICA Stormarknad Högskolan', provider='ica', id='1003925', bms='2308', member='Stammis', address='Halmstad', source='https://www.ica.se/erbjudanden/maxi-ica-stormarknad-hogskolan-1003925/'),
    'ica-flygstaden': dict(name='Maxi ICA Stormarknad Flygstaden', provider='ica', id='1003781', bms='11066', member='Stammis', address='Halmstad', source='https://www.ica.se/erbjudanden/maxi-ica-stormarknad-flygstaden-1003781/'),
    'ica-vallas': dict(name='ICA Supermarket Vallås', provider='ica', id='1003811', bms='2311', member='Stammis', address='Halmstad', source='https://www.ica.se/erbjudanden/ica-supermarket-vallas-1003811/'),
    'ica-sondrum': dict(name='ICA Supermarket Söndrum', provider='ica', id='1003648', bms='2309', member='Stammis', address='Halmstad', source='https://www.ica.se/erbjudanden/ica-supermarket-sondrum-1003648/'),
    'ica-frosakull': dict(name='ICA Nära Frösakull', provider='ica', id='1004064', bms='11590', member='Stammis', address='Halmstad', source='https://www.ica.se/erbjudanden/ica-nara-frosakull-1004064/'),
    'citygross': dict(name='City Gross Halmstad', provider='citygross', id='3210', site='22', category='2930', member='PRIO', address='Stormgatan 14', source='https://www.citygross.se/butiker/halmstad'),
    'tempo-backagard': dict(name='Tempo Halmstad Bäckagård', provider='official', address='Akvarellgränd 1', source='https://www.tempo.se/butiker/tempo-halmstad-backagard/'),
}


def normalize_city_leaflets(rows, today):
    """Validate branch-specific leaflet links; never infer individual product prices."""
    if not isinstance(rows, list) or len(rows) > 50:
        raise ValueError('Oväntat reklambladsformat.')
    result, seen = [], set()
    for row in rows:
        require_mapping(row, 'reklamblad')
        if str(row.get('store')) != '3210':
            raise ValueError('Reklambladet tillhör en annan butik.')
        start, end = iso_day(row.get('availableFrom')), iso_day(row.get('availableTo'))
        if start > end:
            raise ValueError('Ogiltiga datum för reklambladet.')
        if not start <= today <= end:
            continue
        link = row.get('link', '')
        if not isinstance(link, str) or not re.fullmatch(r'https://online\.flippingbook\.com/view/[0-9]+/?', link):
            raise ValueError('Oväntad reklambladslänk.')
        title = plain_text(row.get('title'))
        if not title:
            raise ValueError('Reklambladet saknar titel.')
        if link not in seen:
            result.append(dict(title=title, url=link, start=start.isoformat(), end=end.isoformat(),
                               description=plain_text(row.get('subTitle'))))
            seen.add(link)
    return result


def fetch_city_leaflets():
    now = dt.datetime.now(STOCKHOLM)
    rows = public_json('https://www.citygross.se/api/v1/leaflets?store=3210')
    return dict(fetchedAt=now.isoformat(), leaflets=normalize_city_leaflets(rows, now.date()))


# Chromium is short-lived and only one ICA fetch may own it per server process.
ICA_BROWSER_LOCK = threading.Lock()
ICA_ROBOTS = None
ICA_ROBOTS_SAVED = 0


def ica_json_field(script, name):
    """Decode an embedded array/object as data, without eval or executing source text."""
    match = re.search(r'"' + re.escape(name) + r'"\s*:\s*([\[{])', script)
    if not match:
        raise ValueError('ICA saknar förväntade uppgifter.')
    start, depth, quoted, escaped = match.end() - 1, 0, False, False
    for index in range(start, len(script)):
        char = script[index]
        if escaped:
            escaped = False
            continue
        if quoted and char == '\\':
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
        if not quoted:
            if char in '[{':
                depth += 1
            elif char in ']}':
                depth -= 1
                if depth == 0:
                    raw = script[start:index + 1]
                    # Initial state uses JS undefined; never change it inside strings.
                    raw = re.sub(r'"(?:\\.|[^"\\])*"|\bundefined\b',
                                 lambda token: 'null' if token.group() == 'undefined' else token.group(), raw)
                    return json.loads(raw)
    raise ValueError('ICA skickade ofullständiga uppgifter.')


class IcaPageText(HTMLParser):
    """Read server-rendered offer counts without running page scripts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def ica_page_data(source, body, config):
    if len(source) > 12_000_000:
        raise ValueError('ICA:s svar är för stort.')
    match = re.search(r'<script(?:\s[^>]*)?>\s*window\.__INITIAL_DATA__\s*=\s*(.*?)</script\s*>', source, re.S)
    if not match:
        raise ValueError('ICA:s produktdata saknas.')
    script = match.group(1)
    store = require_mapping(ica_json_field(script, 'storeInfoModel'), 'ICA store')
    if (store.get('accountNumber') != config['id'] or store.get('storeName') != config['name']
            or str(store.get('storeId')) != config['bms']):
        raise ValueError('ICA svarade med fel butik.')
    rows = ica_json_field(script, 'weeklyOffers')
    if body is None:
        markup = IcaPageText()
        markup.feed(source)
        markup.close()
        body = ' '.join(markup.parts)
    counts = re.findall(r'Visar\s+(\d+)\s+(?:stycken|erbjudanden)', body)
    if (not isinstance(rows, list) or len(rows) > 2000 or not counts
            or any(int(count) != len(rows) for count in counts)):
        raise ValueError('ICA:s produktlista kunde inte bekräftas som komplett.')
    return rows


def ica_discount(row, store, price):
    """Use unambiguous priced units only; never use the lowest price of a range."""
    unknown = dict(discount_percent=None, saving=None, saving_unit='')
    mechanics = require_mapping(row.get('parsedMechanics'), 'ICA price mechanics')
    if mechanics.get('benefitType') == 'PERCOFF':
        percent = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*%\s*rabatt', price, re.I)
        if percent and 0 < Decimal(percent.group(1).replace(',', '.')) <= 100:
            return dict(unknown, discount_percent=float(percent.group(1).replace(',', '.')))
        return unknown
    if mechanics.get('benefitType') != 'FIXED':
        return unknown
    single = re.fullmatch(r'(\d+(?:[.,:]\d{1,2})?)\s+kr/(st|kg|l|liter)(?:\s*\+\s*pant)?', price, re.I)
    bundle = re.fullmatch(r'(\d+)\s+för\s+(\d+(?:[.,:]\d{1,2})?)\s+kr(?:\s*\+\s*pant)?', price, re.I)
    if single:
        unit = 'l' if single.group(2).lower() == 'liter' else single.group(2).lower()
        amount = price_amount(single.group(1))
    elif (bundle and int(bundle.group(1)) > 0 and type(mechanics.get('quantity')) is int
          and mechanics['quantity'] == int(bundle.group(1))):
        unit, amount = 'st', price_amount(bundle.group(2)) / Decimal(bundle.group(1))
    else:
        return unknown
    ordinary = price_amount(store.get('regularPrice'))
    reference = plain_text(store.get('referencePriceText'))
    explicit_units = re.findall(r'/\s*(st|kg|l|liter)\b', reference.casefold())
    if ordinary is None or ordinary <= 0 or any(('l' if x == 'liter' else x) != unit for x in explicit_units):
        return unknown
    saving = max(Decimal(0), ordinary - amount)
    return dict(discount_percent=float(saving / ordinary * 100), saving=float(saving), saving_unit=unit)


def normalize_ica(rows, config, today):
    if not isinstance(rows, list):
        raise ValueError('Oväntat ICA-format.')
    offers, seen = [], set()
    for row in rows:
        require_mapping(row, 'ICA offer')
        identity = row.get('id')
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError('ICA skickade dubbla eller ogiltiga erbjudanden.')
        seen.add(identity)
        details = require_mapping(row.get('details'), 'ICA details')
        stores = row.get('stores')
        if not isinstance(stores, list) or len(stores) != 1:
            raise ValueError('ICA:s butikspriser kunde inte bekräftas.')
        store = require_mapping(stores[0], 'ICA offer store')
        if store.get('storeMarketingName') != config['name'] or str(store.get('BMSStoreId')) != config['bms']:
            raise ValueError('ICA skickade priser för fel butik.')
        if type(store.get('storeInd')) is not bool:
            raise ValueError('ICA saknar uppgift om erbjudandet gäller i butik.')
        end = iso_day(row.get('validTo'))
        if not store['storeInd'] or end < today:
            continue
        # The public current-offer page establishes observation today, not a start date.
        start = iso_day(row['validFrom']).isoformat() if row.get('validFrom') else None
        if start and dt.date.fromisoformat(start) > today:
            continue
        name, price = plain_text(details.get('name')), plain_text(details.get('mechanicInfo'))
        if not name or not price:
            raise ValueError('ICA saknar produktnamn eller visningspris.')
        traits = row.get('traits')
        if not isinstance(traits, list) or any(not isinstance(t, str) for t in traits):
            raise ValueError('ICA:s medlemsvillkor kunde inte bekräftas.')
        member = any(t.casefold() == 'stammis' for t in traits)
        self_scan = details.get('isSelfScan')
        if type(self_scan) is not bool:
            raise ValueError('ICA:s behörighetsvillkor saknas.')
        conditions = []
        for text in [row.get('condition'), row.get('restriction'), details.get('disclaimer'),
                     row.get('comparisonPrice'), store.get('ecomDisclaimer')]:
            value = plain_text(text)
            if value and value not in conditions:
                conditions.append(value)
        if self_scan:
            conditions.append('Gäller vid självscanning. Se butikens villkor.')
        for trait in traits:
            if trait.casefold() not in ('stammis', 'butiksunikt') and trait not in conditions:
                conditions.append(trait)
        eans = row.get('eans')
        if not isinstance(eans, list):
            raise ValueError('ICA:s sortinformation är ogiltig.')
        variants, image = [], ''
        for ean in eans:
            require_mapping(ean, 'ICA variety')
            text = plain_text(ean.get('articleDescription'))
            if text and text not in variants:
                variants.append(text)
            if not image and ean.get('image'):
                image = safe_image(ean['image'])
        if not image:
            picture = require_mapping(row.get('picture'), 'ICA image')
            # Plain publication IDs only; unresolved Cloudinary templates are not URLs.
            filename = picture.get('fileName')
            if isinstance(filename, str) and re.fullmatch(r'[a-zA-Z0-9_-]+', filename):
                image = 'https://assets.icanet.se/c_fit,w_300,h_300/' + filename
        ordinary = plain_text(store.get('regularPrice'))
        reference = plain_text(store.get('referencePriceText'))
        if reference:
            conditions.append(reference)
        offers.append(dict(name=name, brand=plain_text(details.get('brand')),
            volume=plain_text(details.get('packageInformation')), description=plain_text(details.get('customerInformation')),
            image=image, price=price, ordinary=ordinary, member=member, member_label='Stammis',
            eligibility='Självscanning' if self_scan else ('LOYALTY' if member else 'GENERAL'),
            conditions=conditions, variants=variants if len(variants) > 1 else [],
            search_extra=' '.join(variants), start=start, end=end.isoformat(), observed=today.isoformat(),
            comparison=dict(kind='gtin', familyVariants=[dict(name=plain_text(e.get('articleDescription')) or name, brand=plain_text(details.get('brand')), volume=label_pack_size(plain_text(e.get('articleDescription')))) for e in eans], variants=[compare_variant(e['id'], ' · '.join(filter(None, [plain_text(e.get('articleDescription')) or name, plain_text(details.get('brand'))]))) for e in eans if valid_gtin(e.get('id'))]),
            **ica_discount(row, store, price)))
    return offers


def ica_require_success(response):
    if response is not None and response.status == 200:
        return
    delay = 60
    if response is not None:
        retry = response.headers.get('retry-after', '')
        try:
            delay = max(delay, int(retry))
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                when = parsedate_to_datetime(retry)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
                delay = max(delay, int((when - dt.datetime.now(dt.timezone.utc)).total_seconds()) + 1)
            except (ValueError, TypeError, OverflowError):
                pass
    error = ExtractionUnavailable('ICA:s erbjudanden är inte tillgängliga.', delay)
    error.source_retry_after = delay
    raise error


def fetch_ica(store_key):
    """One ordinary, resource-limited browser; no stealth, login or challenge solving."""
    global ICA_ROBOTS, ICA_ROBOTS_SAVED
    config = STORES[store_key]
    if not ICA_BROWSER_LOCK.acquire(timeout=40):
        raise ExtractionUnavailable('ICA hämtas redan. Försök igen om en stund.')
    try:
        from playwright.sync_api import sync_playwright
        from urllib.robotparser import RobotFileParser
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(timeout=15000)
            try:
                context = browser.new_context(service_workers='block', java_script_enabled=False)
                # Initial offer state and markup are inline. No product-image/analytics downloads.
                context.route('**/*', lambda route: route.continue_() if (
                    route.request.is_navigation_request() and route.request.frame == route.request.frame.page.main_frame
                    and urllib.parse.urlsplit(route.request.url).scheme == 'https'
                    and urllib.parse.urlsplit(route.request.url).netloc == 'www.ica.se') else route.abort())
                page = context.new_page()
                if ICA_ROBOTS is None or time.monotonic() - ICA_ROBOTS_SAVED >= 86400:
                    response = page.goto('https://www.ica.se/robots.txt', wait_until='domcontentloaded', timeout=20000)
                    ica_require_success(response)
                    robots = RobotFileParser()
                    robots.parse(response.text().splitlines())
                    ICA_ROBOTS, ICA_ROBOTS_SAVED = robots, time.monotonic()
                if not ICA_ROBOTS.can_fetch('FoodSale', config['source']):
                    raise ExtractionUnavailable('ICA tillåter inte denna hämtning.')
                response = page.goto(config['source'], wait_until='domcontentloaded', timeout=20000)
                ica_require_success(response)
                if page.url.rstrip('/') != config['source'].rstrip('/'):
                    raise ValueError('ICA öppnade en annan sida.')
                rows = ica_page_data(response.text(), None, config)
            finally:
                browser.close()
        now = dt.datetime.now(STOCKHOLM)
        return dict(store=config['name'], address=config['address'], source=config['source'],
                    fetchedAt=now.isoformat(), today=now.date().isoformat(), sourceResults=len(rows),
                    offers=normalize_ica(rows, config, now.date()),
                    note='ICA:s aktuella butikserbjudanden. Startdatum saknas hos källan; endast slutdatum visas. Personliga kuponger ingår inte.')
    finally:
        ICA_BROWSER_LOCK.release()


@cached_metadata(ttl=86400, max_entries=1)
def citygross_metadata():
    from urllib.robotparser import RobotFileParser
    base = 'https://www.citygross.se/api/v1/'
    rules = RobotFileParser()
    rules.parse(read_public('https://www.citygross.se/robots.txt').splitlines())
    paths = ('sites', 'PageData/2930', 'Loop54/category/2930/products')
    if not all(rules.can_fetch('FoodSale', base + path) for path in paths):
        raise ValueError('City Gross tillåter inte denna hämtning.')
    sites = require_mapping(public_json(base + 'sites'), 'City Gross stores').get('sites')
    if not isinstance(sites, list):
        raise ValueError('City Gross butiker kunde inte bekräftas.')
    matches = [site for site in sites if isinstance(site, dict) and site.get('id') == 22]
    if (len(matches) != 1 or matches[0].get('name') != 'City Gross Halmstad'
            or str(matches[0].get('storeNumber')) != '3210' or matches[0].get('city') != 'Halmstad'):
        raise ValueError('City Gross svarade med fel butik.')
    category = require_mapping(public_json(base + 'PageData/2930'), 'City Gross category')
    page = require_mapping(category.get('data'), 'City Gross category data')
    if (page.get('id') != 2930 or page.get('type') != 'ProductCategoryPage'
            or page.get('name') != 'Veckans erbjudanden' or page.get('ispublished') is not True):
        raise ValueError('City Gross kategori för veckoerbjudanden har ändrats.')
    return matches[0]


def normalize_citygross(rows, today):
    if not isinstance(rows, list):
        raise ValueError('Oväntat produktformat från City Gross.')
    result, seen = [], set()
    units = {'PCE': 'st', 'KGM': 'kg', 'LTR': 'l'}
    for product in rows:
        require_mapping(product, 'City Gross product')
        identity = product.get('id')
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError('City Gross skickade dubbla eller ogiltiga produkter.')
        seen.add(identity)
        store = require_mapping(product.get('productStoreDetails'), 'City Gross store prices')
        if str(store.get('id')) != '3210' or store.get('p_has_current_week_only_discount') is not True:
            raise ValueError('City Gross skickade fel butik eller kampanjkategori.')
        prices = require_mapping(store.get('prices'), 'City Gross prices')
        promotion = require_mapping(prices.get('activePromotion'), 'City Gross active promotion')
        promotions = prices.get('promotions')
        if not isinstance(promotions, list) or promotion not in promotions:
            raise ValueError('City Gross aktiva kampanj kunde inte bekräftas.')
        start, end = iso_day(promotion.get('from')), iso_day(promotion.get('to'))
        if start > end:
            raise ValueError('City Gross skickade ogiltiga kampanjdatum.')
        if not start <= today <= end:
            continue
        member = promotion.get('membersOnly')
        quantity = promotion.get('minQuantity')
        limit = promotion.get('maxAppliedPerReceipt')
        if (type(member) is not bool or type(quantity) is not int or quantity < 1
                or (limit is not None and (type(limit) is not int or limit < 0))):
            raise ValueError('City Gross kampanjvillkor kunde inte bekräftas.')
        details = require_mapping(promotion.get('priceDetails'), 'City Gross promotion price')
        ordinary = require_mapping(prices.get('ordinaryPrice'), 'City Gross ordinary price')
        unit = units.get(details.get('unit'))
        ordinary_unit = units.get(ordinary.get('unit'))
        value, each, reference = (price_amount(promotion.get('value')),
                                  price_amount(details.get('price')), price_amount(ordinary.get('price')))
        if not unit or not ordinary_unit or value is None or each is None or reference is None or min(value, each, reference) < 0:
            raise ValueError('City Gross skickade ett ogiltigt pris eller enhet.')
        effect = promotion.get('effectType')
        if effect == 'ItemsTotal' and unit == 'st':
            price = (f'{quantity} för {money(value)} kr' if quantity > 1 else f'{money(value)} kr/st')
            paid_each = value / quantity
        elif effect == 'PriceChangeAll' and quantity == 1:
            price, paid_each = f'{money(value)} kr/{unit}', value
        else:
            raise ValueError('City Gross kampanjtyp stöds ännu inte.')
        deposit = product.get('returnablePackageDepositInformation')
        if deposit is not None:
            require_mapping(deposit, 'City Gross deposit')
        has_deposit = deposit is not None and deposit.get('description') == 'Pant'
        if has_deposit:
            price += ' +pant'
        conditions = []
        if limit:
            conditions.append(f'Max {limit} erbjudandeköp per kvitto.')
        minimum = promotion.get('minAmount')
        if minimum is not None:
            minimum = price_amount(minimum)
            if minimum is None or minimum < 0:
                raise ValueError('City Gross minsta köpesumma är ogiltig.')
            if minimum:
                conditions.append('Vid köp för minst ' + money(minimum) + ' kr.')
        coupon = promotion.get('couponId')
        if coupon:
            conditions.append('Kupong krävs hos City Gross.')
        comparison = price_amount(details.get('comparativePrice'))
        comparison_unit = units.get(details.get('comparativePriceUnit'))
        if comparison is not None and comparison_unit:
            conditions.append('Jämförpris ' + money(comparison) + ' kr/' + comparison_unit)
        lowest = prices.get('lowestPriceLast30Days')
        if lowest is not None:
            if isinstance(lowest, dict):
                lowest_value, lowest_unit = price_amount(lowest.get('price')), units.get(lowest.get('unit'))
            else:
                lowest_value, lowest_unit = price_amount(lowest), ordinary_unit
            if lowest_value is not None and lowest_unit:
                conditions.append('Lägsta pris 30 dagar: ' + money(lowest_value) + ' kr/' + lowest_unit)
        metrics = dict(discount_percent=None, saving=None, saving_unit='')
        if (unit == ordinary_unit and reference > 0 and not minimum and not coupon
                and abs(paid_each - each) <= Decimal('0.011')):
            saving = max(Decimal(0), reference - paid_each)
            metrics = dict(discount_percent=float(saving / reference * 100), saving=float(saving), saving_unit=unit)
        name = plain_text(product.get('name'))
        if not name:
            raise ValueError('City Gross produkten saknar namn.')
        images = product.get('images') or []
        if not isinstance(images, list):
            raise ValueError('City Gross bilder har oväntat format.')
        image = ''
        for candidate in images:
            require_mapping(candidate, 'City Gross image')
            path = candidate.get('url')
            if path:
                if not isinstance(path, str):
                    raise ValueError('City Gross bildlänk är ogiltig.')
                image = safe_image(path) if urllib.parse.urlsplit(path).scheme else (
                    'https://www.citygross.se/images/products/' + urllib.parse.quote(path, safe='/') + '?w=300&h=300')
                break
        keywords = product.get('marketingKeyWords') or []
        if not isinstance(keywords, list) or any(not isinstance(word, str) for word in keywords):
            raise ValueError('City Gross sökord har oväntat format.')
        result.append(dict(name=name, brand=plain_text(product.get('brand')),
            volume=plain_text(product.get('descriptiveSize')), description=plain_text(product.get('subtitle')),
            search_extra=' '.join(keywords), image=image, price=price,
            ordinary=money(reference) + ' kr/' + ordinary_unit + (' +pant' if has_deposit else ''),
            member=member, member_label='PRIO', eligibility='Kupong krävs' if coupon else ('LOYALTY' if member else 'GENERAL'),
            conditions=conditions, start=start.isoformat(), end=end.isoformat(),
            comparison=dict(kind='gtin', variants=[compare_variant(product['gtin'], ' · '.join(filter(None, [name, plain_text(product.get('brand')), plain_text(product.get('descriptiveSize'))])), product['id'])] if valid_gtin(product.get('gtin')) else []), **metrics))
    return result


def fetch_citygross():
    config, store = STORES['citygross'], citygross_metadata()
    base = 'https://www.citygross.se/api/v1/Loop54/category/2930/products'
    rows, expected, size, pages, began = [], None, 500, None, time.monotonic()
    for page in range(10):
        if time.monotonic() - began > 40:
            raise TimeoutError('City Gross hämtning tog för lång tid.')
        params = dict(currentWeekDiscountOnly='true', store='3210', take=500, skip=page * size,
                      SortField='p_name', SortOrder='ASC')
        data = require_mapping(public_json(base + '?' + urllib.parse.urlencode(params)), 'City Gross product page')
        batch = data.get('items')
        total, page_size, total_pages = data.get('totalCount'), data.get('pageSize'), data.get('totalPages')
        if (not isinstance(batch, list) or type(total) is not int or not 0 <= total <= 5000
                or type(page_size) is not int or not 1 <= page_size <= 500
                or type(total_pages) is not int or not 0 <= total_pages <= 10
                or type(data.get('currentPage')) is not int or data['currentPage'] != page
                or len(batch) > page_size):
            raise ValueError('City Gross sidindelning har ändrats.')
        if expected is None:
            expected, size, pages = total, page_size, total_pages
            if pages != (total + size - 1) // size and not (total == 0 and pages == 1):
                raise ValueError('City Gross sidantal är inkonsekvent.')
        elif (total, page_size, total_pages) != (expected, size, pages):
            raise ValueError('City Gross priser ändrades under hämtningen.')
        rows.extend(batch)
        if page + 1 >= pages:
            break
    else:
        raise ValueError('City Gross skickade för många sidor.')
    if len(rows) != expected:
        raise ValueError('City Gross produktlista är ofullständig.')
    now = dt.datetime.now(STOCKHOLM)
    return dict(store=config['name'], address=plain_text(store.get('streetAddress')), source=config['source'],
                fetchedAt=now.isoformat(), sourceResults=len(rows), today=now.date().isoformat(),
                offers=normalize_citygross(rows, now.date()),
                note='City Gross webbpriser för Halmstad. PRIO-priser kräver medlemskap. Se butikens fullständiga villkor.')


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
    parser.close()
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


@cached_metadata(ttl=86400, max_entries=1)
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


@cached_metadata(ttl=86400, max_entries=6)
def coop_store(store_key):
    config = STORES[store_key]
    access = coop_access()
    url = access['storeApiUrl'].rstrip('/') + '/stores/' + config['id'] + '?api-version=v7&includeFlyers=true'
    data = require_mapping(public_json(url, {'Ocp-Apim-Subscription-Key': access['storeApiSubscriptionKey']}), 'Coop store')
    if (str(data.get('ledgerAccountNumber')) != config['id'] or data.get('name') != config['name']
            or clean_text(data.get('city')).casefold() != 'halmstad'):
        raise ValueError('Butikens identitet kunde inte bekräftas.')
    return data


def coop_price_label(price):
    style = price.get('termStyle')
    if style == 'Ladder Deal':
        steps = price.get('steps')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise ValueError('Coops mängdpris saknar giltiga steg.')
        labels, details = [], []
        for step in steps:
            step = require_mapping(step, 'Coop price step')
            quantity = price_amount(step.get('quantity'))
            if quantity is None or quantity <= 0 or step.get('dealType') != 'pris':
                raise ValueError('Okänt mängdpris från Coop.')
            unit = plain_text(step.get('unit'))
            count = f'{quantity:g}' + (' ' + unit if unit and unit != 'st' else '')
            labels.append(count + ' för ' + money(step.get('discountValue')) + ' kr')
            comparison = plain_text(step.get('comparisonPrice'))
            if comparison:
                details.append(comparison)
        return ' · '.join(labels), details
    if style == 'Buy X, Pay Y':
        buy, pay = price_amount(price.get('buyAmount')), price_amount(price.get('payAmount'))
        if (buy is None or pay is None or not 0 < pay < buy or
                buy != buy.to_integral_value() or pay != pay.to_integral_value()):
            raise ValueError('Ogiltigt köp-/betala-villkor från Coop.')
        claim = plain_text(price.get('claim'))
        return f'Köp {buy:g}, betala för {pay:g}', [claim] if claim else []
    amount = money(price.get('discountValue'))
    if style == 'Percentage Off':
        return amount + ' % rabatt', []
    if style != 'Discount Price':
        raise ValueError('Okänd pristyp från Coop.')
    quantity = price_amount(price.get('quantity'))
    unit = plain_text(price.get('unit'))
    if quantity is not None and quantity > 1:
        return f'{quantity:g} för {amount} kr', []
    return amount + ' kr' + ('/' + unit if unit else ''), []


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
        label, price_conditions = coop_price_label(price)
        conditions = price_conditions + [plain_text(content.get(k)) for k in
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
        offers.append(dict(comparison=dict(kind='gtin', variants=coop_compare_variants([raw] + variants), familyVariants=[dict(name=plain_text((r.get('content') or {}).get('title')), brand=plain_text((r.get('content') or {}).get('brand')), volume=plain_text((r.get('content') or {}).get('amountInformation'))) for r in [raw] + variants]), name=name, brand=plain_text(content.get('brand')),
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


def lidl_campaigns(stockholm_day):
    # All three local branches share this public regional dataset, fetched once.
    homepage = read_public('https://www.lidl.se/')
    paths = list(dict.fromkeys(re.findall(r'href="(/c/store/[^"#?]+)"', homepage)))
    if not 1 <= len(paths) <= 60:
        raise ValueError('Lidls kampanjlista har ändrats.')
    for path in paths:
        if not re.fullmatch(r'/c/store/[a-z0-9-]+/a\d+', path):
            raise ValueError('Okänd kampanjadress från Lidl.')
    products = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        # Bounded batches avoid retaining every campaign HTML page at once.
        for offset in range(0, len(paths), 4):
            urls = ['https://www.lidl.se' + path for path in paths[offset:offset + 4]]
            for page in pool.map(read_public, urls):
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
        offers.append(dict(name=plain_text(raw.get('fullTitle') or raw.get('title')), brand={12178463: 'OLW'}.get(raw['productId'], ''), volume=volume,
            description=plain_text(facts.get('description')), image=safe_image(raw.get('image') or ''),
            price=money(amount) + ' kr' + ('/kg' if unit == 'kg' else ''),
            ordinary=money(old) + ' kr' if old is not None else '', ordinary_label='Visat referenspris',
            member=plus is not None, member_label='Lidl Plus', eligibility='LOYALTY' if plus is not None else 'GENERAL',
            conditions=list(dict.fromkeys(filter(None, conditions))), start=start.isoformat(), end=end.isoformat(),
            discount_percent=percent, saving=saving, saving_unit=unit,
            comparison=dict(kind='lidl', path=raw.get('canonicalUrl') or raw.get('canonicalPath'), product=raw['productId'])))
    return offers


@cached_metadata(ttl=86400, max_entries=3)
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
                comparison=dict(kind='axfood', sku=clean_text(promo.get('mainProductCode')), promotion=clean_text(promo.get('code'))),
                **discount_details(product, promo)))
    return offers


@cached_metadata(ttl=86400, max_entries=3)
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
    started = time.monotonic()
    expected_total = None
    expected_pages = None
    for page in range(100):
        if time.monotonic() - started > 40:
            raise TimeoutError('Butikens hämtning tog för lång tid.')
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


def escape(value):
    return html.escape(str(value), quote=True)


def display_date(value):
    return dt.date.fromisoformat(value).strftime('%d/%m/%Y')


def search_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c))


# Search only public offer metadata; this bounded cache stores no visitor queries.
SEARCH_UNITS = {'g': 'g', 'gr': 'g', 'gram': 'g', 'grams': 'g', 'kg': 'kg', 'kilogram': 'kg',
                'mg': 'mg', 'milligram': 'mg', 'hg': 'hg', 'hektogram': 'hg',
                'mm': 'mm', 'cm': 'cm', 'dm': 'dm', 'm': 'm', 'meter': 'm',
                'oz': 'oz', 'ounce': 'oz', 'lb': 'lb', 'lbs': 'lb',
                'ml': 'ml', 'milliliter': 'ml',
                'cl': 'cl', 'centiliter': 'cl', 'dl': 'dl', 'deciliter': 'dl',
                'l': 'l', 'liter': 'l', 'litre': 'l', 'liters': 'l', 'st': 'st', 'styck': 'st', 'stycken': 'st',
                'pack': 'pack', '%': '%'}
SEARCH_TOKEN = re.compile(r'\d+(?:[.,]\d+)?|[^\W\d_]+|%', re.UNICODE)
SEARCH_MEASURE = re.compile(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*('
                            + '|'.join(sorted(SEARCH_UNITS, key=len, reverse=True)) + r')(?![a-z])')
SEARCH_PROTECTED = frozenset(('utan', 'med', 'ej', 'inte', 'fri', 'fritt', 'fria', 'no', 'non', 'free', 'eko'))
SEARCH_DIET_PREFIXES = ('laktos', 'gluten', 'mjolkfri', 'notfri', 'aggfri', 'sockerfri',
                        'osotad', 'osaltad', 'alkoholfri', 'vegan', 'vegetar', 'ekolog')


def search_number(value):
    integer, _, fraction = value.replace(',', '.').partition('.')
    integer, fraction = integer.lstrip('0') or '0', fraction.rstrip('0')
    return integer + ('.' + fraction if fraction else '')


def search_tokens(value):
    normalized = search_key(value)
    words = []
    for token in SEARCH_TOKEN.findall(normalized):
        words.append(search_number(token) if token[0].isdigit() else SEARCH_UNITS.get(token, token))
    measures = frozenset((search_number(number), SEARCH_UNITS[unit])
                         for number, unit in SEARCH_MEASURE.findall(normalized))
    return tuple(dict.fromkeys(words)), measures


def search_protected(term):
    return (not term.isalpha() or term in SEARCH_UNITS.values() or term in SEARCH_PROTECTED
            or term.startswith(SEARCH_DIET_PREFIXES) or term.endswith(('fri', 'fritt', 'fria')))


def search_literal(term, words):
    if term in words:
        return True
    if search_protected(term):
        # Dietary/negative terms, numbers and units require complete literal tokens.
        return False
    return any(term in word for word in words if word.isalpha())


@lru_cache(maxsize=4096)
def search_document(name, brand, volume, description, variants):
    primary, _ = search_tokens(name + ' ' + brand)
    words, measures = search_tokens(' '.join((name, brand, volume, description, variants)))
    return primary, words, measures


def search_offers(offers, query):
    terms, measures = search_tokens(query)
    if not terms:
        return offers
    strict_terms = tuple(term for term in terms if search_protected(term))
    documents = []
    for offer in offers:
        document = search_document(offer['name'], offer['brand'], offer['volume'],
                                   offer['description'], offer.get('search_extra', ''))
        if measures <= document[2] and all(search_literal(term, document[1]) for term in strict_terms):
            documents.append((offer, document))
    exact = []
    for offer, (primary, words, _) in documents:
        if all(search_literal(term, words) for term in terms):
            # Name/brand matches outrank matches found only in descriptions/variants.
            rank = (search_key(offer['name']).strip() == search_key(query).strip(),
                    sum(term in primary for term in terms),
                    sum(search_literal(term, primary) for term in terms))
            exact.append((rank, offer))
    if exact:
        return [offer for _, offer in sorted(exact, key=lambda item: item[0], reverse=True)]
    # Long natural-language requests keep strict keyword behavior; no broad expansion.
    if len(terms) > 8:
        return []
    vocabulary = sorted({word for _, (_, words, _) in documents for word in words
                         if len(word) >= 4 and not search_protected(word)})
    corrections, whole_corrections = {}, {}
    for term in terms:
        if len(term) < 4 or search_protected(term):
            continue
        edits = 1 if len(term) < 10 else 2
        # Swedish compounds often put the product word at an edge: snabb+kaffe.
        # Bound the edge lengths and never split protected dietary words.
        forms = {word: {word} | {part for size in range(max(4, len(term) - edits), len(term) + edits + 1)
                                for part in (word[:size], word[-size:])}
                 for word in vocabulary}
        candidates = sorted({part for parts in forms.values() for part in parts})
        matches = {word: 1 - distance / max(len(term), len(word))
                   for word, distance, _ in process.extract(term, candidates, scorer=OSA.distance,
                                                          score_cutoff=edits, limit=None)
                   if 1 - distance / max(len(term), len(word)) >= .8}
        corrections[term] = {word: max((matches.get(part, 0) for part in parts), default=0)
                             for word, parts in forms.items()}
        whole_corrections[term] = {word: matches[word] for word in vocabulary if word in matches}
    approximate = []
    for offer, (primary, words, _) in documents:
        scores, primary_matches = [], 0
        for term in terms:
            if search_literal(term, words):
                scores.append(1)
                primary_matches += search_literal(term, primary)
                continue
            score = max((corrections.get(term, {}).get(word, 0) for word in words), default=0)
            if not score:
                break
            scores.append(score)
            primary_matches += max((whole_corrections.get(term, {}).get(word, 0) for word in primary), default=0)
        else:
            approximate.append((sum(scores) / len(scores), primary_matches,
                                dict(offer, search_approximate=True)))
    approximate.sort(key=lambda item: (-item[0], -item[1]))
    return [offer for _, _, offer in approximate]


def select_offers(offers, query='', mode='Alla erbjudanden', expiry='Alla slutdatum',
                  order='Willys ordning', today=None):
    today = today or dt.datetime.now(STOCKHOLM).date()
    selected = []
    for offer in offers:
        end = dt.date.fromisoformat(offer['end'])
        start = dt.date.fromisoformat(offer['start']) if offer.get('start') else None
        if ((start is not None and today < start) or today > end
                or (start is None and offer.get('observed') != today.isoformat())):
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
        selected.append(offer)
    if query.strip():
        selected = search_offers(selected, query)
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
    if offer.get('search_approximate'):
        membership += '<span class="badge">Liknande träff</span>'
    discount = offer.get('discount_percent')
    saving = offer.get('saving')
    saving_label = ''
    if discount is not None and discount > 0 and saving is not None:
        membership += f'<span class="badge">−{discount:.0f} %</span>'
        saving_text = f'{saving:.2f}'.replace('.', ',')
        saving_label = f'<p class="ordinary">Besparing {saving_text} kr/{escape(offer["saving_unit"])}</p>'
    image = ('<img loading="lazy" decoding="async" referrerpolicy="no-referrer" src="' + escape(offer['image']) +
             '" alt="' + escape(offer['name']) + '">') if offer['image'] else '<span>Bild saknas</span>'
    ordinary = '<p class="ordinary">' + escape(offer.get('ordinary_label', 'Ordinarie')) + ' ' + escape(offer['ordinary']) + '</p>' if offer['ordinary'] else ''
    conditions = ''.join('<div>' + escape(v) + '</div>' for v in offer['conditions'])
    variants = offer.get('variants', [])
    if variants:
        conditions += '<details><summary>Visa ' + str(len(variants)) + ' sorter</summary>' + ''.join(
            '<div>' + escape(v) + '</div>' for v in variants) + '</details>'
    brand = ' · '.join(filter(None, [offer['brand'], offer['volume']]))
    dates = (display_date(offer['start']) + ' – ' + display_date(offer['end'])) if offer.get('start') else ('Gäller till ' + display_date(offer['end']))
    return f'''<article class="offer-card" data-offer="{offer_key(offer)}"><div class="offer-turn"><div class="offer-front"><div class="badges">{membership}{urgency}</div>
<button type="button" class="offer-picture" data-compare-open aria-expanded="false" aria-label="Jämför {escape(offer['name'])} i Halmstad">{image}<span class="compare-affordance"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 8h14l-4-4M20 16H6l4 4"/><path d="M18 8a7 7 0 0 1 1 8M6 16a7 7 0 0 1-1-8"/></svg>Jämför priser</span></button><div class="offer-brand">{escape(brand)}</div>
<h2>{escape(offer['name'])}</h2><p class="offer-description">{escape(offer['description'])}</p>
<div class="price-block"><div class="offer-price">{escape(offer['price'])}</div>{ordinary}{saving_label}</div>
<div class="conditions">{conditions}</div><div class="offer-dates">{dates}</div></div><section class="offer-back" aria-label="Prisjämförelse" aria-hidden="true" inert><header class="compare-header"><div><h3>Jämför i Halmstad</h3><p class="compare-product">{escape(offer['name'])}</p></div><button type="button" class="compare-close quiet" data-compare-close aria-label="Tillbaka till produkten"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m14 6-6 6 6 6"/></svg></button></header><div class="compare-state"></div></section></div></article>'''


def read_public(url, headers=None):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'FoodSale/2.0 (public offers viewer)',
        'Accept': 'application/json,text/html', **(headers or {})})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                body = response.read(12_000_001)
                if len(body) > 12_000_000:
                    raise ValueError('Butikens svar är för stort.')
                return body.decode('utf-8')
        except urllib.error.HTTPError as exc:
            delay = 0
            retry = exc.headers.get('Retry-After', '')
            try:
                delay = max(0, int(retry))
            except ValueError:
                if retry:
                    from email.utils import parsedate_to_datetime
                    try:
                        delay = max(0, (parsedate_to_datetime(retry) - dt.datetime.now(dt.timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        delay = 30
            exc.source_retry_after = delay
            if attempt == 0 and exc.code in (502, 503, 504) and delay <= 1:
                time.sleep(max(.25, delay))
                continue
            raise


def get_json(path, params=None, base=BASE):
    url = base + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    return require_mapping(public_json(url), 'store response')


PAGE = r"""<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="description" content="Veckans butikserbjudanden i Halmstad. Sök, filtrera och hitta dina fynd.">
<title>Food Sale · Halmstad</title>
<script>(()=>{let theme;try{theme=new URLSearchParams(location.search).get('theme')||localStorage.getItem('foodsale-theme')}catch{}if(!['light','dark'].includes(theme))theme=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';document.documentElement.dataset.theme=theme})();
</script>
<style>
:root {
  color-scheme: light;
  --viewer-bg: #f6f5f1; --viewer-card: #ffffff; --viewer-text: #222222;
  --viewer-muted: #62625d; --viewer-border: #d3d1c9; --viewer-control: #88877f;
  --viewer-accent: #a12d26; --viewer-input: #ffffff; --viewer-hover: #edece6;
  --viewer-warning-bg: #fff3dc; --viewer-warning-text: #66480e;
  --viewer-error-bg: #fff1ee; --viewer-error-text: #a12d26;
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px;
  --space-6: 24px; --space-8: 32px;
}
:root[data-theme=dark] {
  color-scheme: dark;
  --viewer-bg: #17191d; --viewer-card: #22262c; --viewer-text: #f1f2f5;
  --viewer-muted: #b8bfc9; --viewer-border: #414854; --viewer-control: #737e8b;
  --viewer-accent: #ffaaa3; --viewer-input: #252a32; --viewer-hover: #303640;
  --viewer-warning-bg: #342a17; --viewer-warning-text: #f3ce82;
  --viewer-error-bg: #352320; --viewer-error-text: #ffb4a9;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { scroll-padding-top: var(--space-4); }
body { margin: 0; background: var(--viewer-bg); color: var(--viewer-text); font: 1rem/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
button, input, select { font: inherit; }
a { color: var(--viewer-accent); text-underline-offset: .2em; text-decoration-thickness: 1px; }
button, a, input, select { touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
:focus-visible { outline: 2px solid var(--viewer-accent); outline-offset: 3px; }
::selection { color: var(--viewer-card); background: var(--viewer-accent); }
input { caret-color: var(--viewer-accent); }
button, select { cursor: pointer; }
button, .button-link {
  display: inline-flex; align-items: center; justify-content: center; gap: var(--space-2);
  min-height: 44px; padding: 10px var(--space-4); border: 1px solid var(--viewer-control);
  border-radius: 8px; background: var(--viewer-card); color: var(--viewer-text);
  font-size: .875rem; font-weight: 650; text-decoration: none;
  transition: background-color 150ms ease-out, border-color 150ms ease-out;
}
button:hover:not(:disabled), .button-link:hover { background: var(--viewer-hover); border-color: var(--viewer-accent); }
button:active:not(:disabled), .button-link:active { background: var(--viewer-hover); }
button:disabled { cursor: wait; opacity: .6; }
button.quiet { background: transparent; border-color: transparent; }
button.quiet:hover:not(:disabled) { border-color: var(--viewer-border); }
.icon { flex: none; width: 16px; height: 16px; vertical-align: middle; }
.block-container { max-width: 1390px; margin: auto; padding: var(--space-8) 40px; }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); margin-bottom: var(--space-8); }
.brand { display: inline-flex; align-items: center; gap: var(--space-3); min-height: 44px; color: var(--viewer-text); text-decoration: none; font-size: 1.125rem; font-weight: 750; letter-spacing: -.025em; }
.brand-mark { padding: 6px 10px; border-radius: 8px; background: #a12d26; color: white; font-size: 1.25rem; letter-spacing: -.03em; }
.brand small { display: block; margin-top: 1px; font-size: .6875rem; letter-spacing: .14em; font-weight: 650; color: var(--viewer-muted); }
.hero { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--space-6); padding-bottom: var(--space-8); margin-bottom: var(--space-6); border-bottom: 1px solid var(--viewer-border); }
.hero h1 { margin: 0 0 var(--space-4); font-size: 3rem; line-height: 1.08; font-weight: 750; letter-spacing: -.03em; text-wrap: balance; }
.hero p { max-width: 65ch; margin: 0; font-size: .9375rem; line-height: 1.6; color: var(--viewer-muted); }
.hero-aside { flex: none; font-size: .8125rem; line-height: 1.9; text-align: right; color: var(--viewer-muted); }
.hero-aside small { font-size: .75rem; }
.location-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--viewer-accent); margin-right: 5px; }
.store-bar { display: flex; align-items: center; gap: var(--space-6); padding: var(--space-4) var(--space-6); border: 1px solid var(--viewer-border); border-radius: 12px; background: var(--viewer-card); margin-bottom: var(--space-4); }
.store-picker { flex: 0 1 360px; min-width: 0; max-width: 100%; }
label { display: block; margin-bottom: 6px; font-size: .75rem; font-weight: 650; color: var(--viewer-muted); }
input, select { width: 100%; min-width: 0; min-height: 46px; padding: 11px var(--space-3); border: 1px solid var(--viewer-control); border-radius: 8px; background: var(--viewer-input); color: var(--viewer-text); }
select { padding-right: var(--space-8); }
input::placeholder { color: var(--viewer-muted); opacity: 1; }
.store-context { display: flex; flex: 1 1 160px; min-width: 0; flex-direction: column; gap: var(--space-1); font-size: .875rem; overflow-wrap: anywhere; }
.store-context span { font-size: .8125rem; color: var(--viewer-muted); }
.source-link { display: inline-flex; align-items: center; gap: 6px; min-height: 44px; padding: var(--space-2) 0; margin-left: auto; font-size: .8125rem; text-decoration: none; }
.source-link:hover { text-decoration: underline; }
.summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-4); margin-bottom: var(--space-6); }
.summary-item { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--space-2); padding: var(--space-3) var(--space-4); border: 1px solid var(--viewer-border); border-radius: 12px; background: var(--viewer-card); }
.summary-item strong { font-size: 1.625rem; line-height: 1.3; letter-spacing: -.025em; font-variant-numeric: tabular-nums; }
.summary-item span { font-size: .8125rem; color: var(--viewer-muted); }
.filters { display: grid; grid-template-columns: 2fr 1.2fr 1.1fr 1.6fr; gap: var(--space-4); align-items: end; }
.filters > div { min-width: 0; }
.actions { display: flex; gap: var(--space-2); grid-column: 1 / -1; justify-content: flex-end; }
#refresh { min-width: 150px; }
.caption { margin: var(--space-4) 0; font-size: .8125rem; line-height: 1.65; color: var(--viewer-muted); }
.notice { margin: var(--space-4) 0; padding: var(--space-3) var(--space-4); border: 1px solid var(--viewer-border); border-radius: 8px; background: var(--viewer-card); color: var(--viewer-text); font-size: .875rem; line-height: 1.6; }
.notice.warning { background: var(--viewer-warning-bg); color: var(--viewer-warning-text); }
.notice.error { background: var(--viewer-error-bg); color: var(--viewer-error-text); }
.results-bar { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: var(--space-2) var(--space-4); margin: var(--space-6) 0 var(--space-4); font-size: .8125rem; color: var(--viewer-muted); }
.results-bar strong { color: var(--viewer-text); font-size: .875rem; font-weight: 650; font-variant-numeric: tabular-nums; }
.offer-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(235px, 1fr)); gap: var(--space-4); }
.offer-card { display: flex; flex-direction: column; min-width: 0; padding: var(--space-4); border: 1px solid var(--viewer-border); border-radius: 14px; background: var(--viewer-card); }
.offer-card:focus-within { border-color: var(--viewer-accent); }
.badges { display: flex; align-items: flex-start; flex-wrap: wrap; gap: 6px; min-height: 26px; }
.badge { padding: 3px 6px; border-radius: 4px; background: var(--viewer-bg); color: var(--viewer-muted); font-size: .6875rem; line-height: 1.55; font-weight: 650; }
.badge.plus { color: var(--viewer-accent); }
.badge.ending { background: var(--viewer-accent); color: var(--viewer-bg); }
.offer-picture { display: flex; align-items: center; justify-content: center; height: 165px; padding: 10px; margin: var(--space-3) 0 var(--space-4); border-radius: 8px; background: white; color: #62625d; font-size: .8125rem; }
.offer-picture img { max-width: 100%; max-height: 145px; object-fit: contain; }
.offer-brand { font-size: .75rem; line-height: 1.6; color: var(--viewer-muted); overflow-wrap: anywhere; }
.offer-card h2 { margin: 6px 0 var(--space-2); font-size: 1.125rem; line-height: 1.35; font-weight: 700; letter-spacing: -.015em; overflow-wrap: anywhere; }
.offer-description { margin: 0 0 var(--space-4); font-size: .8125rem; line-height: 1.6; color: var(--viewer-muted); overflow-wrap: anywhere; }
.offer-description:empty { display: none; }
.price-block { margin-top: auto; }
.offer-price { font-size: 1.75rem; line-height: 1.25; font-weight: 750; letter-spacing: -.025em; color: var(--viewer-accent); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.ordinary { margin: 6px 0 var(--space-3); font-size: .75rem; line-height: 1.6; color: var(--viewer-muted); }
.conditions { font-size: .75rem; line-height: 1.7; color: var(--viewer-muted); overflow-wrap: anywhere; }
.conditions summary { display: list-item; min-height: 44px; padding: 11px 0; cursor: pointer; color: var(--viewer-accent); }
.conditions details[open] summary { padding-bottom: 6px; }
.offer-dates { padding-top: var(--space-3); margin-top: var(--space-3); border-top: 1px solid var(--viewer-border); font-size: .75rem; line-height: 1.6; color: var(--viewer-muted); font-variant-numeric: tabular-nums; }
.empty { padding: 40px var(--space-6); border: 1px dashed var(--viewer-border); border-radius: 12px; color: var(--viewer-muted); text-align: center; }
.empty h2, .empty strong { color: var(--viewer-text); }
.empty h2 { font-size: 1.375rem; margin: 0 0 var(--space-3); }
.empty p { max-width: 65ch; margin: var(--space-2) auto var(--space-6); font-size: .9375rem; }
.pagination { padding: var(--space-6) 0; text-align: center; }
.pagination button { margin: var(--space-1); }
.skeleton { min-width: 0; height: 370px; padding: var(--space-4); border: 1px solid var(--viewer-border); border-radius: 14px; background: var(--viewer-card); }
.skeleton div { height: 165px; margin: var(--space-8) 0 var(--space-6); border-radius: 8px; background: var(--viewer-bg); }
.skeleton span { display: block; height: 16px; width: 70%; margin: var(--space-4) 0; border-radius: 4px; background: var(--viewer-bg); }
.skeleton span:last-child { width: 40%; }
.footnote { margin-top: var(--space-6); padding-top: var(--space-6); border-top: 1px solid var(--viewer-border); font-size: .8125rem; line-height: 1.7; color: var(--viewer-muted); }
.footnote p { max-width: 75ch; }
.skip { position: absolute; left: var(--space-4); top: -100px; z-index: 1; padding: var(--space-3); border-radius: 6px; background: var(--viewer-card); }
.skip:focus { top: 10px; }
@media (max-width: 1000px) {
  .store-bar { flex-wrap: wrap; gap: var(--space-3) var(--space-6); }
  .store-picker { flex-basis: 100%; }
  .filters { grid-template-columns: 1fr 1fr; }
  .search-field { grid-column: 1 / -1; }
  .hero-aside { display: none; }
}
@media (max-width: 640px) {
  .block-container { padding: var(--space-6) var(--space-4); }
  .topbar { margin-bottom: var(--space-6); }
  .hero { padding-bottom: var(--space-6); }
  .hero h1 { font-size: 2.5rem; }
  .hero p { font-size: .875rem; }
  .store-bar { padding: var(--space-4); }
  .source-link { margin-left: 0; }
  .summary { gap: var(--space-2); }
  .summary-item { display: block; padding: var(--space-3) 10px; }
  .summary-item strong { display: block; font-size: 1.5rem; margin-bottom: var(--space-1); }
  .summary-item span { display: block; font-size: .75rem; line-height: 1.45; }
  .filters { gap: var(--space-3); }
  .actions { justify-content: stretch; }
  .actions button { flex: 1; }
  input, select { font-size: 1rem; }
  .offer-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
  .offer-card { padding: var(--space-3); }
  .offer-picture { height: 130px; padding: 6px; margin: var(--space-3) 0; }
  .offer-picture img { max-height: 118px; }
  .offer-card h2 { font-size: 1rem; }
  .offer-price { font-size: 1.5rem; }
}
@media (max-width: 359px) {
  .offer-grid { grid-template-columns: minmax(0, 1fr); }
  .brand { gap: var(--space-2); }
  #theme { padding: 10px var(--space-2); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; scroll-behavior: auto !important; }
}
/* A single-card comparison keeps its place in the product grid. */
.offer-card { display: block; padding: 0; perspective: 1400px; overflow: visible; }
.offer-turn { position: relative; display: grid; height: 100%; transform-style: preserve-3d; transition: transform .46s cubic-bezier(.2,.8,.2,1); }
.offer-card.is-flipped .offer-turn { transform: rotateY(180deg); }
.offer-front, .offer-back { grid-area: 1 / 1; backface-visibility: hidden; -webkit-backface-visibility: hidden; border-radius: 13px; background: var(--viewer-card); }
.offer-front { display: flex; flex-direction: column; min-width: 0; padding: 16px; }
.offer-back { position: absolute; inset: 0; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; transform: rotateY(180deg); text-align: left; }
button.offer-picture { position: relative; width: 100%; border: 0; cursor: pointer; color: #292923; box-shadow: none; }
button.offer-picture:hover { background: #f4f3ef; }
.compare-affordance { position: absolute; right: 6px; bottom: 6px; display: inline-flex; gap: 6px; align-items: center; min-height: 32px; padding: 4px 8px; border-radius: 6px; background: white; color: #292923; font-size: .75rem; line-height: 1.2; }
.compare-affordance svg { width: 16px; height: 16px; }
.compare-header { display: grid; grid-template-columns: minmax(0,1fr) 44px; gap: 8px; align-items: start; padding: 16px 12px 8px 16px; border-bottom: 1px solid var(--viewer-border); }
.compare-header h3 { margin: 0 0 4px; font-size: 1rem; line-height: 1.35; overflow-wrap: anywhere; }
.compare-product { margin: 0; font-size: .75rem; line-height: 1.4; color: var(--viewer-muted); overflow-wrap: anywhere; }
.compare-close { width: 44px; min-width: 44px; min-height: 44px; padding: 10px; }
.compare-close svg { width: 20px; height: 20px; }
.compare-state { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.compare-status { padding: 8px 16px; margin: 0; font-size: .75rem; line-height: 1.45; color: var(--viewer-muted); }
.compare-progress { width: calc(100% - 32px); height: 3px; margin: 0 16px 8px; accent-color: var(--viewer-accent); }
.compare-scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin; scrollbar-color: var(--viewer-border) transparent; padding: 0 16px 12px; }
.compare-list { padding: 0; margin: 0; list-style: none; }
.compare-row { padding: 12px 0; border-bottom: 1px solid var(--viewer-border); }
.compare-row:last-child { border-bottom: 0; }
.compare-store { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 0 0 5px; }
.compare-store a { display: inline-flex; align-items: center; min-height: 44px; color: inherit; font-size: .8125rem; font-weight: 650; text-decoration: underline; text-decoration-color: var(--viewer-border); text-underline-offset: 3px; overflow-wrap: anywhere; }
.compare-selected { color: var(--viewer-accent); font-size: .6875rem; font-weight: 700; }
.compare-meta, .compare-message { font-size: .75rem; line-height: 1.45; margin: 4px 0 0; color: var(--viewer-muted); overflow-wrap: anywhere; }
.compare-prices { display: grid; grid-template-columns: 1fr 1fr .8fr; gap: 6px; margin: 8px 0 0; }
.compare-prices div { min-width: 0; }
.compare-prices dt { font-size: .6875rem; line-height: 1.4; color: var(--viewer-muted); }
.compare-prices dd { margin: 3px 0 0; font-size: .8125rem; line-height: 1.4; font-weight: 650; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.compare-row[data-selected="true"] .compare-prices dd { font-weight: 750; }
.compare-row details { margin-top: 4px; font-size: .75rem; color: var(--viewer-muted); }
.compare-row summary { display: flex; align-items: center; min-height: 44px; cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }
.compare-footer { padding: 8px 16px; border-top: 1px solid var(--viewer-border); }
.compare-footer p { margin: 0; font-size: .75rem; line-height: 1.4; color: var(--viewer-muted); }
.compare-choice { display: grid; gap: 12px; padding: 12px 16px; }
.compare-choice label { font-size: .8125rem; font-weight: 650; }
.compare-choice select { width: 100%; min-width: 0; min-height: 44px; }
.compare-choice button, .compare-retry { min-height: 44px; width: 100%; }
.compare-error { margin: 0; padding: 12px 16px; font-size: .8125rem; line-height: 1.5; }
.compare-retry { margin-top: 8px; }
@media (max-width: 680px) {
  .offer-front { padding: 12px; }
  .compare-header { padding-left: 12px; padding-right: 6px; }
  .compare-header h3 { font-size: .875rem; }
  .compare-scroll { padding-left: 12px; padding-right: 12px; }
  .compare-status, .compare-footer { padding-left: 12px; padding-right: 12px; }
  .compare-affordance { left: 2px; right: 2px; justify-content: center; font-size: .6875rem; }
}
@media (prefers-reduced-motion: reduce) { .offer-turn { transition: none; } }

</style>
</head>
<body>
<a class="skip" href="#main">Hoppa till erbjudanden</a>
<div class="block-container">
<header class="topbar"><a class="brand" href="/" aria-label="Food Sale startsida"><span class="brand-mark">fs.</span><span>Food Sale <small>HALMSTAD</small></span></a>
<button id="theme" class="quiet" type="button" aria-label="Byt till mörkt tema">Mörkt tema</button></header>
<main id="main" tabindex="-1">
<section class="hero"><div><h1>Veckans fynd,<br>nära dig.</h1><p>Alla erbjudanden på ett ställe. Hitta det som passar dig.</p></div><div class="hero-aside"><span class="location-dot"></span> Halmstad, Sverige<br><small id="coverage">19 butiker · välj butik för aktuella erbjudanden</small></div></section>
<section class="store-bar" aria-label="Butik"><div class="store-picker"><label for="store">Välj din butik</label><select id="store"><option value="willys-oster">Willys Halmstad Öster</option><option value="willys-flygstaden">Willys Halmstad Flygstaden</option><option value="hemkop-c">Hemköp Halmstad C</option><option value="coop-stora">Stora Coop Halmstad</option><option value="coop-gamletull">Coop Gamletull Halmstad</option><option value="coop-flygaregatan">Coop Flygaregatan</option><option value="coop-furuvagen">Coop Furuvägen</option><option value="coop-fyllinge">Coop Fyllinge</option><option value="xtra-snostorp">X:-Tra Snöstorpsvägen</option><option value="lidl-torpaledsvagen">Lidl Torpaledsvägen</option><option value="lidl-gesallgatan">Lidl Gesällgatan</option><option value="lidl-tegelbruket">Lidl Tegelbruket</option><option value="ica-hogskolan">Maxi ICA Stormarknad Högskolan · Officiell sida</option><option value="ica-flygstaden">Maxi ICA Stormarknad Flygstaden · Officiell sida</option><option value="ica-vallas">ICA Supermarket Vallås · Officiell sida</option><option value="ica-sondrum">ICA Supermarket Söndrum · Officiell sida</option><option value="ica-frosakull">ICA Nära Frösakull · Officiell sida</option><option value="citygross">City Gross Halmstad · Officiell sida</option><option value="tempo-backagard">Tempo Halmstad Bäckagård · Officiell sida</option></select></div><div class="store-context"><strong id="store-name"></strong><span id="address"></span></div><a id="source" class="source-link" target="_blank" rel="noopener noreferrer">Butikens erbjudanden <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6M20 4 10 14"/><path d="M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/></svg></a></section>
<section id="summary" class="summary" aria-label="Översikt"><div class="summary-item"><strong id="total">—</strong><span>aktuella erbjudanden</span></div><div class="summary-item"><strong id="members">—</strong><span>medlemspriser</span></div><div class="summary-item"><strong id="ending">—</strong><span>slutar idag</span></div></section>
<form id="filters" class="filters" role="search">
<div class="search-field"><label for="search">Sök produkter</label><input type="search" id="search" maxlength="200" placeholder="Vara, varumärke eller storlek…" autocomplete="off"></div>
<div><label for="membership">Medlemskrav</label><select id="membership"><option value="all">Alla erbjudanden</option><option value="general">Utan medlemskrav</option><option value="member">Medlemspriser</option></select></div>
<div><label for="expiry">Giltighet</label><select id="expiry"><option value="all">Alla slutdatum</option><option value="today">Slutar idag</option><option value="soon">Inom 3 dagar</option></select></div>
<div><label for="sort">Sortera</label><select id="sort"><option value="source">Butikens ordning</option><option value="discount">Störst prissänkning (%)</option><option value="saving">Störst besparing (kr/enhet)</option><option value="expiry">Slutar snart</option><option value="name">Namn A–Ö</option></select></div>
<div class="actions"><button id="refresh" type="button"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5"/><path d="M19.5 12a7.5 7.5 0 1 1-2-5L20 9"/></svg><span class="button-label">Uppdatera</span></button><button id="reset" class="quiet" type="button">Återställ</button></div>
</form>
<div id="status" class="notice" role="status" aria-live="polite" hidden></div>
<p id="note" class="caption" hidden></p><p id="sort-note" class="caption" hidden>Besparing beräknas mot butikens visade jämförelsepris, per prisenhet. Sorter kan skilja sig. Saknas jämförbara priser visas varan sist.</p>
<p id="search-note" class="caption" role="status" hidden></p>
<div class="results-bar"><strong id="results-count" aria-live="polite">Hämtar erbjudanden…</strong><span id="freshness"></span></div>
<div id="grid" class="offer-grid" aria-busy="true"></div>
<div id="empty" class="empty" hidden><strong>Inga matchningar just nu</strong><p>Prova ett annat sökord eller återställ filtren.</p></div>
<div id="official" class="empty" hidden><h2>Erbjudandena finns hos butiken</h2><p>Den här butiken har ännu ingen produktlista i Food Sale. Sökning och sortering är därför inte tillgängliga här.</p><div id="leaflets" aria-live="polite"></div><a id="official-source" class="button-link" target="_blank" rel="noopener noreferrer">Öppna butikens erbjudanden <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6M20 4 10 14"/><path d="M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/></svg></a></div>
<div class="pagination"><p id="page-note" class="caption" role="status" aria-live="polite"></p><button id="more" type="button" hidden>Visa fler</button><button id="all" class="quiet" type="button" hidden>Visa alla</button></div>
</main>
<footer class="footnote"><strong>Food Sale / Halmstad</strong><p>Offentliga butikserbjudanden. Medlemspriser kräver medlemskap hos respektive kedja. Personliga kuponger och lokalt lagersaldo ingår inte. Kontrollera alltid pris och villkor hos butiken. Fristående visare.</p></footer>
<noscript><p>Aktivera JavaScript för att söka och filtrera erbjudanden. Butikerna finns också via sina officiella erbjudandesidor.</p></noscript>
</div>
<script id="catalogue" type="application/json">{"willys-oster": {"name": "Willys Halmstad Öster", "provider": "axfood", "address": "Laholmsvägen 37", "member": "Willys Plus", "source": "https://www.willys.se/erbjudanden/butik?StoreID=2236"}, "willys-flygstaden": {"name": "Willys Halmstad Flygstaden", "provider": "axfood", "address": "Kundvägen 4", "member": "Willys Plus", "source": "https://www.willys.se/erbjudanden/butik?StoreID=2137"}, "hemkop-c": {"name": "Hemköp Halmstad C", "provider": "axfood", "address": "Brogatan 14–16", "member": "Klubb Hemköp", "source": "https://www.hemkop.se/butik/4245"}, "coop-stora": {"name": "Stora Coop Halmstad", "provider": "coop", "address": "Prästvägen 1", "member": "Coop-medlem", "source": "https://www.coop.se/butiker-erbjudanden/stora-coop/stora-coop-halmstad/"}, "coop-gamletull": {"name": "Coop Gamletull Halmstad", "provider": "coop", "address": "Gamletullsgatan 20", "member": "Coop-medlem", "source": "https://www.coop.se/butiker-erbjudanden/coop/coop-gamletull-halmstad/"}, "coop-flygaregatan": {"name": "Coop Flygaregatan", "provider": "coop", "address": "Flygaregatan 12", "member": "Coop-medlem", "source": "https://www.coop.se/butiker-erbjudanden/coop/coop-flygaregatan/"}, "coop-furuvagen": {"name": "Coop Furuvägen", "provider": "coop", "address": "Furuvägen 63", "member": "Coop-medlem", "source": "https://www.coop.se/butiker-erbjudanden/coop/coop-furuvagen/"}, "coop-fyllinge": {"name": "Coop Fyllinge", "provider": "coop", "address": "Fyllingevägen 2", "member": "Coop-medlem", "source": "https://www.coop.se/butiker-erbjudanden/coop/coop-fyllinge/"}, "xtra-snostorp": {"name": "X:-Tra Snöstorpsvägen", "provider": "coop", "address": "Snöstorpsvägen 109", "member": "Coop-medlem", "source": "https://x-tra.se/butiker/snoestorpsvaegen"}, "lidl-torpaledsvagen": {"name": "Lidl Torpaledsvägen", "provider": "lidl", "address": "Torpaledsvägen 4", "member": "Lidl Plus", "source": "https://www.lidl.se/s/sv-SE/butiker/halmstad/torpaledsvaegen-4/"}, "lidl-gesallgatan": {"name": "Lidl Gesällgatan", "provider": "lidl", "address": "Gesällgatan 3", "member": "Lidl Plus", "source": "https://www.lidl.se/s/sv-SE/butiker/halmstad/gesaellgatan-3/"}, "lidl-tegelbruket": {"name": "Lidl Tegelbruket", "provider": "lidl", "address": "Tegelbruket 1", "member": "Lidl Plus", "source": "https://www.lidl.se/s/sv-SE/butiker/halmstad/tegelbruket-1/"}, "ica-hogskolan": {"name": "Maxi ICA Stormarknad Högskolan", "provider": "ica", "address": "Halmstad", "source": "https://www.ica.se/erbjudanden/maxi-ica-stormarknad-hogskolan-1003925/"}, "ica-flygstaden": {"name": "Maxi ICA Stormarknad Flygstaden", "provider": "ica", "address": "Halmstad", "source": "https://www.ica.se/erbjudanden/maxi-ica-stormarknad-flygstaden-1003781/"}, "ica-vallas": {"name": "ICA Supermarket Vallås", "provider": "ica", "address": "Halmstad", "source": "https://www.ica.se/erbjudanden/ica-supermarket-vallas-1003811/"}, "ica-sondrum": {"name": "ICA Supermarket Söndrum", "provider": "ica", "address": "Halmstad", "source": "https://www.ica.se/erbjudanden/ica-supermarket-sondrum-1003648/"}, "ica-frosakull": {"name": "ICA Nära Frösakull", "provider": "ica", "address": "Halmstad", "source": "https://www.ica.se/erbjudanden/ica-nara-frosakull-1004064/"}, "citygross": {"name": "City Gross Halmstad", "provider": "citygross", "address": "Stormgatan 14", "source": "https://www.citygross.se/butiker/halmstad"}, "tempo-backagard": {"name": "Tempo Halmstad Bäckagård", "provider": "official", "address": "Akvarellgränd 1", "source": "https://www.tempo.se/butiker/tempo-halmstad-backagard/"}}</script>
<script>'use strict';
const catalogue=JSON.parse(document.getElementById('catalogue').textContent);
const $=id=>document.getElementById(id);
let limit=48, sequence=0, controller, debounce, poll, fetchedAt=null, activeStore=null;
const params=new URLSearchParams(location.search);
$('store').value=catalogue[params.get('store')]?params.get('store'):'willys-oster';
function themeButton(){const dark=document.documentElement.dataset.theme==='dark';$('theme').textContent=dark?'Ljust tema':'Mörkt tema';$('theme').setAttribute('aria-label',dark?'Byt till ljust tema':'Byt till mörkt tema')}
function saveUrl(){const url=new URL(location.href);url.searchParams.set('store',$('store').value);url.searchParams.set('theme',document.documentElement.dataset.theme);history.replaceState(null,'',url)}
$('theme').addEventListener('click',()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;try{localStorage.setItem('foodsale-theme',theme)}catch{}themeButton();saveUrl()});themeButton();
function notice(text,kind='info'){$('status').textContent=text;$('status').className='notice '+kind;$('status').hidden=!text}
function clearCards(){ $('grid').replaceChildren();$('total').textContent='—';$('members').textContent='—';$('ending').textContent='—';$('more').hidden=true;$('all').hidden=true;$('page-note').textContent='';$('results-count').textContent='';$('search-note').hidden=true;$('search-note').textContent='';$('freshness').textContent='';$('empty').hidden=true;fetchedAt=null }
function loading(){if(!$('grid').children.length){$('grid').innerHTML=Array.from({length:6},()=>'<div class="skeleton" aria-hidden="true"><div></div><span></span><span></span></div>').join('')} $('grid').setAttribute('aria-busy','true')}
function storeDetails(){const config=catalogue[$('store').value];$('store-name').textContent=config.name;$('address').textContent=config.address;for(const id of ['source','official-source'])$(id).href=config.source;const external=config.provider==='official';$('official').hidden=!external;$('summary').hidden=external;$('filters').hidden=external;if(external){$('note').hidden=true;$('sort-note').hidden=true}return external}
function schedule(seconds){clearTimeout(poll);poll=setTimeout(()=>{if(document.visibilityState==='visible')load();else schedule(60)},seconds*1000)}
async function load(force=false){
 if(!force&&$('grid').querySelector('.is-flipped')){schedule(60);return}
 clearTimeout(poll);clearTimeout(debounce);if(controller)controller.abort();const requestId=++sequence;const store=$('store').value;
 if(activeStore!==store){clearCards();notice('');$('note').hidden=true;$('sort-note').hidden=true;activeStore=store;$('leaflets').replaceChildren()}saveUrl();
 if(storeDetails()){
  clearCards();$('grid').setAttribute('aria-busy','false');notice('');$('leaflets').replaceChildren();
  if(store==='citygross'){
   controller=new AbortController();$('leaflets').textContent='Hämtar aktuella reklamblad…';
   try{
    const response=await fetch('/api/leaflets/citygross',{signal:controller.signal,cache:'no-store'});
    const data=await response.json();if(requestId!==sequence)return;
    if(!response.ok)throw new Error();
    $('leaflets').replaceChildren();
    const note=document.createElement('p');note.textContent='Aktuella reklamblad. Produktsökning och prissortering är ännu inte tillgängliga.';$('leaflets').append(note);
    for(const leaflet of data.leaflets){const paragraph=document.createElement('p'),link=document.createElement('a');link.className='button-link';link.href=leaflet.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=leaflet.title;paragraph.append(link);const dates=document.createElement('p');dates.textContent=leaflet.description||`${leaflet.start} – ${leaflet.end}`;paragraph.append(dates);$('leaflets').append(paragraph)}
    const status=document.createElement('p');status.textContent=data.message;$('leaflets').append(status);
    schedule(300);
   }catch(error){if(error.name==='AbortError'||requestId!==sequence)return;$('leaflets').textContent='Reklambladen kunde inte hämtas. Använd butikens officiella länk.';schedule(60)}
  }
  return
 }
 if(fetchedAt&&Date.now()-new Date(fetchedAt).getTime()>=600000)clearCards();
 loading();if(!fetchedAt)notice('Hämtar butikens erbjudanden…');
 controller=new AbortController();const query=new URLSearchParams({store,q:$('search').value,membership:$('membership').value,expiry:$('expiry').value,sort:$('sort').value,limit:String(limit)});
 $('refresh').disabled=true;$('refresh').querySelector('.button-label').textContent=force?'Uppdaterar…':'Uppdatera';
 try{
  const response=await fetch('/api/offers?'+query,{method:force?'POST':'GET',headers:force?{'X-FoodSale-Request':'1'}:{},signal:controller.signal,cache:'no-store'});
  const data=await response.json();if(requestId!==sequence)return;
  if(!response.ok||data.status!=='ok')throw new Error(data.message||'Erbjudandena kunde inte hämtas. Försök igen om en stund.');
  $('grid').innerHTML=data.cards;if(['ica','citygross'].includes(catalogue[store].provider)&&data.shown>0)$('store').selectedOptions[0].textContent=catalogue[store].name;$('official').hidden=true;$('summary').hidden=false;$('filters').hidden=false;$('total').textContent=data.total;$('members').textContent=data.members;$('ending').textContent=data.ending;
  $('results-count').textContent=`${data.matches} av ${data.total} erbjudanden`;
  $('search-note').textContent=data.searchApproximate?`Inga exakta träffar. Visar ${data.matches} liknande träffar.`:'';$('search-note').hidden=!data.searchApproximate;
  fetchedAt=data.fetchedAt;const date=new Date(fetchedAt);$('freshness').textContent='Hämtat '+date.toLocaleString('sv-SE',{timeZone:'Europe/Stockholm',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})+' · svensk tid';
  $('note').textContent=data.note||'';$('note').hidden=!data.note;$('sort-note').hidden=!['discount','saving'].includes($('sort').value);
  notice(data.message||'',data.stale||data.refreshFailed?'warning':'info');
  $('empty').hidden=data.matches>0;$('more').hidden=!data.hasMore;$('all').hidden=!data.hasMore;
  $('page-note').textContent=data.hasMore?`Visar ${data.shown} av ${data.matches}. Sökningen omfattar alla erbjudanden.`:'';
  $('more').textContent='Visa 48 till';$('all').textContent=`Visa alla ${data.matches}`;
  schedule(data.refreshing?3:60);
 }catch(error){if(error.name==='AbortError'||requestId!==sequence)return;clearCards();if(['ica','citygross'].includes(catalogue[store].provider)){$('official').hidden=false;$('summary').hidden=true;$('filters').hidden=true;$('store').selectedOptions[0].textContent=catalogue[store].name+' · Officiell sida'}notice(error.message||'Anslutningen misslyckades. Försök igen.','error');schedule(60)}
 finally{if(requestId===sequence){$('grid').setAttribute('aria-busy','false');$('refresh').disabled=false;$('refresh').querySelector('.button-label').textContent='Uppdatera'}}
}
$('filters').addEventListener('submit',event=>{event.preventDefault();limit=48;load()});
$('search').addEventListener('input',()=>{clearTimeout(debounce);limit=48;debounce=setTimeout(()=>load(),200)});
for(const id of ['membership','expiry','sort'])$(id).addEventListener('change',()=>{limit=48;load()});
$('store').addEventListener('change',()=>{limit=48;load()});$('refresh').addEventListener('click',()=>load(true));
$('reset').addEventListener('click',()=>{$('search').value='';$('membership').value='all';$('expiry').value='all';$('sort').value='source';limit=48;load()});
$('more').addEventListener('click',()=>{limit=Math.min(2000,limit+48);load()});$('all').addEventListener('click',()=>{limit=2000;load()});
// Comparisons are requested only by an activated card. Polling never reorders the grid.
const comparisonStates=new WeakMap();
const el=(tag,text,className)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node};
function comparisonClose(card,restoreFocus=true){const state=comparisonStates.get(card);if(state){state.controller?.abort();clearTimeout(state.timer)}card.classList.remove('is-flipped');card.querySelector('.offer-front').inert=false;card.querySelector('.offer-front').setAttribute('aria-hidden','false');const back=card.querySelector('.offer-back');back.inert=true;back.setAttribute('aria-hidden','true');card.querySelector('[data-compare-open]').setAttribute('aria-expanded','false');if(restoreFocus)card.querySelector('[data-compare-open]').focus({preventScroll:true})}
function comparisonCloseAll(){for(const card of $('grid').querySelectorAll('.is-flipped'))comparisonClose(card,false)}
function comparisonError(card,text,refresh=false){const host=card.querySelector('.compare-state');host.replaceChildren();host.append(el('p',text,'compare-error'));const footer=el('div',undefined,'compare-footer');const retry=el('button',refresh?'Uppdatera erbjudandena':'Försök igen','compare-retry');retry.type='button';retry.dataset[refresh?'compareRefresh':'compareRetry']='';footer.append(retry);host.append(footer)}
function comparisonRender(card,data){
 const state=comparisonStates.get(card);if(!state||!card.classList.contains('is-flipped'))return;
 const host=card.querySelector('.compare-state'),oldScroll=host.querySelector('.compare-scroll')?.scrollTop||0;
 const opened=new Set([...host.querySelectorAll('details[open]')].map(x=>x.dataset.store));
 const focused=host.contains(document.activeElement)?document.activeElement:null,focusStore=focused?.closest('.compare-row')?.dataset.store;
 const focusKind=focused?.classList.contains('compare-scroll')?'scroll':focused?.tagName==='SUMMARY'?'summary':focused?.tagName==='A'?'link':'';
 card.querySelector('.compare-product').textContent=data.product;
 host.replaceChildren();
 if(data.state==='choose'){
  host.append(el('p',data.message,'compare-status'));const form=el('form',undefined,'compare-choice'),label=el('label','Välj sort och förpackning'),select=el('select');select.setAttribute('aria-label','Exakt produktvariant');
  for(const variant of data.variants){const option=el('option',variant.name+(variant.gtin?' · '+variant.gtin:''));option.value=variant.id;select.append(option)}label.append(select);form.append(label);const button=el('button','Jämför vald variant');button.type='submit';form.append(button);form.addEventListener('submit',event=>{event.preventDefault();comparisonStart(card,select.value)});host.append(form);return
 }
 const status=el('p',data.state==='queued'?'Jämförelsen väntar på ledig hämtning…':data.done?`Prisstatus klar · ${data.completed} av ${data.total} butiker`:`Hämtar priser · ${data.completed} av ${data.total} butiker`,'compare-status');status.setAttribute('role','status');host.append(status);
 if(!data.done){const progress=el('progress',undefined,'compare-progress');progress.max=data.total;progress.value=data.completed;progress.setAttribute('aria-label','Butiker kontrollerade');host.append(progress)}
 const scroll=el('div',undefined,'compare-scroll');scroll.tabIndex=0;scroll.setAttribute('aria-label','Priser i Halmstad');const list=el('ul',undefined,'compare-list');
 for(const row of data.rows){
  const item=el('li',undefined,'compare-row');item.dataset.selected=String(row.store===data.selected);item.dataset.store=row.store;
  const title=el('div',undefined,'compare-store'),link=el('a',row.name);try{const url=new URL(row.source);if(url.protocol==='https:'){link.href=url.href;link.target='_blank';link.rel='noopener noreferrer'}}catch{}title.append(link);if(row.store===data.selected)title.append(el('span','Vald butik','compare-selected'));item.append(title);
  if(row.status==='price'){
   const meta=[row.scope,row.member?row.memberLabel:''];if(row.fetchedAt){const date=new Date(row.fetchedAt);if(!Number.isNaN(date.valueOf()))meta.push('Hämtat '+date.toLocaleTimeString('sv-SE',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Stockholm'}))}item.append(el('p',meta.filter(Boolean).join(' · '),'compare-meta'));
   const prices=el('dl',undefined,'compare-prices');const percent=typeof row.percent==='number'&&Number.isFinite(row.percent)?new Intl.NumberFormat('sv-SE',{maximumFractionDigits:1}).format(row.percent)+' %':'–';
   for(const [label,value]of [[row.ordinaryScope==='web'?'Ord. (webb)':'Ordinarie',row.ordinary||'–'],['Nu',row.sale||'–'],['Rabatt',percent]]){const group=el('div');group.append(el('dt',label),el('dd',value));prices.append(group)}item.append(prices);
   if(row.packSize)item.append(el('p',(row.matchedProduct?row.matchedProduct+' · ':'')+row.packSize+(row.matchKind==='family'?' · Samma sort, annan förpackning möjlig':''),'compare-meta'));
   if(row.unitPrice){const comparison=el('p',undefined,'compare-message'),unit=el('strong',row.unitPrice);comparison.append(unit);if(typeof row.unitDifferencePercent==='number'&&row.store!==data.selected)comparison.append(document.createTextNode(' · '+(row.unitDifferencePercent===0?'Samma jämförpris':new Intl.NumberFormat('sv-SE',{maximumFractionDigits:1}).format(Math.abs(row.unitDifferencePercent))+' % '+(row.unitDifferencePercent<0?'billigare':'dyrare')+' per '+row.unit+' än vald butik')));item.append(comparison)}
   if(row.message)item.append(el('p',row.message,'compare-message'));
   if(row.conditions?.length){const details=el('details');details.dataset.store=row.store;details.open=opened.has(row.store);details.append(el('summary','Prisvillkor'));for(const condition of row.conditions)details.append(el('p',condition,'compare-message'));item.append(details)}
  }else item.append(el('p',row.status==='loading'?'Kontrollerar…':row.message||'Priset är inte tillgängligt.','compare-message'));
  list.append(item)
 }
 scroll.append(list);host.append(scroll);scroll.scrollTop=oldScroll;
 if(focusKind==='scroll')scroll.focus({preventScroll:true});else if(focusStore&&focusKind){const target=[...list.children].find(x=>x.dataset.store===focusStore)?.querySelector(focusKind==='link'?'a':'summary');target?.focus({preventScroll:true})}
 const footer=el('div',undefined,'compare-footer');footer.append(el('p',(data.message||'Priser gäller samma identifierade produkt.')+' – betyder att uppgiften saknas.'));
 if(data.state==='unavailable'){const retry=el('button','Försök igen','compare-retry');retry.type='button';retry.dataset.compareRetry='';if(data.retryAfter>0){retry.disabled=true;retry.textContent='Försök igen om '+data.retryAfter+' s';state.timer=setTimeout(()=>{if(retry.isConnected){retry.disabled=false;retry.textContent='Försök igen'}},data.retryAfter*1000)}footer.append(retry)}host.append(footer)
}
async function comparisonStart(card,variant=''){
 let state=comparisonStates.get(card);if(state){state.controller?.abort();clearTimeout(state.timer)}state={controller:new AbortController(),variant};comparisonStates.set(card,state);
 card.classList.add('is-flipped');const front=card.querySelector('.offer-front'),back=card.querySelector('.offer-back');front.inert=true;front.setAttribute('aria-hidden','true');back.inert=false;back.setAttribute('aria-hidden','false');card.querySelector('[data-compare-open]').setAttribute('aria-expanded','true');back.querySelector('[data-compare-close]').focus({preventScroll:true});
 const host=card.querySelector('.compare-state');host.replaceChildren(el('p','Hämtar exakt produkt och priser…','compare-status'));
 try{const params=new URLSearchParams({store:$('store').value,offer:card.dataset.offer,variant});const response=await fetch('/api/comparisons?'+params,{method:'POST',headers:{'X-FoodSale-Request':'1'},signal:state.controller.signal,cache:'no-store'});const data=await response.json();if(comparisonStates.get(card)!==state||!card.classList.contains('is-flipped'))return;if(!response.ok){comparisonError(card,typeof data.detail==='string'?data.detail:'Jämförelsen kunde inte startas.',response.status===409);return}comparisonRender(card,data);if(!data.done)state.timer=setTimeout(()=>comparisonPoll(card,state,data.id),900)}catch(error){if(error.name!=='AbortError')comparisonError(card,'Anslutningen misslyckades. Försök igen.')}
}
async function comparisonPoll(card,state,id){
 if(comparisonStates.get(card)!==state||!card.isConnected||!card.classList.contains('is-flipped'))return;
 try{const response=await fetch('/api/comparisons/'+encodeURIComponent(id),{signal:state.controller.signal,cache:'no-store'});const data=await response.json();if(comparisonStates.get(card)!==state||!card.classList.contains('is-flipped'))return;if(!response.ok){comparisonError(card,'Jämförelsen behöver uppdateras. Försök igen.');return}comparisonRender(card,data);if(!data.done)state.timer=setTimeout(()=>comparisonPoll(card,state,id),1200)}catch(error){if(error.name!=='AbortError')comparisonError(card,'Anslutningen bröts. Försök igen.')}
}
$('grid').addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;const card=button.closest('.offer-card');if(!card)return;if(button.hasAttribute('data-compare-open'))comparisonStart(card);else if(button.hasAttribute('data-compare-close'))comparisonClose(card);else if(button.hasAttribute('data-compare-retry'))comparisonStart(card,comparisonStates.get(card)?.variant||'');else if(button.hasAttribute('data-compare-refresh')){comparisonCloseAll();load(true)}});
$('grid').addEventListener('keydown',event=>{if(event.key==='Escape'){const card=event.target.closest('.is-flipped');if(card){event.preventDefault();comparisonClose(card)}}});
for(const id of ['store','membership','expiry','sort'])$(id).addEventListener('change',comparisonCloseAll,{capture:true});
$('search').addEventListener('input',comparisonCloseAll,{capture:true});$('filters').addEventListener('submit',comparisonCloseAll,{capture:true});$('refresh').addEventListener('click',comparisonCloseAll,{capture:true});$('reset').addEventListener('click',comparisonCloseAll,{capture:true});$('more').addEventListener('click',comparisonCloseAll,{capture:true});$('all').addEventListener('click',comparisonCloseAll,{capture:true});

document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')load()});load();
</script>
</body></html>
"""
CSP = "default-src 'self'; script-src 'self' 'sha256-FVh2dXczo5qLXZusltiw0aujMjmhcULRFOnWz+qlwkY=' 'sha256-C8FywE6pgTQWkw7XVLvcSGV3dZBz9kKfttVfcQN1jAc=' 'sha256-KrziamPpHMAJ6OdsLaygbIMNix1P4wEUcjD2//iwJXw='; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"

MEMBERSHIP = {'all': 'Alla erbjudanden', 'general': 'Utan medlemskrav', 'member': 'Medlemspriser'}
EXPIRY = {'all': 'Alla slutdatum', 'today': 'Slutar idag', 'soon': 'Inom 3 dagar'}
SORTING = {'source': 'Butikens ordning', 'discount': 'Störst prissänkning (%)',
           'saving': 'Störst besparing (kr/enhet)', 'expiry': 'Slutar snart', 'name': 'Namn A–Ö'}


def store_snapshot(store_key, force=False):
    config = STORES[store_key]
    today = dt.datetime.now(STOCKHOLM).date().isoformat()
    if config['provider'] == 'lidl':
        # One shared regional offers cache, not nested five-minute caches.
        lidl_store(store_key)
        key = ('lidl', today)
        loader = lambda: fetch_lidl(store_key)
    else:
        key = (store_key, today)
        loader = lambda: (fetch_ica(store_key) if config['provider'] == 'ica' else
                          fetch_coop(store_key) if config['provider'] == 'coop' else
                          fetch_citygross() if config['provider'] == 'citygross' else fetch_offers(store_key))
    cache = ica_offer_cache if config['provider'] == 'ica' else offer_cache
    snapshot = cache.get(key, loader, force=force)
    age = (dt.datetime.now(STOCKHOLM) - dt.datetime.fromisoformat(snapshot.value['fetchedAt'])).total_seconds()
    if age >= 600:
        raise ExtractionUnavailable('De sparade priserna är för gamla. Kontrollera butikens officiella erbjudanden eller försök igen.')
    return snapshot


@asynccontextmanager
async def lifespan(application):
    yield
    # All network calls have finite timeouts; finish workers on graceful shutdown.
    comparison_service.close()
    offer_cache.close()
    ica_offer_cache.close()


app = FastAPI(title='Food Sale · Halmstad', version='2.0', lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = CSP
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    return response


@app.get('/', response_class=HTMLResponse)
async def index():
    # The shell renders without waiting for any grocery source.
    return HTMLResponse(PAGE)


@app.get('/api/leaflets/citygross')
def city_leaflets():
    """A separate, honestly labelled fallback, not a product-offers integration."""
    try:
        now = dt.datetime.now(STOCKHOLM)
        snapshot = offer_cache.get(('city-leaflets', now.date().isoformat()), fetch_city_leaflets)
        now = dt.datetime.now(STOCKHOLM)
        value = snapshot.value
        age = (now - dt.datetime.fromisoformat(value['fetchedAt'])).total_seconds()
        if age >= 600:
            raise ExtractionUnavailable('Reklambladen är för gamla.')
        leaflets = [row for row in value['leaflets'] if row['start'] <= now.date().isoformat() <= row['end']]
        message = 'Kontrollerat ' + dt.datetime.fromisoformat(value['fetchedAt']).strftime('%H:%M') + ' · svensk tid.'
        if snapshot.failed:
            message += ' Uppdateringen misslyckades; visar nyligen sparade länkar.'
        elif snapshot.stale:
            message += ' Visar sparade länkar medan en ny kontroll pågår.'
        if not leaflets:
            message += ' Inga aktuella reklamblad hittades.'
        return dict(leaflets=leaflets, fetchedAt=value['fetchedAt'], stale=snapshot.stale,
                    refreshing=snapshot.refreshing, refreshFailed=snapshot.failed, message=message)
    except Exception:
        return JSONResponse({'message': 'Reklambladen kunde inte hämtas. Öppna butikens officiella sida.'},
                            status_code=503, headers={'Retry-After': '60'})


@app.get('/healthz')
async def health():
    return {'status': 'ok'}


def offers_response(store, q, membership, expiry, sort, limit, force=False):
    config = STORES[store]
    if config['provider'] == 'official':
        return JSONResponse(dict(status='official', store=config['name'], source=config['source'],
            message='Produktlistan är inte tillgänglig här. Öppna butikens officiella erbjudanden.'))
    try:
        snapshot = store_snapshot(store, force=force)
        result = snapshot.value
        today = dt.datetime.now(STOCKHOLM).date()
        current = select_offers(result['offers'], today=today)
        selected = select_offers(current, q, MEMBERSHIP[membership], EXPIRY[expiry], SORTING[sort], today)
        shown = selected[:limit]
        message = ''
        if snapshot.failed:
            message = 'Uppdateringen misslyckades. Visar en nyligen sparad prislista; priserna kan ha ändrats.'
        elif snapshot.stale:
            message = 'Visar sparade erbjudanden medan aktuella priser hämtas i bakgrunden.'
        elif snapshot.refreshing:
            message = 'Visar de senast hämtade erbjudandena. En ny kontroll pågår i bakgrunden.'
        return JSONResponse(dict(status='ok', store=config['name'], source=config['source'],
            fetchedAt=result['fetchedAt'], stale=snapshot.stale, refreshing=snapshot.refreshing,
            refreshFailed=snapshot.failed, message=message, note=result.get('note', ''),
            total=len(current), members=sum(o['member'] for o in current),
            ending=sum(o['end'] == today.isoformat() for o in current),
            matches=len(selected), shown=len(shown), hasMore=len(selected) > len(shown),
            searchApproximate=any(o.get('search_approximate', False) for o in selected),
            cards=''.join(card(o, today) for o in shown)))
    except (ExtractionUnavailable, urllib.error.URLError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        # Source errors may contain anonymous-client keys. Never return their raw text.
        return JSONResponse(dict(status='unavailable', store=config['name'], source=config['source'],
            message='Erbjudandena kunde inte hämtas. Kontrollera butikens officiella sida eller försök igen om en stund.'),
            status_code=503, headers={'Retry-After': str(max(1, int(getattr(exc, 'retry_after', 30))))})


@app.api_route('/api/offers', methods=['GET', 'POST'])
def offers(request: Request,
           store: Literal['willys-oster', 'willys-flygstaden', 'hemkop-c', 'coop-stora', 'coop-gamletull', 'coop-flygaregatan', 'coop-furuvagen', 'coop-fyllinge', 'xtra-snostorp', 'lidl-torpaledsvagen', 'lidl-gesallgatan', 'lidl-tegelbruket', 'ica-hogskolan', 'ica-flygstaden', 'ica-vallas', 'ica-sondrum', 'ica-frosakull', 'citygross', 'tempo-backagard'] = 'willys-oster',
           q: str = Query(default='', max_length=200),
           membership: Literal['all', 'general', 'member'] = 'all',
           expiry: Literal['all', 'today', 'soon'] = 'all',
           sort: Literal['source', 'discount', 'saving', 'expiry', 'name'] = 'source',
           limit: int = Query(default=48, ge=1, le=2000)):
    force = request.method == 'POST'
    if force:
        origin = request.headers.get('origin')
        if (request.headers.get('x-foodsale-request') != '1' or
            request.headers.get('sec-fetch-site') not in (None, 'same-origin', 'none') or
            (origin and (urllib.parse.urlsplit(origin).scheme not in ('http', 'https') or
                         urllib.parse.urlsplit(origin).netloc != request.headers.get('host')))):
            raise HTTPException(status_code=403, detail='Uppdatering måste göras från appen.')
    return offers_response(store, q, membership, expiry, sort, limit, force=force)


# Exact product identities and lazy, public-price comparison.
def valid_gtin(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) not in (8, 12, 13, 14):
        return ''
    total = sum(int(char) * (3 if index % 2 == 0 else 1) for index, char in enumerate(reversed(value[:-1])))
    return value.zfill(14) if (-total) % 10 == int(value[-1]) else ''


def coop_compare_variants(records):
    result, seen = [], set()
    for row in records:
        barcode = str(row.get('externalId') or '')
        if valid_gtin(barcode) and valid_gtin(barcode) not in seen:
            seen.add(valid_gtin(barcode))
            content = row.get('content') or {}
            label = ' · '.join(filter(None, [plain_text(content.get(k)) for k in ('title', 'brand', 'amountInformation')]))
            result.append(compare_variant(barcode, label))
    return result


def compare_variant(gtin, name, sku=''):
    return dict(id='gtin:' + gtin, gtin=gtin, name=name, sku=sku) if valid_gtin(gtin) else None


def offer_key(offer):
    fields = {k: offer.get(k) for k in ('name', 'brand', 'volume', 'price', 'ordinary', 'member', 'start', 'end', 'comparison')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def cached_store_value(store):
    config = STORES[store]
    today = dt.datetime.now(STOCKHOLM).date().isoformat()
    cache = ica_offer_cache if config['provider'] == 'ica' else offer_cache
    key = ('lidl', today) if config['provider'] == 'lidl' else (store, today)
    with cache.lock:
        entry = cache.entries.get(key)
        value = entry.value if entry else None
        if value and (dt.datetime.now(STOCKHOLM) - dt.datetime.fromisoformat(value['fetchedAt'])).total_seconds() < 600:
            return value
    return None


@cached_metadata(ttl=300, max_entries=128)
def axfood_variants(base, promotion):
    if not re.fullmatch(r'\d{4,18}', promotion):
        raise ValueError('Ogiltig kampanjidentitet.')
    variants, seen = [], set()
    for page in range(10):
        data = require_mapping(public_json(base + '/axfood/rest/v2/promotions/' + promotion + '/products?' +
                                           urllib.parse.urlencode(dict(page=page, pageSize=200))), 'product variants')
        items, pages = data.get('items'), data.get('totalPages')
        if not isinstance(items, list) or type(pages) is not int or not 0 <= pages <= 10 or data.get('page') != page:
            raise ValueError('Produktvarianterna kunde inte bekräftas.')
        for product in items:
            require_mapping(product, 'variant')
            sku = product.get('code', '')
            if not re.fullmatch(r'\d{6,12}_[A-Z]{1,4}', sku) or sku in seen:
                raise ValueError('Ogiltig produktvariant.')
            seen.add(sku)
            label = ' · '.join(filter(None, [plain_text(product.get('name')), plain_text(product.get('manufacturer')),
                                               plain_text(product.get('displayVolume'))]))
            variants.append(dict(id='sku:' + sku, sku=sku, gtin='', name=label))
        if page + 1 >= pages:
            return variants
    raise ValueError('För många produktvarianter.')


@cached_metadata(ttl=300, max_entries=256)
def axfood_identity(base, sku):
    if not re.fullmatch(r'\d{6,12}_[A-Z]{1,4}', sku):
        raise ValueError('Ogiltig produktkod.')
    product = require_mapping(public_json(base + '/axfood/rest/v2/p/' + sku), 'product identity')
    if product.get('code') != sku or not valid_gtin(product.get('ean')):
        raise ValueError('Exakt streckkod kunde inte bekräftas.')
    return dict(gtin=product['ean'], sku=sku, brand=plain_text(product.get('manufacturer')), volume=plain_text(product.get('displayVolume')), name=' · '.join(filter(None, [plain_text(product.get('name')),
                plain_text(product.get('manufacturer')), plain_text(product.get('displayVolume'))])))


@cached_metadata(ttl=300, max_entries=128)
def city_catalogue(gtin, sku=''):
    if not valid_gtin(gtin):
        raise ValueError('Ogiltig streckkod.')
    citygross_metadata()
    base = 'https://www.citygross.se/api/v1/Loop54/'
    if sku and re.fullmatch(r'\d{6,12}_[A-Z]{1,4}', sku):
        data = public_json(base + 'products/' + sku + '?store=3210')
        products = data.get('items') if isinstance(data, dict) else None
    else:
        data = public_json(base + 'search?' + urllib.parse.urlencode(dict(SearchQuery=gtin, store='3210', skip=0, take=10)))
        products = require_mapping(data.get('searchResults'), 'catalogue search').get('products')
    if not isinstance(products, list):
        raise ValueError('Produktkatalogen kunde inte läsas.')
    exact = [p for p in products if isinstance(p, dict) and valid_gtin(p.get('gtin')) == valid_gtin(gtin)]
    if len(exact) != 1 or str(require_mapping(exact[0].get('productStoreDetails'), 'catalogue store').get('id')) != '3210':
        return None
    return dict(exact[0], _comparisonFetchedAt=dt.datetime.now(STOCKHOLM).isoformat())


# Each session is ours, anonymous, and scoped to one verified branch. Never use visitor cookies.
CATALOGUE_SESSIONS = {}
CATALOGUE_SESSION_LOCKS = {key: threading.Lock() for key, value in STORES.items() if value['provider'] == 'axfood'}


@cached_metadata(ttl=300, max_entries=512)
def axfood_catalogue(store_key, sku, gtin):
    import http.cookiejar
    config = STORES[store_key]
    if not valid_gtin(gtin) or not re.fullmatch(r'\d{6,12}_[A-Z]{1,4}', sku):
        raise ValueError('Ogiltig produktidentitet.')
    with CATALOGUE_SESSION_LOCKS[store_key]:
        state = CATALOGUE_SESSIONS.get(store_key)
        def read(opener, path, method='GET', csrf=''):
            headers = {'User-Agent': 'FoodSale/2.0 (public offers viewer)', 'Accept': 'application/json', 'Origin': config['base']}
            if csrf:
                headers['X-CSRF-Token'] = csrf
            request = urllib.request.Request(config['base'] + path, data=b'' if method == 'POST' else None,
                                             method=method, headers=headers)
            try:
                with opener.open(request, timeout=12) as response:
                    body = response.read(12_000_001)
                    if len(body) > 12_000_000:
                        raise ValueError('Produktkatalogens svar är för stort.')
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                from types import SimpleNamespace
                ica_require_success(SimpleNamespace(status=exc.code, headers={'retry-after': exc.headers.get('Retry-After', '')}))
        if state is None or time.monotonic() - state[0] >= 300:
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            token = read(opener, '/axfood/rest/v2/csrf-token')
            if not isinstance(token, str) or not token:
                raise ValueError('Butikssessionen kunde inte startas.')
            read(opener, '/axfood/rest/v2/store/activate?' + urllib.parse.urlencode(dict(
                storeId=config['id'], activelySelected='true', forceAsPickingStore='true')), 'POST', token)
            active = require_mapping(read(opener, '/axfood/rest/v2/store/active'), 'active store')
            if active.get('storeId') != config['id'] or active.get('name') != config['name']:
                raise ValueError('Priset kunde inte knytas till rätt butik.')
            state = (time.monotonic(), opener)
            CATALOGUE_SESSIONS[store_key] = state
        product = require_mapping(read(state[1], '/axfood/rest/v2/p/' + sku), 'catalogue product')
        if product.get('code') != sku or valid_gtin(product.get('ean')) != valid_gtin(gtin):
            raise ValueError('Katalogprodukten har en annan streckkod.')
        return dict(product, _comparisonFetchedAt=dt.datetime.now(STOCKHOLM).isoformat())


def comparison_offer_row(store, offer, fetched, ambiguous=False):
    return dict(store=store, name=STORES[store]['name'], status='price', source=STORES[store]['source'],
                ordinary='' if ambiguous else offer.get('ordinary', ''), sale=offer['price'],
                percent=None if ambiguous else offer.get('discount_percent'), member=offer.get('member', False),
                memberLabel=offer.get('member_label', STORES[store].get('member', 'Medlemspris')),
                conditions=offer.get('conditions', []), scope='Webberbjudande' if store == 'citygross' else 'Butikserbjudande',
                fetchedAt=fetched, end=offer.get('end'), observed=offer.get('observed'), message='Ordinarie pris saknas för exakt vald sort.' if ambiguous else '')


def product_price_row(store, product, fetched):
    fetched = product.get('_comparisonFetchedAt', fetched)
    if store == 'citygross':
        details = require_mapping(product.get('productStoreDetails'), 'catalogue price')
        prices = require_mapping(details.get('prices'), 'catalogue prices')
        if prices.get('activePromotion'):
            # This is the same public product, but it may not be in the weekly category.
            copy = dict(product, productStoreDetails=dict(details, p_has_current_week_only_discount=True))
            deals = normalize_citygross([copy], dt.datetime.now(STOCKHOLM).date())
            if deals:
                return comparison_offer_row(store, deals[0], fetched)
        price = require_mapping(prices.get('currentPrice'), 'current price')
        unit = {'PCE': 'st', 'KGM': 'kg', 'LTR': 'l'}.get(price.get('unit'))
        if not unit or price_amount(price.get('price')) is None:
            raise ValueError('Katalogpriset saknar enhet.')
        label = money(price['price']) + ' kr/' + unit
        regular = prices.get('hasDiscount') is False and prices.get('hasPromotion') is False
    else:
        promotions = product.get('potentialPromotions')
        if not isinstance(promotions, list):
            raise ValueError('Katalogens kampanjstatus saknas.')
        if promotions:
            try:
                deals = normalize([product], dt.datetime.now(STOCKHOLM).date())
            except ValueError:
                deals = []
            if deals:
                row = comparison_offer_row(store, dict(deals[0], member_label=STORES[store]['member']), fetched)
                row['scope'] = 'Webberbjudande'
                return row
        label, regular = clean_text(product.get('price')), not promotions
        if price_amount(product.get('priceNoUnit')) is None or product.get('priceUnit') not in ('kr/st', 'kr/kg', 'kr/l'):
            raise ValueError('Katalogpriset saknar verifierbar enhet.')
        label = money(product['priceNoUnit']) + ' kr/' + product['priceUnit'].split('/')[1]
        deposit = price_amount(re.sub(r'\s*kr$', '', clean_text(product.get('depositPrice'))))
        if deposit is not None and deposit > 0:
            label += ' +pant'
    return dict(store=store, name=STORES[store]['name'], status='price', source=STORES[store]['source'],
                ordinary=label if regular else '', sale=label, percent=0 if regular else None, member=False,
                memberLabel='', conditions=[], scope='Ordinarie webbpris' if regular else 'Webbpris', fetchedAt=fetched,
                end=None, message='' if regular else 'Rabattvillkor kunde inte bekräftas.')


# Browser fallback is serialized with ICA extraction, never a parallel Chromium per store.
PRICE_SOURCE_COOLDOWNS = {}
LIDL_COMPARISON_IDENTITIES = OrderedDict()
PRICE_SOURCE_LOCK = threading.Lock()


def public_product_nodes(markup):
    """Read standard Product JSON-LD, including graph/list wrappers, without executing content."""
    found = []
    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            kinds = value.get('@type', [])
            if 'Product' in ([kinds] if isinstance(kinds, str) else kinds):
                found.append(value)
            for key in ('@graph', 'itemListElement', 'item'):
                if key in value:
                    walk(value[key])
    for raw in re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', markup, re.I | re.S):
        try:
            walk(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return found


def node_gtins(node):
    result = set()
    for key in ('gtin', 'gtin8', 'gtin12', 'gtin13', 'gtin14'):
        values = node.get(key, [])
        for value in values if isinstance(values, list) else [values]:
            if valid_gtin(value):
                result.add(valid_gtin(value))
    return result


@cached_metadata(ttl=86400, max_entries=12)
def comparison_robots(base):
    from urllib.robotparser import RobotFileParser
    rules = RobotFileParser()
    rules.parse(read_public(base + '/robots.txt').splitlines())
    return rules


def comparison_browser_page(url, base, blocked_api=''):
    """Ordinary anonymous browser; persistent refusals open a host-wide circuit breaker."""
    if not url.startswith(base + '/') or not comparison_robots(base).can_fetch('FoodSale', url):
        raise ValueError('Källan tillåter inte denna produkthämtning.')
    if not ICA_BROWSER_LOCK.acquire(timeout=30):
        raise ExtractionUnavailable('Produkthämtningen är upptagen.')
    try:
        with PRICE_SOURCE_LOCK:
            if time.monotonic() < PRICE_SOURCE_COOLDOWNS.get(base, 0):
                raise ExtractionUnavailable('Källan nekade automatisk prisåtkomst. Försök senare.', 3600)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(timeout=15000)
            try:
                context = browser.new_context(service_workers='block')
                context.route('**/*', lambda route: route.abort() if route.request.resource_type in
                              ('image', 'font', 'media') else route.continue_())
                page = context.new_page()
                refused = []
                page.on('response', lambda response: refused.append(response.status) if
                        (response.status in (401, 403, 429) and
                         (response.url == url or (blocked_api and blocked_api in response.url))) else None)
                response = page.goto(url, wait_until='domcontentloaded', timeout=20000)
                if response is not None and response.status in (401, 403, 429):
                    with PRICE_SOURCE_LOCK:
                        PRICE_SOURCE_COOLDOWNS[base] = time.monotonic() + 3600
                    raise ExtractionUnavailable('Källan nekade automatisk prisåtkomst.', 3600)
                if response is None or response.status >= 400:
                    raise ValueError('Produktsidan är inte tillgänglig.')
                # Wait for the SPA's structured product results, not network-idle/analytics.
                for _ in range(10):
                    if refused or public_product_nodes(page.content()):
                        break
                    page.wait_for_timeout(300)
                if refused:
                    with PRICE_SOURCE_LOCK:
                        PRICE_SOURCE_COOLDOWNS[base] = time.monotonic() + 3600
                    raise ExtractionUnavailable('Källan nekade automatisk prisåtkomst.', 3600)
                final = urllib.parse.urlsplit(page.url)
                expected = urllib.parse.urlsplit(url)
                if final.netloc != expected.netloc or final.path.rstrip('/') != expected.path.rstrip('/'):
                    raise ValueError('Produktsidan bytte butik eller adress.')
                return page.content()
            finally:
                browser.close()
    finally:
        ICA_BROWSER_LOCK.release()


@cached_metadata(ttl=86400, max_entries=256)
def lidl_product_variants(path, product):
    if not isinstance(path, str) or not re.fullmatch(r'/p/[a-z0-9-]+/p' + str(product), path):
        raise ValueError('Lidls produktadress saknas.')
    base = 'https://www.lidl.se'
    url = base + path
    if not comparison_robots(base).can_fetch('FoodSale', url):
        raise ValueError('Produktsidan tillåter inte hämtning.')
    # Product metadata is normally server-rendered: avoid Chrome unless it is missing.
    markup = read_public(url)
    nodes = [n for n in public_product_nodes(markup) if str(n.get('sku')) == str(product)]
    if not nodes:
        nodes = [n for n in public_product_nodes(comparison_browser_page(url, base)) if str(n.get('sku')) == str(product)]
    variants = []
    for node in nodes:
        for gtin in sorted(node_gtins(node)):
            variant = compare_variant(gtin, plain_text(node.get('name')))
            brand = node.get('brand')
            variant['brand'] = plain_text(brand.get('name')) if isinstance(brand, dict) else plain_text(brand)
            variants.append(variant)
    with PRICE_SOURCE_LOCK:
        LIDL_COMPARISON_IDENTITIES[(path, product)] = tuple(v['gtin'] for v in variants)
        while len(LIDL_COMPARISON_IDENTITIES) > 256:
            LIDL_COMPARISON_IDENTITIES.popitem(last=False)
    return list({v['id']: v for v in variants}.values())


@cached_metadata(ttl=300, max_entries=256)
def browser_catalogue_price(store, gtin):
    config = STORES[store]
    if config['provider'] != 'ica' or not valid_gtin(gtin):
        return None
    # This is the official branch-linked online shop, not ICA's sample assortment.
    base = 'https://handlaprivatkund.ica.se'
    url = base + '/stores/' + config['id'] + '/search?' + urllib.parse.urlencode(dict(q=gtin))
    markup = comparison_browser_page(url, base, '/api/webproductpagews/')
    matches = [n for n in public_product_nodes(markup) if valid_gtin(gtin) in node_gtins(n)]
    prices = []
    for node in matches:
        offers = node.get('offers', [])
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('priceCurrency') != 'SEK':
                continue
            amount = price_amount(offer.get('price'))
            end = clean_text(offer.get('priceValidUntil'))
            if end and (not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end) or end < dt.datetime.now(STOCKHOLM).date().isoformat()):
                continue
            # A per-item price must be explicit; do not turn comparison/kg prices into pack prices.
            spec = offer.get('priceSpecification')
            if amount is not None and isinstance(spec, dict) and spec.get('unitCode') in ('C62', 'H87'):
                prices.append((amount, end))
    if len(set(prices)) != 1:
        return None
    amount, end = prices[0]
    return dict(store=store, name=config['name'], source=url, status='price', ordinary='',
                sale=money(amount) + ' kr/st', percent=None, member=False, memberLabel='', conditions=[],
                scope='Webbpris', fetchedAt=dt.datetime.now(STOCKHOLM).isoformat(), end=end or None,
                message='Offentligt webbpris. Ordinarie referenspris och medlemsvillkor saknas.')



# Product-family comparison: brand and flavour stay fixed; pack size may differ.
FAMILY_STOP = {'och', 'and', 'maxipack', 'storpack', 'multipack', 'pack', 'forpackning'}


def family_words(value, brand=''):
    value = SEARCH_MEASURE.sub(lambda m: m[0] if m[2] == '%' else ' ', search_key(value))
    words = re.findall(r'[^\W\d_]+|\d+(?:[.,]\d+)?%?', value, re.UNICODE)
    brand_words = set(re.findall(r'[^\W\d_]+', search_key(brand), re.UNICODE))
    return set(words) - FAMILY_STOP - brand_words


def comparison_brand(value):
    # These feeds mix origin countries into their manufacturer field. Strip only explicit origin segments.
    origins = {'sverige', 'spanien', 'marocko', 'danmark', 'norge', 'finland', 'tyskland', 'italien',
               'frankrike', 'polen', 'nederlanderna', 'holland', 'portugal', 'belgien', 'grekland'}
    parts = [p.strip() for p in re.split(r'[/,.;]', search_key(value)) if p.strip()]
    parts = [p for p in parts if p not in origins]
    return re.sub(r'[\W_]+', ' ', ' '.join(parts)).strip()


def same_product_family(target, name, brand):
    if not comparison_brand(target.get('brand', '')) or comparison_brand(brand) != comparison_brand(target['brand']):
        return False
    left, right = family_words(target['name'], target['brand']), family_words(name, brand)
    if not left or len(left) != len(right):
        return False
    # Full-text first. Fuzzy spelling only: no flavour, dietary or type-token omissions.
    if left == right:
        return True
    remaining = right - left
    for token in left - right:
        candidates = [v for v in remaining if len(token) >= 5 and len(v) >= 5 and OSA.distance(token, v) <= 1]
        if len(candidates) != 1:
            return False
        remaining.remove(candidates[0])
    return not remaining


def pack_measure(volume):
    text = search_key(clean_text(volume)).replace(',', '.').rstrip(' .')
    if re.search(r'\b(?:ca|cirka)\b|\d\s*[-–/]\s*\d', text):
        return None
    match = re.fullmatch(r'\s*(?:(\d+)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*(kg|g|gram|ml|cl|dl|l|liter|st|styck|stycken|pack)(?:\s*)', text)
    if not match:
        match = re.fullmatch(r'\s*(\d+)\s*-\s*pack\s*', text)
        if match:
            return float(match[1]), 'st'
        return None
    count, amount, unit = float(match[1] or 1), float(match[2]), match[3]
    factor, base = {'kg': (1, 'kg'), 'g': (.001, 'kg'), 'gram': (.001, 'kg'),
                    'ml': (.001, 'l'), 'cl': (.01, 'l'), 'dl': (.1, 'l'), 'l': (1, 'l'),
                    'liter': (1, 'l'), 'st': (1, 'st'), 'styck': (1, 'st'), 'stycken': (1, 'st'), 'pack': (1, 'st')}[unit]
    total = count * amount * factor
    return (total, base) if 0 < total < 100000 else None


def label_pack_size(label):
    if re.search(r'\b(?:ca|cirka)\b|\d\s*[-–/]\s*\d', search_key(label)):
        return ''
    pattern = r'(?<![\d.,])(?:\d+\s*[x×]\s*)?\d+(?:[.,]\d+)?\s*(?:kg|gram|g|ml|cl|dl|liter|l|stycken|styck|st)(?![a-z])'
    sizes = re.findall(pattern, search_key(label))
    return sizes[0] if len(sizes) == 1 and pack_measure(sizes[0]) else ''


def comparable_unit_price(price, volume):
    text = search_key(clean_text(price))
    if any(token in text for token in ('fran', '%', ' upp till ', '·')) or '+pant' in text.replace(' ', ''):
        return None
    # Reject composite/ranged prices, preserve volume and multibuy requirements.
    match = re.fullmatch(r'\s*(?:(\d+)\s*(?:st\s*)?for\s*)?(\d+(?:[.,]\d+)?)\s*kr(?:\s*/\s*(kg|l|st))?\s*', text)
    if not match:
        return None
    count, amount, unit = int(match[1] or 1), float(match[2].replace(',', '.')), match[3]
    if count <= 0 or not 0 <= amount < 1000000:
        return None
    if unit in ('kg', 'l'):
        return (amount, unit) if count == 1 else None
    size = pack_measure(volume)
    if size:
        return amount / count / size[0], size[1]
    # Never compare a packet as a single piece when its weight/volume is unknown.
    return None


def with_unit_price(row, volume, product='', match='exact'):
    row = dict(row, packSize=volume, matchedProduct=product, matchKind=match)
    for key in ('unitAmount', 'unit', 'unitPrice', 'unitDifferencePercent'):
        row.pop(key, None)
    unit = comparable_unit_price(row.get('sale', ''), volume)
    if unit:
        row.update(unitAmount=round(unit[0], 6), unit=unit[1], unitPrice=money(round(unit[0], 2)) + ' kr/' + unit[1])
    return row


@cached_metadata(ttl=300, max_entries=256)
def family_catalogue_search(base, name, brand):
    # Shared public search results discover products, never establish branch prices.
    if base not in ('https://www.willys.se', 'https://www.hemkop.se') or not brand:
        return []
    words = [w for w in re.findall(r'[^\W\d_]+', name, re.UNICODE) if search_key(w) in family_words(name, brand)]
    # Preserve Swedish spelling for retailer search; accent folding is only for our matcher.
    query = ' '.join(words[:4])
    if not words or len(query) > 120:
        return []
    results = []
    for search in (query, brand):
        url = base + '/axfood/rest/v2/search?' + urllib.parse.urlencode(dict(q=search, page=0, size=100))
        if not comparison_robots(base).can_fetch('FoodSale', url):
            return []
        data = require_mapping(public_json(url), 'catalogue search')
        results = data.get('results') or []
        if not isinstance(results, list):
            raise ValueError('Produktsökningen har ändrats.')
        if any(same_product_family(dict(name=name, brand=brand), plain_text(p.get('name')), plain_text(p.get('manufacturer'))) for p in results if isinstance(p, dict)):
            break
    target = dict(name=name, brand=brand)
    return [p for p in results if isinstance(p, dict) and
            same_product_family(target, plain_text(p.get('name')), plain_text(p.get('manufacturer'))) and
            pack_measure(plain_text(p.get('displayVolume')))][:6]


@cached_metadata(ttl=300, max_entries=128)
def city_family_search(name, brand):
    if not brand:
        return []
    query = ' '.join([brand] + [w for w in re.findall(r'[^\W\d_]+', name, re.UNICODE) if search_key(w) in family_words(name, brand)])
    data = require_mapping(public_json('https://www.citygross.se/api/v1/Loop54/search?' +
           urllib.parse.urlencode(dict(SearchQuery=query, store='3210', skip=0, take=30))), 'catalogue search')
    products = require_mapping(data.get('searchResults'), 'catalogue products').get('products') or []
    if not isinstance(products, list):
        raise ValueError('Produktsökningen har ändrats.')
    now = dt.datetime.now(STOCKHOLM).isoformat()
    return [dict(p, _comparisonFetchedAt=now) for p in products if isinstance(p, dict) and
            str((p.get('productStoreDetails') or {}).get('id')) == '3210' and
            same_product_family(dict(name=name, brand=brand), plain_text(p.get('name')), plain_text(p.get('brand'))) and
            valid_gtin(p.get('gtin'))][:6]


@cached_metadata(ttl=300, max_entries=128)
def browser_family_prices(store, name, brand):
    config = STORES[store]
    if config['provider'] != 'ica' or not brand:
        return []
    words = [w for w in re.findall(r'[^\W\d_]+', name, re.UNICODE) if search_key(w) in family_words(name, brand)]
    base = 'https://handlaprivatkund.ica.se'
    url = base + '/stores/' + config['id'] + '/search?' + urllib.parse.urlencode(dict(q=' '.join([brand] + words)))
    markup = comparison_browser_page(url, base, '/api/webproductpagews/')
    candidates = []
    for product in public_product_nodes(markup):
        label = plain_text(product.get('name'))
        source_brand = product.get('brand')
        source_brand = plain_text(source_brand.get('name')) if isinstance(source_brand, dict) else plain_text(source_brand)
        if not same_product_family(dict(name=name, brand=brand), label, source_brand):
            continue
        volume = plain_text(product.get('size')) or label_pack_size(label)
        offers = product.get('offers', [])
        offers = offers if isinstance(offers, list) else [offers]
        valid = []
        for offer in offers:
            if not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('priceCurrency') != 'SEK':
                continue
            amount, spec = price_amount(offer.get('price')), offer.get('priceSpecification')
            if amount is None or not isinstance(spec, dict) or spec.get('unitCode') not in ('C62', 'H87', 'KGM', 'LTR'):
                continue
            end = clean_text(offer.get('priceValidUntil'))
            if end and (not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end) or end < dt.datetime.now(STOCKHOLM).date().isoformat()):
                continue
            unit = {'C62': 'st', 'H87': 'st', 'KGM': 'kg', 'LTR': 'l'}[spec['unitCode']]
            valid.append((amount, unit, end))
        if len(set(valid)) != 1:
            continue
        amount, unit, end = valid[0]
        row = with_unit_price(dict(store=store, name=config['name'], source=url, status='price', ordinary='',
                    sale=money(amount) + ' kr/' + unit, percent=None, member=False, memberLabel='', conditions=[],
                    scope='Webbpris', fetchedAt=dt.datetime.now(STOCKHOLM).isoformat(), end=end or None,
                    message='Webbpris. Ordinarie pris och medlemsvillkor kunde inte bekräftas.'), volume, label, 'family')
        if row.get('unitPrice'):
            candidates.append(row)
    return candidates[:6]


class ComparisonService:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = OrderedDict()
        self.workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='comparison')
        self.sources = ThreadPoolExecutor(max_workers=3, thread_name_prefix='comparison-source')

    def start(self, store, offer, fetched, variant=''):
        import secrets
        key = (store, offer_key(offer), variant)
        with self.lock:
            for job in self.jobs.values():
                if job['_key'] == key and time.monotonic() - job['_created'] < (30 if job['state'] == 'unavailable' else 300):
                    return self.public(job)
            if sum(not job['done'] for job in self.jobs.values()) >= 6:
                raise HTTPException(429, 'Flera jämförelser hämtas redan. Försök igen om en stund.', headers={'Retry-After': '5'})
            while len(self.jobs) >= 32:
                expired = next((k for k, j in self.jobs.items() if j['done']), None)
                if expired is None:
                    raise HTTPException(429, 'Jämförelsen är upptagen. Försök igen.')
                self.jobs.pop(expired)
            rows = {key: dict(store=key, name=value['name'], status='loading', source=value['source']) for key, value in STORES.items()}
            rows[store] = dict(with_unit_price(comparison_offer_row(store, offer, fetched), offer.get('volume', ''), offer['name']), verified=False)
            job = dict(id=secrets.token_urlsafe(18), state='queued', done=False, product=offer['name'], variants=[],
                       message='', rows=rows, selected=store, _key=key, _created=time.monotonic())
            self.jobs[job['id']] = job
            self.workers.submit(self.run, job, store, offer, variant)
            return self.public(job)

    def public(self, job):
        import copy
        result = copy.deepcopy({k: v for k, v in job.items() if not k.startswith('_')})
        result['rows'] = [result['rows'][job['selected']]] + [v for key, v in result['rows'].items() if key != job['selected']]
        now = dt.datetime.now(STOCKHOLM)
        for row in result['rows']:
            if row['status'] == 'price':
                age = (now - dt.datetime.fromisoformat(row['fetchedAt'])).total_seconds()
                if row.get('referenceFetchedAt') and (now - dt.datetime.fromisoformat(row['referenceFetchedAt'])).total_seconds() >= 600:
                    row.update(ordinary='', percent=None, message='Ordinarie webbpriset behöver uppdateras.')
                if (age >= 600 or (row.get('end') and row['end'] < now.date().isoformat())
                        or (row.get('observed') and row['observed'] != now.date().isoformat())):
                    row.update(status='unavailable', ordinary='', sale='', percent=None, unitPrice='', unitAmount=None, message='Priset behöver uppdateras.')
                elif age >= 300:
                    row['message'] = 'Sparat pris. Kontrollera butikens aktuella villkor.'
        baseline = result['rows'][0]
        for row in result['rows']:
            if row.get('unitPrice') and baseline.get('unitAmount') and row.get('unit') == baseline.get('unit') and row['status'] == 'price' and baseline['status'] == 'price':
                row['unitDifferencePercent'] = round((row['unitAmount'] / baseline['unitAmount'] - 1) * 100, 1)
        if baseline.get('unit'):
            result['rows'][1:] = sorted(result['rows'][1:], key=lambda r: (r.get('unit') != baseline['unit'] or r['status'] != 'price', r.get('unitAmount') if r.get('unitAmount') is not None else float('inf')))
        result['retryAfter'] = max(0, int(30 - (time.monotonic() - job['_created'])) + 1) if job['state'] == 'unavailable' else 0
        result['completed'] = sum(row['status'] != 'loading' for row in result['rows'])
        result['total'] = len(STORES)
        return result

    def poll(self, identity):
        with self.lock:
            job = self.jobs.get(identity)
            if not job or time.monotonic() - job['_created'] > 600:
                raise HTTPException(404, 'Jämförelsen har gått ut. Försök igen.')
            return self.public(job)

    def run(self, job, store, offer, chosen):
        from concurrent.futures import as_completed
        try:
            with self.lock:
                job['state'] = 'loading'
            meta = offer.get('comparison') or {}
            variants = meta.get('variants', [])
            if meta.get('kind') == 'axfood':
                variants = axfood_variants(STORES[store]['base'], meta['promotion'])
            if meta.get('kind') == 'lidl':
                variants = lidl_product_variants(meta.get('path'), meta.get('product'))
            if not variants and offer.get('brand'):
                # The family can still be searched without a source barcode. Candidate identity is not called exact.
                variants = [dict(id='family', name=offer['name'], gtin='', familyOnly=True)]
            if not variants:
                raise ValueError('Produktens varumärke eller streckkod saknas hos källan.')
            if len(variants) > 1 and not chosen:
                with self.lock:
                    job.update(state='choose', done=True, variants=variants,
                               message='Välj exakt sort och förpackning för att jämföra samma produkt.')
                return
            selected = next((v for v in variants if v['id'] == chosen), None) if chosen else variants[0]
            if selected is None:
                raise ValueError('Välj en variant från erbjudandet.')
            if selected.get('sku') and not selected.get('gtin'):
                selected = dict(selected, **axfood_identity(STORES[store]['base'], selected['sku']))
            gtin, sku = selected['gtin'], selected.get('sku', '')
            if not valid_gtin(gtin) and not selected.get('familyOnly'):
                raise ValueError('Exakt streckkod kunde inte bekräftas.')
            with self.lock:
                job['product'] = selected['name']
                job['rows'][store]['verified'] = bool(valid_gtin(gtin))
                job['variants'] = variants
                job['message'] = 'Samma varumärke och sort. Olika storlekar jämförs per kg, liter eller styck. Medlemskap och mängdköp gäller.'
                if len(variants) > 1 and (meta.get('kind') != 'axfood' or sku != meta.get('sku')):
                    row = job['rows'][store]
                    row.update(ordinary='', percent=None, message='Ordinarie pris saknas för exakt vald sort.')
            # City Gross's barcode search also supplies the shared Axfood SKU when available.
            catalogue = None
            try:
                catalogue = city_catalogue(gtin, sku)
                if catalogue:
                    sku = catalogue['id']
            except Exception:
                pass
            family = dict(name=selected.get('name') or offer['name'], brand=offer.get('brand', ''), volume=offer.get('volume', ''))
            if len(variants) > 1:
                family['volume'] = selected.get('volume') or label_pack_size(selected.get('name', '')) or family['volume']
            if selected.get('brand'):
                family.update(name=selected.get('name', family['name']), brand=selected['brand'])
            if selected.get('volume'):
                family['volume'] = selected['volume']
            if catalogue and not family['brand']:
                family.update(name=plain_text(catalogue.get('name')), brand=plain_text(catalogue.get('brand')))
            family['baseUnit'] = job['rows'][store].get('unit', '')
            futures = {self.sources.submit(self.family_row, key, gtin, sku, catalogue, family): key
                       for key in STORES if key != store}
            with self.lock:
                selected_row = dict(job['rows'][store])
            futures[self.sources.submit(self.enrich_ordinary, store, selected_row, gtin, sku, catalogue)] = store
            try:
                for future in as_completed(futures, timeout=100):
                    key = futures[future]
                    try:
                        row = future.result()
                    except Exception:
                        row = self.unavailable(key, 'Priset kunde inte verifieras just nu.')
                    with self.lock:
                        job['rows'][key] = with_unit_price(row, family['volume'], family['name']) if key == store else row
            except TimeoutError:
                for future, key in futures.items():
                    if not future.done():
                        future.cancel()
                        with self.lock:
                            job['rows'][key] = self.unavailable(key, 'Hämtningen tog för lång tid.')
            with self.lock:
                job.update(state='ready', done=True)
        except Exception:
            with self.lock:
                for key, row in job['rows'].items():
                    if row['status'] == 'loading':
                        job['rows'][key] = self.unavailable(key, 'Exakt produktmatchning saknas.')
                job.update(state='unavailable', done=True,
                           message='Källans ursprungliga erbjudande visas. Exakt variant kunde inte identifieras.')
                job['rows'][store].update(scope='Originalerbjudande · exakt variant ej bekräftad', verified=False,
                                          message='Ingen prisjämförelse har gjorts för detta erbjudande.')

    def family_row(self, store, gtin, sku, catalogue, family):
        exact = None
        try:
            exact = self.store_row(store, gtin, sku, catalogue, allow_browser=False)
            if exact.get('status') == 'price' and exact.get('unitPrice'):
                return exact
        except Exception:
            pass
        if not family.get('brand'):
            return exact or self.unavailable(store, 'Varumärket kunde inte bekräftas.')
        config, candidates = STORES[store], []
        lookup_failed = False
        try:
            value = cached_store_value(store)
            if not value and config['provider'] not in ('official', 'lidl'):
                value = store_snapshot(store).value
            for offer in select_offers(value['offers'] if value else []):
                if same_product_family(family, offer['name'], offer.get('brand', '')):
                    row = with_unit_price(comparison_offer_row(store, offer, value['fetchedAt']), offer.get('volume', ''), offer['name'], 'family')
                    if row.get('unitPrice'):
                        candidates.append(row)
                # Always inspect source-enumerated variants, including when a generic match has an ambiguous pack range.
                meta = offer.get('comparison') or {}
                variants = list(meta.get('familyVariants', [])) + list(meta.get('variants', []))
                variants += [dict(name=name) for name in offer.get('variants', [])]
                seen = set()
                for variant in variants:
                    label = variant.get('name', '')
                    brand = variant.get('brand') or offer.get('brand', '')
                    volume = variant.get('volume') or label_pack_size(label) or offer.get('volume', '')
                    identity = (label, brand, volume)
                    if identity in seen or not same_product_family(family, label, brand):
                        continue
                    seen.add(identity)
                    row = with_unit_price(comparison_offer_row(store, offer, value['fetchedAt'], True), volume, label, 'family')
                    if row.get('unitPrice'):
                        candidates.append(row)
            if config['provider'] == 'axfood':
                products = family_catalogue_search(config['base'], family['name'], family['brand'])
                if not products and config['base'] == 'https://www.hemkop.se':
                    products = family_catalogue_search('https://www.willys.se', family['name'], family['brand'])
                for product in products:
                    code = product.get('code', '')
                    identity = axfood_identity(config['base'], code)
                    scoped = axfood_catalogue(store, code, identity['gtin'])
                    # Confirm metadata again in the branch-scoped response.
                    if same_product_family(family, plain_text(scoped.get('name')), plain_text(scoped.get('manufacturer'))):
                        row = with_unit_price(product_price_row(store, scoped, dt.datetime.now(STOCKHOLM).isoformat()),
                                              plain_text(scoped.get('displayVolume')), plain_text(scoped.get('name')), 'family')
                        if row.get('unitPrice'):
                            candidates.append(row)
            elif config['provider'] == 'ica' and not candidates:
                candidates.extend(browser_family_prices(store, family['name'], family['brand']))
            elif store == 'citygross':
                for product in city_family_search(family['name'], family['brand']):
                    row = with_unit_price(product_price_row(store, product, product['_comparisonFetchedAt']),
                                          plain_text(product.get('descriptiveSize')), plain_text(product.get('name')), 'family')
                    if row.get('unitPrice'):
                        candidates.append(row)
        except Exception:
            lookup_failed = True
        target = pack_measure(family.get('volume', ''))
        base_unit = target[1] if target else family.get('baseUnit', '')
        comparable = [r for r in candidates if base_unit and r.get('unit') == base_unit]
        if comparable:
            comparable.sort(key=lambda r: (r['unitAmount'], r.get('member', False)))
            best = comparable[0]
            best['message'] = 'Samma varumärke och sort. Förpackningen kan skilja sig.'
            if len(comparable) > 1:
                best['conditions'] = list(best.get('conditions', [])) + ['Även: ' + r.get('packSize', '') + ' · ' + r['sale'] + ' · ' + r['unitPrice'] + (' · ' + r.get('memberLabel', 'Medlemspris') if r.get('member') else '') for r in comparable[1:]]
            return best
        if exact and exact.get('status') == 'price':
            return exact
        if config['provider'] in ('ica', 'coop'):
            message = 'Ingen jämförbar träff i butikens aktuella erbjudanden.'
            message += ' Övriga webbpriser kunde inte hämtas.' if lookup_failed else ' Ordinarie sortiment ingår inte i erbjudandekällan.'
            return self.unavailable(store, message)
        return exact or self.unavailable(store, 'Ingen verifierad träff för samma varumärke och sort.')

    def enrich_ordinary(self, store, row, gtin, sku, catalogue):
        row = dict(row)
        if row.get('ordinary'):
            return row
        try:
            if store == 'citygross' and catalogue:
                product = catalogue
            elif STORES[store]['provider'] == 'axfood' and sku:
                product = axfood_catalogue(store, sku, gtin)
            else:
                return row
            reference = product_price_row(store, product, dt.datetime.now(STOCKHOLM).isoformat())
            if reference.get('ordinary'):
                row['ordinary'] = reference['ordinary']
                row['referenceFetchedAt'] = reference['fetchedAt']
                row['ordinaryScope'] = 'web'
                # A website reference is not silently used as an in-store reference.
                same_web_offer = row.get('scope') == 'Webberbjudande' and reference.get('sale') == row.get('sale')
                row['percent'] = reference.get('percent') if same_web_offer else None
                row['message'] = '' if same_web_offer else 'Ordinarie webbpris. Butikens referenspris är inte bekräftat.'
        except Exception:
            pass
        return row

    @staticmethod
    def unavailable(store, message):
        return dict(store=store, name=STORES[store]['name'], source=STORES[store]['source'], status='unavailable', message=message)

    def store_row(self, store, gtin, sku, catalogue, allow_browser=True):
        if not valid_gtin(gtin):
            return self.unavailable(store, 'Exakt streckkod kunde inte bekräftas.')
        config = STORES[store]
        if config['provider'] == 'official':
            return self.unavailable(store, 'Källan saknar en tillgänglig produktlista.')
        if config['provider'] == 'lidl':
            value = cached_store_value(store)
            if not value:
                return self.unavailable(store, 'Källan saknar verifierade streckkoder.')
        else:
            value = store_snapshot(store).value
        matches, variant_checks = [], 0
        for offer in select_offers(value['offers']):
            meta = offer.get('comparison') or {}
            exact = any(valid_gtin(v.get('gtin')) == valid_gtin(gtin) for v in meta.get('variants', []) if v.get('gtin'))
            if meta.get('kind') == 'lidl':
                # Only inspect details already resolved by a clicked card; never crawl the whole grid.
                identity_key = (meta.get('path'), meta.get('product'))
                known = LIDL_COMPARISON_IDENTITIES.get(identity_key, ())
                exact = valid_gtin(gtin) in known
            if meta.get('kind') == 'axfood' and sku:
                exact = sku == meta.get('sku')
                if not exact:
                    # Only use a cached, verified promotion-membership list; never match prices by name.
                    base, promotion = config['base'], meta.get('promotion', '')
                    # Promotion IDs shared with the selected product can be resolved without text matching.
                    try:
                        candidates = []
                        if variant_checks < 2 and offer['brand'] and catalogue and search_key(offer['brand']) == search_key(catalogue.get('brand', '')) and search_key(offer['name']) in search_key(catalogue.get('description') or catalogue.get('name', '')):
                            variant_checks += 1
                            candidates = axfood_variants(base, promotion)
                        exact = any(v.get('sku') == sku for v in candidates)
                    except Exception:
                        pass
            if exact:
                ambiguous = len(meta.get('variants', [])) > 1 or (meta.get('kind') == 'axfood' and sku != meta.get('sku'))
                matches.append(with_unit_price(comparison_offer_row(store, offer, value['fetchedAt'], ambiguous), offer.get('volume', ''), offer['name']))
        if matches:
            # Keep the source's general offer first; membership terms stay explicit.
            matches.sort(key=lambda row: row.get('member', False))
            row = matches[0]
            if len(matches) > 1:
                row['conditions'] = list(row['conditions']) + ['Även: ' + x['sale'] + (' (' + x['memberLabel'] + ')' if x['member'] else '') for x in matches[1:]]
            return self.enrich_ordinary(store, row, gtin, sku, catalogue)
        now = dt.datetime.now(STOCKHOLM).isoformat()
        if store == 'citygross' and catalogue:
            return with_unit_price(product_price_row(store, catalogue, now), plain_text(catalogue.get('descriptiveSize')), plain_text(catalogue.get('name')))
        if config['provider'] == 'axfood' and sku:
            product = axfood_catalogue(store, sku, gtin)
            return with_unit_price(product_price_row(store, product, now), plain_text(product.get('displayVolume')), plain_text(product.get('name')))
        if config['provider'] == 'ica' and allow_browser:
            try:
                row = browser_catalogue_price(store, gtin)
                if row:
                    return row
            except Exception:
                return self.unavailable(store, 'Webbpriset kunde inte hämtas. Källan kan neka automatisk åtkomst.')
        return self.unavailable(store, 'Inget exakt erbjudande hittades. Ordinarie pris är inte tillgängligt.')

    def close(self):
        self.workers.shutdown(wait=True, cancel_futures=True)
        self.sources.shutdown(wait=True, cancel_futures=True)


comparison_service = ComparisonService()


@app.post('/api/comparisons')
def start_comparison(request: Request, store: str = Query(max_length=40),
                     offer: str = Query(pattern=r'^[a-f0-9]{24}$'), variant: str = Query(default='', max_length=80)):
    origin = request.headers.get('origin')
    if (request.headers.get('x-foodsale-request') != '1' or
            request.headers.get('sec-fetch-site') not in (None, 'same-origin', 'none') or
            (origin and (urllib.parse.urlsplit(origin).scheme not in ('http', 'https') or
                         urllib.parse.urlsplit(origin).netloc != request.headers.get('host')))):
        raise HTTPException(403, 'Öppna jämförelsen från appen.')
    if store not in STORES:
        raise HTTPException(422, 'Okänd butik.')
    value = cached_store_value(store)
    selected = next((o for o in select_offers(value['offers']) if offer_key(o) == offer), None) if value else None
    if selected is None:
        raise HTTPException(409, 'Prislistan har ändrats. Uppdatera erbjudandena och försök igen.')
    return comparison_service.start(store, selected, value['fetchedAt'], variant)


@app.get('/api/comparisons/{identity}')
def poll_comparison(identity: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{24}', identity):
        raise HTTPException(404, 'Jämförelsen hittades inte.')
    return comparison_service.poll(identity)



def main():
    parser = argparse.ArgumentParser(description='Food Sale · Halmstad')
    parser.add_argument('--host', default=os.environ.get('HOST', '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    parser.add_argument('--no-browser', action='store_true', help='Öppna inte webbläsaren automatiskt')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Porten måste vara mellan 1 och 65535.')
    if not args.no_browser and args.host in ('127.0.0.1', 'localhost') and not os.environ.get('PORT'):
        def open_when_ready():
            url = f'http://127.0.0.1:{args.port}'
            for _ in range(100):
                try:
                    with urllib.request.urlopen(url + '/healthz', timeout=.3) as response:
                        if response.status == 200:
                            webbrowser.open(url)
                            return
                except OSError:
                    time.sleep(.1)
        threading.Thread(target=open_when_ready, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level='info', access_log=False)


if __name__ == '__main__':
    main()
