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
        with patch.object(self.service,'store_row',return_value=dict(status='unavailable')), patch.object(a,'cached_store_value',return_value=empty), patch.object(a,'family_catalogue_search',return_value=[dict(code='bad'),dict(code='good')]), patch.object(a,'axfood_identity',side_effect=[ValueError('bad metadata'),dict(gtin=GTIN)]), patch.object(a,'axfood_catalogue',return_value=product), patch.object(a,'product_price_row',return_value=dict(sale='26,50 kr/st',status='price')):
            result = self.service.family_row('willys-oster','','',None,FAMILY)
        self.assertEqual(result['unitPriceDisplay'],'96,36 kr/kilo')

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
