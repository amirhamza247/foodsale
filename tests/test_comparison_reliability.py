"""Offline regressions for independent offer/catalogue fallbacks and exact identities."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('comparison_reliability_app', Path(__file__).resolve().parents[1] / 'app.py')
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)
GTIN = '7340005403530'
FAMILY = dict(name='Chips Dill & Gräslök', brand='OLW', volume='275g', baseUnit='kg')

class ComparisonReliability(unittest.TestCase):
    def setUp(self):
        self.service = a.ComparisonService()
        self.now = dt.datetime.now(a.STOCKHOLM).isoformat()
    def tearDown(self):
        self.service.close()

    def test_offer_failure_does_not_disable_exact_catalogue(self):
        product = dict(name='Chips Dill & Gräslök', displayVolume='275g')
        row = dict(sale='26,50 kr/st', status='price')
        with patch.object(a, 'cached_store_value', return_value=None), patch.object(a, 'store_snapshot', side_effect=ValueError('feed unavailable')), patch.object(a, 'axfood_catalogue', return_value=product) as lookup, patch.object(a, 'product_price_row', return_value=row):
            result = self.service.store_row('willys-oster', GTIN, '100000000_ST', None)
        lookup.assert_called_once()
        self.assertEqual(result['unitPriceDisplay'], '96,36 kr/kilo')

    def test_offer_failure_does_not_disable_family_catalogue(self):
        row = a.with_unit_price(dict(sale='26,50 kr/st', status='price'), '275g')
        with patch.object(self.service, 'store_row', side_effect=ValueError('feed')), patch.object(a, 'cached_store_value', return_value=None), patch.object(a, 'store_snapshot', side_effect=ValueError('feed')), patch.object(a, 'coop_family_prices', return_value=[row]) as lookup:
            result = self.service.family_row('coop-stora', '', '', None, FAMILY)
        lookup.assert_called_once_with('coop-stora', FAMILY['name'], FAMILY['brand'])
        self.assertEqual(result['unitPriceDisplay'], '96,36 kr/kilo')

    def test_ica_exact_fallback_is_used_after_offer_miss(self):
        row = a.with_unit_price(dict(sale='26,50 kr/st', status='price'), '275g')
        empty = dict(offers=[], fetchedAt=self.now)
        with patch.object(self.service, 'store_row', return_value=dict(status='unavailable')), patch.object(a, 'cached_store_value', return_value=empty), patch.object(a, 'browser_catalogue_price', return_value=row) as exact, patch.object(a, 'browser_family_prices') as family:
            result = self.service.family_row('ica-hogskolan', GTIN, '', None, FAMILY)
        exact.assert_called_once_with('ica-hogskolan', GTIN)
        family.assert_not_called()
        self.assertEqual(result['unit'], 'kg')

    def test_exact_barcode_does_not_require_brand(self):
        row = a.with_unit_price(dict(sale='26,50 kr/st', status='price'), '275g')
        with patch.object(self.service, 'store_row', return_value=dict(status='unavailable')), patch.object(a, 'coop_family_prices', return_value=[row]) as lookup:
            result = self.service.family_row('coop-stora', GTIN, '', None, dict(FAMILY, brand=''))
        lookup.assert_called_once_with('coop-stora', '', '', GTIN)
        self.assertEqual(result['unitPriceDisplay'], '96,36 kr/kilo')

    def test_exact_gtin_can_supply_known_net_quantity(self):
        row = a.with_unit_price(dict(sale='26,50 kr/st', status='price'), '')
        self.assertEqual(row['unitScope'], 'purchase-item')
        with patch.object(self.service, 'store_row', return_value=row):
            result = self.service.family_row('willys-oster', GTIN, '', None, FAMILY)
        self.assertEqual(result['unitPriceDisplay'], '96,36 kr/kilo')
        self.assertEqual(result['quantitySource'], 'selected-exact-gtin')

    def test_incompatible_offer_quote_does_not_block_regular_catalogue(self):
        offer = dict(name=FAMILY['name'],brand='OLW',volume='',price='20 kr/st',ordinary='',member=False,
            conditions=[],eligibility='GENERAL',start=None,end=(dt.date.today()+dt.timedelta(days=2)).isoformat())
        snapshot = dict(offers=[offer],fetchedAt=self.now)
        good = a.with_unit_price(dict(sale='26,50 kr/st', status='price'), '275g')
        with patch.object(self.service, 'store_row', return_value=dict(status='unavailable')), patch.object(a,'cached_store_value',return_value=snapshot), patch.object(a,'coop_family_prices',return_value=[good]) as catalogue:
            result = self.service.family_row('coop-stora', '', '', None, FAMILY)
        catalogue.assert_called_once()
        self.assertEqual(result['unitPriceDisplay'], '96,36 kr/kilo')

    def test_city_short_title_uses_source_category_not_missing_flavour(self):
        target = dict(name='Chips Dill & Gräslök Maxipack', brand='OLW')
        product = dict(name='Dill&Gräslök', category='Chips', brand='OLW')
        self.assertTrue(a.city_family_matches(target, product))
        for changes in (dict(category='Dippmix'), dict(brand='Estrella'), dict(name='Dill'), dict(name='Dill&Gräslök Light')):
            self.assertFalse(a.city_family_matches(target, dict(product, **changes)))

    def test_coop_exact_barcode_accepts_short_title_but_not_other_item(self):
        product = dict(type='Product', availableOnline=True, ean=GTIN, name='Dill&Gräslök', manufacturerName='OLW',
            salesPriceData=dict(b2cPrice=26.5), comparativePriceData=dict(b2cPrice=96.36),
            comparativePriceUnit=dict(unit='kg'), salesUnit='Styck', packageSizeInformation='275g')
        data = dict(results=dict(items=[product], count=1))
        rows, _, _ = a.coop_catalogue_items(data, {}, 'coop-stora', self.now, GTIN)
        self.assertEqual(rows[0]['matchKind'], 'exact')
        self.assertEqual(rows[0]['unitPriceDisplay'], '96,36 kr/kilo')
        self.assertFalse(a.coop_catalogue_items(data, {}, 'coop-stora', self.now, '7310865093530')[0])

    def test_one_bad_catalogue_candidate_does_not_discard_the_next(self):
        product = dict(name=FAMILY['name'], manufacturer='OLW', displayVolume='275g')
        empty = dict(offers=[], fetchedAt=self.now)
        with patch.object(self.service,'store_row',return_value=dict(status='unavailable')), patch.object(a,'cached_store_value',return_value=empty), patch.object(a,'family_catalogue_search',return_value=[dict(code='bad'),dict(code='good')]), patch.object(a,'axfood_catalogue',side_effect=[ValueError('bad metadata'),product]), patch.object(a,'product_price_row',return_value=dict(sale='26,50 kr/st',status='price')):
            result = self.service.family_row('willys-oster','','',None,FAMILY)
        self.assertEqual(result['unitPriceDisplay'],'96,36 kr/kilo')

    def test_lidl_published_brands_enable_cross_store_family_matching(self):
        # Minimal fields captured from Lidl's official feed on 22 Nov 2026.
        products = json.loads((Path(__file__).parent/'fixtures'/'lidl-identities-2026-11-22.json').read_text())
        offers = a.normalize_lidl(products, dt.date(2026,11,22))
        cheese = next(o for o in offers if 'Hushållsost' in o['name'])
        self.assertEqual(cheese['brand'], 'Arla')
        self.assertTrue(a.same_product_family(dict(name='Hushållsost',brand='Arla'),cheese['name'],cheese['brand']))
        chips = next(o for o in offers if o['comparison']['product']==12178463)
        self.assertEqual(chips['brand'],'OLW')

    def test_lidl_identity_page_failure_still_compares_confirmed_brand(self):
        import time
        offer = dict(name=FAMILY['name'],brand='OLW',volume='275g',price='26,50 kr/st',ordinary='',member=False,
            conditions=[],eligibility='GENERAL',start=None,end=(dt.date.today()+dt.timedelta(days=1)).isoformat(),
            comparison=dict(kind='lidl',product=12178463,path='/p/chips/p12178463'))
        with patch.object(a,'lidl_product_variants',side_effect=ValueError('source page unavailable')),patch.object(a,'city_catalogue',return_value=None),patch.object(self.service,'family_row',return_value=self.service.unavailable('hemkop-c','synthetic missing'))as lookup:
            job=self.service.start('lidl-torpaledsvagen',offer,self.now)
            deadline=time.monotonic()+3
            while not job['done'] and time.monotonic()<deadline:
                time.sleep(.01);job=self.service.poll(job['id'])
        self.assertTrue(job['done']);self.assertEqual(job['state'],'ready');self.assertTrue(lookup.called)
        self.assertFalse(job['rows'][0]['verified'])

    def test_multipack_notation_does_not_change_product_family(self):
        for label in ('Chips Dill & Gräslök 2x275g','Chips Dill & Gräslök 2-pack 275g','Chips Dill & Gräslök 2p/275g'):
            self.assertTrue(a.same_product_family(FAMILY,label,'OLW'),label)
        target=dict(name='Cola Zero 33cl',brand='Brand')
        self.assertTrue(a.same_product_family(target,'Cola Zero 6-pack 33cl','Brand'))
        self.assertFalse(a.same_product_family(target,'Cola Original 6-pack 33cl','Brand'))
        self.assertFalse(a.same_product_family(dict(name='Mjölk 1,5%',brand='Arla'),'Mjölk 3% 6-pack','Arla'))
        self.assertTrue(a.same_product_family(dict(name='Mjölk 1,5%',brand='Arla'),'Mjölk 1.50 % 6-pack','Arla'))
        self.assertFalse(a.same_product_family(dict(name='Vitamin B12',brand='Brand'),'Vitamin B6 6-pack','Brand'))

    def test_explicit_dairy_fat_content_can_complete_a_product_name(self):
        name=a.family_label_from_facts('Arla Hushållsost','<ul><li>Fetthalt: 26%</li><li>Max 3 förp.</li></ul>')
        self.assertEqual(name,'Arla Hushållsost 26%')
        self.assertTrue(a.same_product_family(dict(name=name,brand='Arla'),'Hushållsost 26%','Arla'))
        self.assertFalse(a.same_product_family(dict(name=name,brand='Arla'),'Hushållsost 17%','Arla'))
        self.assertEqual(a.family_label_from_facts('Chips Dill','Fetthalt: 26%'),'Chips Dill')
        self.assertEqual(a.family_label_from_facts('Mjölk 3%','Fetthalt: 3%'),'Mjölk 3%')

    def test_weighted_catalogue_family_lookup_still_verifies_branch_and_sku(self):
        class Response:
            def __init__(self,value):self.value=value
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps(self.value).encode()[:n]
        config=a.STORES['hemkop-c']
        product=dict(code='101197477_KG',ean='2340375300000',name='Hushållsost 26%',manufacturer='Arla',displayVolume='ca: 1.1kg')
        self.assertFalse(a.valid_gtin(product['ean']))
        for wrong in (False,True):
            a.axfood_catalogue.clear();a.CATALOGUE_SESSIONS.pop('hemkop-c',None)
            active=dict(storeId='wrong' if wrong else config['id'],name=config['name'])
            with patch.object(a.urllib.request,'build_opener')as build:
                build.return_value.open.side_effect=[Response('csrf'),Response({}),Response(active),Response(product)]
                if wrong:
                    with self.assertRaises(ValueError):a.axfood_catalogue('hemkop-c',product['code'],'',family_only=True)
                else:
                    value=a.axfood_catalogue('hemkop-c',product['code'],'',family_only=True)
                    self.assertEqual(value['code'],product['code'])
        with self.assertRaises(ValueError):a.axfood_catalogue('hemkop-c',product['code'],product['ean'])
        a.axfood_catalogue.clear();a.CATALOGUE_SESSIONS.pop('hemkop-c',None)

    def test_clicked_weighted_axfood_card_can_use_family_comparison(self):
        import time
        item=dict(name='Hushållsost 26%',brand='Arla',volume='ca: 1kg',price='99 kr/kg',ordinary='',member=False,
            conditions=[],eligibility='GENERAL',start=None,end=(dt.date.today()+dt.timedelta(days=1)).isoformat(),
            comparison=dict(kind='axfood',sku='101197477_KG',promotion='123456'))
        variants=[dict(id='sku:101197477_KG',sku='101197477_KG',name='Hushållsost 26% Arla',gtin='')]
        with patch.object(a,'axfood_variants',return_value=variants),patch.object(a,'axfood_identity',side_effect=ValueError('weight code')),patch.object(a,'city_catalogue',return_value=None),patch.object(self.service,'family_row',return_value=self.service.unavailable('coop-stora','synthetic source'))as lookup:
            job=self.service.start('hemkop-c',item,self.now);deadline=time.monotonic()+3
            while not job['done'] and time.monotonic()<deadline:
                time.sleep(.01);job=self.service.poll(job['id'])
        self.assertTrue(job['done']);self.assertEqual(job['state'],'ready');self.assertFalse(job['rows'][0]['verified'])
        self.assertEqual(job['rows'][0]['unitPriceDisplay'],'99,00 kr/kilo');self.assertTrue(lookup.called)

    def test_ica_browser_quote_keeps_size_and_unit(self):
        for unit, size, price, expected in [('C62','275g',26.50,'96,36 kr/kilo'), ('KGM','',26.50,'26,50 kr/kilo'), ('LTR','',26.50,'26,50 kr/liter')]:
            a.browser_catalogue_price.clear()
            product = {'@type':'Product','gtin13':GTIN,'name':'Chips Dill & Gräslök','size':size,'offers':{'@type':'Offer','priceCurrency':'SEK','price':price,'priceSpecification':{'unitCode':unit}}}
            markup = '<script type="application/ld+json">'+json.dumps(product)+'</script>'
            with patch.object(a,'comparison_browser_page',return_value=markup):
                row = a.browser_catalogue_price('ica-hogskolan',GTIN)
            self.assertEqual(row['unitPriceDisplay'],expected)
        a.browser_catalogue_price.clear()

if __name__ == '__main__':
    unittest.main()
