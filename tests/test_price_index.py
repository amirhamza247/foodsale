"""Temporary-database tests; synthetic public-price rows never reach the app's real index."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('price_index_app',Path(__file__).resolve().parents[1]/'app.py')
a=importlib.util.module_from_spec(spec);sys.modules[spec.name]=a;spec.loader.exec_module(a)
GTIN='7340005403530'

class PriceIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=str(Path(self.directory.name)/'prices.sqlite3')
        self.index=a.PriceIndex(self.path)
        self.now=dt.datetime.now(a.STOCKHOLM)
        self.offer=dict(name='Chips Dill & Gräslök',brand='OLW',volume='275g',price='26,50 kr/st',ordinary='',member=False,
            eligibility='GENERAL',conditions=[],start=None,end=(self.now.date()+dt.timedelta(days=1)).isoformat(),
            comparison=dict(kind='gtin',variants=[dict(id='one',gtin=GTIN,name='Chips Dill & Gräslök',brand='OLW',volume='275g')]))
    def tearDown(self):self.directory.cleanup()
    def row(self,store,age=0):
        return a.with_unit_price(dict(store=store,name=a.STORES[store]['name'],source=a.STORES[store]['source'],status='price',
            sale='26,50 kr/st',ordinary='',member=False,conditions=[],scope='Synthetic test price',
            fetchedAt=(self.now-dt.timedelta(seconds=age)).isoformat(),end=self.offer['end']), '275g','Chips Dill & Gräslök')
    def wait(self,service,job):
        deadline=time.monotonic()+4
        while not job['done'] and time.monotonic()<deadline:
            time.sleep(.01);job=service.poll(job['id'])
        self.assertTrue(job['done']);return job
    def context(self):
        family=dict(name=self.offer['name'],brand='OLW',volume='275g',baseUnit='kg',unitScope='physical-unit')
        return dict(gtin=GTIN,sku='',family=family,variants=self.offer['comparison']['variants'],product=self.offer['name'],resolvedAt=time.time())
    def seed(self,age=0):
        context=self.context()
        self.index.put(a.index_offer_key('willys-oster',self.offer,''),context)
        row=self.row('hemkop-c',age)
        self.index.put(a.index_product_key(GTIN,context['family']),{'hemkop-c':dict(row=row,observed=dt.datetime.fromisoformat(row['fetchedAt']).timestamp())},merge=True)

    def test_disk_restart_and_newer_source_time_wins(self):
        self.index.put('prices:test',{'a':dict(observed=20,row={'value':'new'})},merge=True)
        other=a.PriceIndex(self.path)
        other.put('prices:test',{'a':dict(observed=10,row={'value':'old'}),'b':dict(observed=15,row={})},merge=True)
        saved=a.PriceIndex(self.path).get('prices:test')
        self.assertEqual(saved['a']['row']['value'],'new');self.assertIn('b',saved)

    def test_concurrent_merges_do_not_lose_other_stores(self):
        threads=[threading.Thread(target=a.PriceIndex(self.path).put,args=('prices:concurrent',{str(i):dict(observed=i,row={})}),kwargs={'merge':True})for i in range(12)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len(self.index.get('prices:concurrent')),12)

    def test_expiry_bound_and_backend_failure(self):
        self.index.put('expired',{'value':1},ttl=-1);self.assertIsNone(self.index.get('expired'))
        self.index.LIMIT=3
        for i in range(8):self.index.put(str(i),{'value':i})
        self.assertIsNone(self.index.get('0'));self.assertEqual(self.index.get('7'),{'value':7})
        broken=a.PriceIndex(str(Path(self.directory.name)/'bad'/'index'))
        with patch.object(broken,'_connection',side_effect=OSError('unwritable'))as connect:
            self.assertIsNone(broken.get('a'));self.assertIsNone(broken.get('b'));self.assertEqual(connect.call_count,1)
        self.assertNotIn('unwritable',broken.last_error)

    def test_restart_warm_job_skips_source_reads_and_preserves_selected_price(self):
        first=a.ComparisonService(self.index)
        try:
            with patch.object(a,'city_catalogue',return_value=None),patch.object(first,'family_row',side_effect=lambda store,*args:self.row(store))as fetch:
                result=self.wait(first,first.start('willys-oster',self.offer,self.now.isoformat()))
                self.assertEqual(fetch.call_count,len(a.STORES)-1)
        finally:first.close()
        second=a.ComparisonService(a.PriceIndex(self.path))
        try:
            with patch.object(a,'city_catalogue',side_effect=AssertionError('rediscovery')),patch.object(second,'family_row',side_effect=AssertionError('unnecessary price fetch'))as fetch:
                result=self.wait(second,second.start('willys-oster',dict(self.offer,price='25,00 kr/st'),self.now.isoformat()))
                fetch.assert_not_called()
            self.assertEqual(result['rows'][0]['unitPriceDisplay'],'90,91 kr/kilo')
            self.assertTrue(all(r.get('indexCached')for r in result['rows'][1:]))
            self.assertTrue(all(r['fetchedAt']==self.now.isoformat()for r in result['rows']))
        finally:second.close()

    def test_separate_python_process_reads_saved_prices(self):
        import subprocess
        context=self.context()
        self.index.put(a.index_offer_key('willys-oster',self.offer,''),context)
        self.index.put(a.index_product_key(GTIN,context['family']),{key:dict(row=self.row(key),observed=self.now.timestamp())for key in a.STORES},merge=True)
        code="""
