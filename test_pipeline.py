import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import vat_pipeline as v

class TestPipeline(unittest.TestCase):
    def test_vat_next_to_exact_name(self):
        text='ALPHA LTD VAT GB111111111. GB SALES LTD Registered here VAT Number: GB 424 3195 13. OMEGA LTD VAT GB987654321'
        found=v.find_candidates(text,'GB SALES LTD')
        self.assertEqual(list(found),['424319513'])
    def test_wrong_company_never_accepted(self):
        text='SOME OTHER COMPANY LIMITED VAT number: GB623619347'
        self.assertEqual(v.find_candidates(text,'WARWICK BURT CONSTRUCTION LIMITED'),{})
    def test_multiple_entries_never_inherit_number(self):
        text='GB SALES LTD. registered at 12 Some Street. OTHER LIMITED VAT GB111222333'
        self.assertEqual(v.find_candidates(text,'GB SALES LTD'),{})
    def test_name_ltd_limited_equivalence(self):
        self.assertEqual(list(v.find_candidates('WIDGETS LIMITED VAT GB123456789','Widgets Ltd')),['123456789'])
    def test_no_unlabelled_nine_digits(self):
        self.assertEqual(v.find_candidates('GB SALES LTD company reference 123456789','GB SALES LTD'),{})
    def test_accented_url_is_encoded(self):
        self.assertEqual(v.safe_url('https://example.org/număr/înregistrare?x=ă'),
                         'https://example.org/num%C4%83r/%C3%AEnregistrare?x=%C4%83')
    def test_local_ip_not_fetched(self):
        self.assertIsNone(v.safe_url('http://127.0.0.1/private'))
    def test_company_name_resolution_ch(self):
        def fake(path):
            if path.startswith('/search/companies'):
                return 'OK',{'items':[{'title':'EXAMPLE LTD','company_number':'01234567'}]}
            if path=='/company/01234567':
                return 'OK',{'company_name':'EXAMPLE LIMITED','registered_office_address':{'postal_code':'SW1A 1AA','address_line_1':'Street'}}
            raise AssertionError(path)
        result=v.CompanyHouse('dummy',transport=fake).resolve('Example Ltd')
        self.assertEqual(result['number'],'01234567')
        self.assertEqual(result['postcode'],'SW1A 1AA')
        self.assertEqual(result['status'],'CH_VERIFIED')
    def test_ambiguous_names_not_assigned(self):
        def fake(path):return 'OK',{'items':[{'title':'TEST LTD','company_number':'00000001'}, {'title':'TEST LTD','company_number':'00000002'}]}
        result=v.CompanyHouse('dummy',transport=fake).resolve('Test Ltd')
        self.assertEqual(result['status'],'AMBIGUOUS_COMPANY')
        self.assertEqual(result['number'],'')
    def test_bad_company_number_name_mismatch(self):
        def fake(path):return 'OK',{'company_name':'DIFFERENT LTD','registered_office_address':{}}
        result=v.CompanyHouse('dummy',transport=fake).resolve('Test Ltd','00123456')
        self.assertEqual(result['status'],'NAME_MISMATCH_REVIEW')
    def test_missing_key_name_only(self):
        res=v.CompanyHouse('').resolve('Example Ltd')
        self.assertEqual(res['status'],'NEEDS_CH_API_KEY')
    def test_demo_is_explicit_and_prior_manual_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            data=[{'sample_id':'8','company_name':'GB SALES LTD','company_number':'12952353','registered_postcode':'M20 2DW'}]
            manuals=[{'company_number':'12952353','vat_number':'424319513','evidence_file':'hmrc.png'}]
            results, candidates, logs=v.run(data,manual_rows=manuals,ch=v.CompanyHouse(''),mode='demo',output=Path(directory))
            self.assertEqual(results[0]['vat_candidate'],'424319513')
            self.assertEqual(results[0]['comparison_to_manual'],'MATCHES_INDEPENDENT_HMRC_CHECK')
            self.assertEqual(candidates[0]['discovery_mode'],'DEMO_FIXTURE')
            with (Path(directory)/'sumar_experiment.json').open(encoding='utf-8') as f:stat=json.load(f)
            self.assertEqual(stat['hmrc_api_checks_performed'],0)
    def test_live_no_candidate_without_source(self):
        class FakeSearch:
            def query(self,q,max_results=5):return 'OK',[]
        class FakeReader:
            def fetch(self,u):return 'ROBOTS_DISALLOWED',''
        with tempfile.TemporaryDirectory() as directory:
            data=[{'sample_id':'1','company_name':'TEST LTD','company_number':'00000001','registered_postcode':'SW1A 1AA'}]
            r,c,logs=v.run(data,manual_rows=[],ch=v.CompanyHouse(''),search=FakeSearch(),reader=FakeReader(),
                           mode='live',output=Path(directory))
            self.assertEqual(r[0]['candidate_status'],'ACCESS_LIMITED_NO_CANDIDATE')
            self.assertEqual(c,[])

if __name__=='__main__':unittest.main()
