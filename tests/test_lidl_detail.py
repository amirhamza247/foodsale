"""Official, server-rendered Lidl data captured 22 Nov 2026; no browser or network."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sp=importlib.util.spec_from_file_location('lidl_detail_app',Path(__file__).resolve().parents[1]/'app.py')
a=importlib.util.module_from_spec(sp);sys.modules[sp.name]=a;sp.loader.exec_module(a)
FIXTURE=Path(__file__).parent/'fixtures'/'lidl-product-12178463-2026-11-22.json'
class LidlDetail(unittest.TestCase):
    def setUp(self):
        self.table=json.loads(FIXTURE.read_text())
        self.markup='<script type="application/json" data-nuxt-data="pdp-view" id="__NUXT_DATA__">'+json.dumps(self.table)+'</script>'
        self.raw=a.lidl_page_product(self.markup,12178463)
        self.today=dt.date(2026,11,22)
    def test_real_product_price_and_price_expiry_override_store_availability(self):
        self.assertEqual(self.raw['eans'],['7340005407439'])
        offers=a.lidl_detail_offers(self.raw,self.today)
        self.assertEqual(a.offer_unit_quote(offers[0])['unitPriceDisplay'],'79,73 kr/kilo')
        self.assertEqual(offers[0]['end'],'2026-11-22')
        self.assertFalse(a.lidl_detail_offers(self.raw,dt.date(2026,11,23)))
    def test_all_three_halmstad_prices_must_be_equal(self):
        broken=copy.deepcopy(self.raw)
        region=next(v for v in broken['regionsV2'].values()if v['regionName']=='Halmstad 3')
        original=broken['regionsPrices'][region['regionPriceId']]
        broken['regionsPrices']['different']=copy.deepcopy(original)
        region['regionPriceId']='different';broken['regionsPrices']['different']['currentPrice']['price']=100
        with self.assertRaises(ValueError):a.lidl_detail_offers(broken,self.today)
        broken=copy.deepcopy(self.raw);broken['storeFacts']['retail']=False
        self.assertFalse(a.lidl_detail_offers(broken,self.today))
    def test_quote_checks_identity_and_keeps_product_locator(self):
        value=dict(product=self.raw,fetchedAt='2026-11-22T12:00:00+01:00')
        locator=dict(path='/p/chips-dill-graslok-maxipack/p12178463',product=12178463)
        family=dict(name='Chips Dill & Gräslök',brand='OLW',baseUnit='kg')
        with patch.object(a,'lidl_detail_product',return_value=value):
            row=a.lidl_detail_quote('lidl-gesallgatan',locator,'7340005407439',family,self.today)
            self.assertEqual(row['unitPriceDisplay'],'79,73 kr/kilo')
            self.assertEqual(row['catalogueLocator']['branch'],a.STORES['lidl-gesallgatan']['address'])
            self.assertEqual(row['matchKind'],'exact')
            self.assertIsNone(a.lidl_detail_quote('lidl-gesallgatan',locator,'',dict(family,brand='Estrella'),self.today))
            self.assertIsNone(a.lidl_detail_quote('lidl-gesallgatan',locator,'',dict(family,name='Chips Grill'),self.today))
    def test_parser_rejects_cycles_unknown_shapes_and_wrong_product(self):
        with self.assertRaises(ValueError):a.lidl_page_product(self.markup,9999)
        broken=copy.deepcopy(self.table);broken[4]['cycle']=4
        markup='<script id="__NUXT_DATA__">'+json.dumps(broken)+'</script>'
        with self.assertRaises(ValueError):a.lidl_page_product(markup,12178463)
        with self.assertRaises(ValueError):a.lidl_page_product('<script id="__NUXT_DATA__">{}</script>',12178463)
    def test_source_page_brand_can_be_used_without_a_browser(self):
        a.lidl_detail_product.clear()
        node={'@type':'Product','sku':'12178463','brand':{'name':'OLW'}}
        markup=self.markup+'<script type="application/ld+json">'+json.dumps(node)+'</script>'
        with patch.object(a,'read_public',return_value=markup),patch.object(a,'comparison_robots')as robots:
            robots.return_value.can_fetch.return_value=True
            value=a.lidl_detail_product('/p/chips-dill-graslok-maxipack/p12178463',12178463)
            self.assertEqual(value['product']['brand']['name'],'OLW')
        a.lidl_detail_product.clear()
if __name__=='__main__':unittest.main()