import importlib.util,json,sys,time
from unittest.mock import patch
sp=importlib.util.spec_from_file_location('separate_app',sys.argv[1]);m=importlib.util.module_from_spec(sp);sys.modules[sp.name]=m;sp.loader.exec_module(m)
service=m.ComparisonService(m.PriceIndex(sys.argv[2]));offer=json.loads(sys.argv[3]);calls=[]
def fail(*args,**kwargs):calls.append(1);raise AssertionError('unexpected source access')
service.family_row=fail;m.city_catalogue=fail
try:
 job=service.start('willys-oster',offer,sys.argv[4]);deadline=time.monotonic()+3
 while not job['done'] and time.monotonic()<deadline:
  time.sleep(.01);job=service.poll(job['id'])
 assert job['done'] and all(r['status']=='price' and r.get('unitPrice')for r in job['rows'])
 assert not calls
 print(json.dumps(dict(rows=len(job['rows']),sourceCalls=len(calls))))
finally:service.close()
"""
        result=subprocess.run([sys.executable,'-c',code,str(Path(a.__file__).resolve()),self.path,json.dumps(self.offer),self.now.isoformat()],capture_output=True,text=True,timeout=10,check=True)
        self.assertEqual(json.loads(result.stdout),dict(rows=19,sourceCalls=0))

    def test_stale_quote_visible_during_refresh_and_failure_keeps_timestamp(self):
        self.seed(age=350);service=a.ComparisonService(self.index);gate=threading.Event();entered=threading.Event()
        def fetch(store,*args):
            if store=='hemkop-c':entered.set();gate.wait(3)
            return service.unavailable(store,'synthetic source unavailable')
        try:
            with patch.object(service,'family_row',side_effect=fetch):
                job=service.start('willys-oster',self.offer,self.now.isoformat());self.assertTrue(entered.wait(2))
                live=service.poll(job['id']);row=next(r for r in live['rows']if r['store']=='hemkop-c')
                self.assertEqual(row['status'],'price');self.assertTrue(row['refreshing']);self.assertIn('Sparat pris',row['priceTerms'])
                original=row['fetchedAt'];gate.set();result=self.wait(service,live)
            row=next(r for r in result['rows']if r['store']=='hemkop-c')
            self.assertTrue(row['refreshFailed']);self.assertEqual(row['fetchedAt'],original)
            saved=self.index.get(a.index_product_key(GTIN,self.context()['family']))['hemkop-c']['row']
            self.assertEqual(saved['fetchedAt'],original)
        finally:gate.set();service.close()

    def test_stored_locator_refreshes_directly_without_rediscovery(self):
        service=a.ComparisonService(self.index)
        locator=dict(provider='axfood',branch=a.STORES['hemkop-c']['id'],sku='100000000_ST',gtin=GTIN)
        entry=dict(row=dict(self.row('hemkop-c',700),catalogueLocator=locator))
        product=dict(name=self.offer['name'],manufacturer='OLW',displayVolume='275g')
        try:
            with patch.object(a,'axfood_catalogue',return_value=product)as lookup,patch.object(a,'product_price_row',return_value=self.row('hemkop-c')),patch.object(service,'family_row')as discovery:
                row=service.indexed_family_row('hemkop-c',GTIN,'',None,self.context()['family'],entry)
                self.assertEqual(row['unitPriceDisplay'],'96,36 kr/kilo');discovery.assert_not_called();lookup.assert_called_once()
            wrong=dict(row=dict(entry['row'],catalogueLocator=dict(locator,branch='wrong-branch')))
            with patch.object(a,'axfood_catalogue')as lookup,patch.object(service,'family_row',return_value={})as discovery:
                service.indexed_family_row('hemkop-c',GTIN,'',None,self.context()['family'],wrong)
                lookup.assert_not_called();discovery.assert_called_once()
        finally:service.close()

    def test_recent_negative_status_is_short_lived(self):
        self.seed();context=self.context();negative=dict(row=dict(store='coop-stora',status='unavailable',name=a.STORES['coop-stora']['name'],source=a.STORES['coop-stora']['source'],message='No verified public price'),observed=time.time())
        self.index.put(a.index_product_key(GTIN,context['family']),{'coop-stora':negative},merge=True)
        service=a.ComparisonService(self.index);calls=[]
        try:
            with patch.object(service,'family_row',side_effect=lambda store,*args:(calls.append(store)or service.unavailable(store,'unavailable'))):
                self.wait(service,service.start('willys-oster',self.offer,self.now.isoformat()))
            self.assertNotIn('coop-stora',calls)
        finally:service.close()
        negative['observed']=time.time()-31
        # Use a new product key's isolated snapshot, rather than overwrite a newer source observation.
        key=a.index_product_key(GTIN,context['family']);self.index.put(key,{'coop-stora':negative})
        service=a.ComparisonService(self.index);calls=[]
        try:
            with patch.object(service,'family_row',side_effect=lambda store,*args:(calls.append(store)or service.unavailable(store,'unavailable'))):
                self.wait(service,service.start('willys-oster',self.offer,self.now.isoformat()))
            self.assertIn('coop-stora',calls)
        finally:service.close()

    def test_cached_formatted_number_is_recomputed(self):
        self.seed();context=self.context();row=dict(self.row('hemkop-c'),unitAmount=1,unitPrice='1,00 kr/kg',unitPriceDisplay='1,00 kr/kilo',quantitySource='selected-exact-gtin')
        self.index.put(a.index_product_key(GTIN,context['family']),{'hemkop-c':dict(row=row,observed=self.now.timestamp())})
        service=a.ComparisonService(self.index)
        try:
            with patch.object(service,'family_row',side_effect=lambda store,*args:service.unavailable(store,'synthetic unavailable')):
                result=self.wait(service,service.start('willys-oster',self.offer,self.now.isoformat()))
            cached=next(r for r in result['rows']if r['store']=='hemkop-c')
            self.assertEqual(cached['unitPriceDisplay'],'96,36 kr/kilo')
            self.assertIn('samma streckkod',cached['unitBasis'])
        finally:service.close()

    def test_hard_age_expiry_future_date_and_invalid_context(self):
        self.assertTrue(a.indexed_row_usable(self.row('hemkop-c',599),self.now))
        self.assertFalse(a.indexed_row_usable(self.row('hemkop-c',600),self.now))
        self.assertFalse(a.indexed_row_usable(self.row('hemkop-c',-10),self.now))
        self.assertFalse(a.indexed_row_usable(dict(self.row('hemkop-c'),end='2000-01-01'),self.now))
        for value in ({},{'family':{},'variants':[]},dict(self.context(),resolvedAt='wrong')):
            self.assertFalse(a.indexed_context_usable(value))

    def test_identity_key_ignores_price_but_not_variant_or_campaign(self):
        key=a.index_offer_key('willys-oster',self.offer,'')
        self.assertEqual(key,a.index_offer_key('willys-oster',dict(self.offer,price='20 kr'),''))
        self.assertNotEqual(key,a.index_offer_key('hemkop-c',self.offer,''))
        self.assertNotEqual(key,a.index_offer_key('willys-oster',self.offer,'other'))
        self.assertNotEqual(key,a.index_offer_key('willys-oster',dict(self.offer,end='2099-01-01'),''))

    def test_source_refusal_pause_survives_restart_without_another_browser(self):
        base='https://handlaprivatkund.ica.se'
        previous=dict(a.PRICE_SOURCE_COOLDOWNS)
        try:
            with patch.object(a,'price_index',self.index):a.record_price_refusal(base)
            a.PRICE_SOURCE_COOLDOWNS.clear()
            with patch.object(a,'price_index',a.PriceIndex(self.path)),patch.object(a,'comparison_robots')as robots:
                robots.return_value.can_fetch.return_value=True
                with self.assertRaises(a.ExtractionUnavailable):a.comparison_browser_page(base+'/stores/1003925/search?q=test',base)
            self.assertGreater(a.PRICE_SOURCE_COOLDOWNS[base],time.monotonic())
        finally:a.PRICE_SOURCE_COOLDOWNS.clear();a.PRICE_SOURCE_COOLDOWNS.update(previous)

    def test_hosted_files_are_not_assumed_durable(self):
        with patch.dict(a.os.environ,{'VERCEL':'1'},clear=True):self.assertEqual(a.PriceIndex.configured().mode,'disabled')
        with patch.dict(a.os.environ,{'PORT':'8000','FOODSALE_PRICE_INDEX_PATH':self.path},clear=True):self.assertEqual(a.PriceIndex.configured().mode,'local')
        with self.assertRaises(ValueError):a.PriceIndex(url='https://evil.example/',token='fixture')

    def test_redis_rest_command_contract_and_circuit_breaker(self):
        index=a.PriceIndex(url='https://fixture.upstash.io',token='test-only')
        with patch.object(index,'_redis',return_value=json.dumps({'value':1}))as redis:
            self.assertEqual(index.get('identity:test'),{'value':1})
            self.assertEqual(redis.call_args.args[0][0],'GET')
        with patch.object(index,'_redis',return_value=1)as redis:
            index.put('prices:test',{'a':dict(observed=1,row={})},merge=True)
            command=redis.call_args.args[0];self.assertEqual(command[0],'EVAL');self.assertEqual(command[2],2);self.assertIn('merge',command)
        with patch.object(index,'_redis',side_effect=OSError('token secret should not leak'))as redis:
            self.assertIsNone(index.get('x'));self.assertIsNone(index.get('y'));self.assertEqual(redis.call_count,1)
            self.assertNotIn('secret',index.last_error)

if __name__=='__main__':unittest.main()
