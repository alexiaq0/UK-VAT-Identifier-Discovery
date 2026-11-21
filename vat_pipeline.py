#!/usr/bin/env python3
"""Veridion UK VAT open-web PoC. Python 3.9+, no VAT guessing.

LIVE: Companies House identity resolution (when API key supplied), web search,
postcode directory + provided evidence URLs, cautious candidate extraction.
DEMO: only local HTML fixtures; never evidence of real web discovery.
HMRC manual checks are read AFTER discovery and never used as search input.
"""
import argparse
import base64
import csv
import ipaddress
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parent
# Synthetic demo pages, not real-world evidence.
DEMO_HTML = {'12952353': '<html><body><h2>GB SALES LTD</h2><p>Registered company no 12952353. VAT Number: GB 424 3195 13</p>\n<p>Another company: RANDOM LIMITED VAT Number: GB123456789</p></body></html>\n', '02811195': '<html><body><ul><li>ALPHA LTD — VAT GB111222333</li><li>WARWICK BURT CONSTRUCTION LIMITED — VAT GB623619347</li><li>OMEGA LTD — VAT GB999999999</li></ul></body></html>\n', '11383949': '<html><body><p>LATHAMS HOLDINGS LIMITED. Registered address: B69 2NY. VAT Reg No GB405235432.</p></body></html>\n'}
USER_AGENT = 'VeridionStudentVATResearch/1.0 (educational PoC; respect robots.txt)'
TIMEOUT = 11
MAX_BYTES = 900_000
POSTCODE_DIRECTORY = 'www.dataindex.online'
VAT_RE = re.compile(r'(?<![A-Z0-9])(?:GB\s*[.:#-]?\s*|VAT\s*(?:REG(?:ISTRATION|ISTERED)?\s*)?(?:NUMBER|NO\.?|ID)?\s*[:#-]?\s*)(?P<digits>(?:\d[\s.\-]*){8}\d)(?!\d)', re.I)
LEGAL_RE = re.compile(r'\b(?:LTD|LIMITED|PLC|LLP)\b', re.I)
POSTCODE_RE = re.compile(r'^(GIR\s*0AA|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})$', re.I)

RESULT_FIELDS = ['sample_id','company_name','company_number','registered_postcode','identity_status','identity_source',
                 'vat_candidate','candidate_status','source_url','source_type','pages_ok','pages_blocked','search_errors',
                 'manual_hmrc_confirmed_vat','manual_hmrc_evidence','comparison_to_manual','note']
CAND_FIELDS = ['sample_id','company_name','company_number','vat_candidate','source_url','source_type',
               'evidence_text','postcode_nearby','discovery_mode','observed_at_utc','verification_status']
LOG_FIELDS = ['sample_id','company_name','stage','request_or_url','status','details']


def normalize_name(name):
    parts = re.findall(r'[A-Z0-9]+', (name or '').upper())
    if parts and parts[-1] == 'LTD': parts[-1] = 'LIMITED'
    return ' '.join(parts)


def company_re(name):
    parts = re.findall(r'[A-Z0-9]+',(name or '').upper())
    if not parts: return re.compile(r'(?!)')
    parts = ['(?:LIMITED|LTD)' if i == len(parts)-1 and p in ('LTD','LIMITED') else re.escape(p)
             for i,p in enumerate(parts)]
    return re.compile(r'(?<![A-Z0-9])' + r'[^A-Z0-9]+'.join(parts) + r'(?![A-Z0-9])')


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments=[]; self.skip=[]
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style','svg','noscript'): self.skip.append(tag)
        if tag in ('div','p','section','article','footer','h1','h2','h3','li','tr','td','br'):
            self.fragments.append(' ')
    def handle_endtag(self, tag):
        if self.skip and tag == self.skip[-1]: self.skip.pop()
        if tag in ('div','p','section','article','footer','h1','h2','h3','li','tr','td','br'):
            self.fragments.append(' ')
    def handle_data(self, text):
        if not self.skip: self.fragments.append(text)
    def get_text(self): return ' '.join(' '.join(self.fragments).split())


def html_to_text(html):
    p=PlainText();p.feed(html);return p.get_text()


def find_candidates(text, legal_name, postcode=''):
    """Use full legal name followed by a nearby explicitly VAT-labelled number.

    Treat a second corporate suffix between name and VAT as a new company's
    entry. Return evidence, not verified legal ownership.
    """
    text=' '.join(text.split())
    upper=text.upper()
    out={}
    for nm in company_re(legal_name).finditer(upper):
        end=min(len(upper),nm.end()+210)
        for match in VAT_RE.finditer(upper,nm.end(),end):
            between=upper[nm.end():match.start()]
            if LEGAL_RE.search(between): break
            # Do not reach a second distinct VAT in the same company block.
            vat=re.sub(r'\D','',match.group('digits'))
            if len(vat)!=9: continue
            context=text[max(0,nm.start()-25):min(len(text),match.end()+60)]
            near_postcode=bool(postcode and postcode.replace(' ','').upper() in context.replace(' ','').upper())
            out[vat]={'evidence_text':context[:350],'postcode_nearby':str(near_postcode).lower()}
            break
    return out


def safe_url(raw):
    """Percent-encode non-ASCII paths to avoid urllib's ASCII codec error."""
    try:
        s=urlsplit(raw.strip())
        if s.scheme not in ('http','https') or not s.hostname: return None
        host=s.hostname.lower().rstrip('.')
        if host in ('localhost','localhost.localdomain') or host.endswith(('.local','.internal')): return None
        try:
            ip=ipaddress.ip_address(host)
            if not ip.is_global: return None
        except ValueError:
            pass
        # URLs originate from untrusted search results; never preserve credentials.
        if s.username or s.password: return None
        netloc=s.netloc.encode('idna').decode('ascii')
        path=quote(s.path or '/',safe='/:%@!$&\'()*+,;=-._~')
        query=quote(s.query,safe='=&;%:@/?+,-._~!$\'()*')
        return urlunsplit((s.scheme,netloc,path,query,''))
    except (ValueError, UnicodeError):
        return None


def read_rows(path):
    with Path(path).open('r',encoding='utf-8-sig',newline='') as handle:
        return list(csv.DictReader(handle))


def write_rows(path,rows,fields):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            clean={k: ("'"+str(v) if str(v).startswith(('=','+','-','@')) else v) for k,v in row.items()}
            writer.writerow(clean)


class WebReader:
    def __init__(self,delay=1.2):
        self.cache={};self.robot_cache={};self.last={};self.delay=delay
    def robots_allowed(self,url):
        p=urlsplit(url);origin=p.scheme+'://'+p.netloc
        if origin not in self.robot_cache:
            try:
                req=Request(origin+'/robots.txt',headers={'User-Agent':USER_AGENT})
                with urlopen(req,timeout=TIMEOUT) as res:
                    robots_txt=res.read(140_000).decode('utf-8','replace')
                robots=RobotFileParser();robots.parse(robots_txt.splitlines())
                self.robot_cache[origin]=('OK',robots)
            except HTTPError as exc:
                if exc.code==404:
                    robots=RobotFileParser();robots.parse(['User-agent: *','Allow: /'])
                    self.robot_cache[origin]=('OK',robots)
                else:self.robot_cache[origin]=('ROBOTS_HTTP_'+str(exc.code),None)
            except (URLError,OSError,TimeoutError,ValueError) as exc:
                self.robot_cache[origin]=('ROBOTS_UNAVAILABLE_'+type(exc).__name__,None)
        status,robots=self.robot_cache[origin]
        if status!='OK': return status
        return 'OK' if robots.can_fetch(USER_AGENT,url) else 'ROBOTS_DISALLOWED'
    def fetch(self,raw_url):
        url=safe_url(raw_url)
        if not url:return 'INVALID_URL',''
        if url in self.cache:return self.cache[url]
        robots=self.robots_allowed(url)
        if robots!='OK': return robots,''
        origin=urlsplit(url).netloc
        pause=self.delay-(time.monotonic()-self.last.get(origin,0))
        if pause>0: time.sleep(pause)
        self.last[origin]=time.monotonic()
        try:
            req=Request(url,headers={'User-Agent':USER_AGENT,'Accept':'text/html,application/xhtml+xml'})
            with urlopen(req,timeout=TIMEOUT) as resp:
                redirected=safe_url(resp.geturl())
                if not redirected: result=('INVALID_REDIRECT','')
                elif 'html' not in resp.headers.get('Content-Type','').lower(): result=('NOT_HTML','')
                else:
                    blob=resp.read(MAX_BYTES+1)
                    if len(blob)>MAX_BYTES:result=('PAGE_TOO_LARGE','')
                    else:
                        charset=resp.headers.get_content_charset() or 'utf-8'
                        result=('OK',html_to_text(blob.decode(charset,'replace')))
        except HTTPError as exc:result=('HTTP_'+str(exc.code),'')
        except (URLError,OSError,TimeoutError,ValueError,UnicodeError) as exc:result=('FETCH_'+type(exc).__name__,'')
        self.cache[url]=result
        return result


class CompanyHouse:
    BASE='https://api.company-information.service.gov.uk'
    def __init__(self,key='',transport=None):
        self.key=key
        self.transport=transport
    def request(self,path):
        if not self.key:return 'NO_API_KEY',{}
        if self.transport:return self.transport(path)
        authorization=base64.b64encode((self.key+':').encode()).decode()
        req=Request(self.BASE+path,headers={'Authorization':'Basic '+authorization,'Accept':'application/json', 'User-Agent':USER_AGENT})
        try:
            with urlopen(req,timeout=TIMEOUT) as resp:return 'OK',json.loads(resp.read(MAX_BYTES))
        except HTTPError as exc:return 'HTTP_'+str(exc.code),{}
        except (URLError,OSError,ValueError,TimeoutError) as exc:return 'API_'+type(exc).__name__,{}
    def resolve(self,name,number='',postcode=''):
        """Never silently choose a near-name match; ambiguous names require review."""
        if not self.key:
            if number:return {'name':name,'number':number,'postcode':postcode,'address':'','status':'INPUT_NOT_API_VERIFIED','source':'INPUT_CSV'}
            return {'name':name,'number':'','postcode':postcode,'address':'','status':'NEEDS_CH_API_KEY','source':'NONE'}
        if number:
            status,item=self.request('/company/'+quote(number,safe=''))
            if status!='OK':return {'name':name,'number':number,'postcode':postcode,'address':'','status':'CH_'+status,'source':'COMPANIES_HOUSE_API'}
            if normalize_name(item.get('company_name',''))!=normalize_name(name):
                return {'name':name,'number':number,'postcode':postcode,'address':'','status':'NAME_MISMATCH_REVIEW','source':'COMPANIES_HOUSE_API'}
            address=item.get('registered_office_address') or {}
            return {'name':item.get('company_name',name),'number':number,'postcode':address.get('postal_code') or postcode,
                    'address':address.get('address_line_1','')+' '+address.get('locality',''),
                    'status':'CH_VERIFIED','source':'COMPANIES_HOUSE_API'}
        status,data=self.request('/search/companies?q='+quote_plus(name)+'&items_per_page=10')
        if status!='OK':return {'name':name,'number':'','postcode':postcode,'address':'','status':'CH_'+status,'source':'COMPANIES_HOUSE_API'}
        matches=[x for x in data.get('items',[]) if normalize_name(x.get('title',''))==normalize_name(name)]
        if postcode:
            narrowed=[x for x in matches if postcode.replace(' ','').upper() in str(x.get('address',{}).get('postal_code','')).replace(' ','').upper()]
            if narrowed:matches=narrowed
        if len(matches)!=1:
            return {'name':name,'number':'','postcode':postcode,'address':'','status':'AMBIGUOUS_COMPANY' if matches else 'NO_EXACT_CH_MATCH','source':'COMPANIES_HOUSE_API'}
        chosen=matches[0]
        return self.resolve(name,chosen.get('company_number',''),postcode)


def directory_url(postcode):
    cleaned=re.sub(r'\s+','',postcode.upper())
    if not cleaned or not POSTCODE_RE.match(cleaned):return ''
    return 'https://'+POSTCODE_DIRECTORY+'/postalcode/postcode/'+quote((cleaned[:-3]+'-'+cleaned[-3:]).lower())


def web_search_queries(name,number,postcode):
    queries=['"'+name+'" "VAT"','"'+name+'" "VAT number"']
    if number:queries.append('"'+name+'" "'+number+'"')
    if postcode:queries.append('"'+name+'" "'+postcode+'" VAT')
    return queries


class WebSearch:
    def __init__(self):
        self.ddgs=None;self.failure=''
        try:
            from ddgs import DDGS
            self.ddgs=DDGS(timeout=12)
        except (ImportError,Exception) as exc:
            self.failure=type(exc).__name__+': '+str(exc)[:120]
    def query(self,q,max_results=5):
        if self.ddgs is None:return 'SEARCH_NOT_INSTALLED',[]
        try:
            raw=self.ddgs.text(q,region='uk-en',max_results=max_results) or []
            urls=[]
            for row in raw:
                u=safe_url(row.get('href') or row.get('url') or '')
                if u and u not in urls:urls.append(u)
            return 'OK',urls
        except Exception as exc:
            return 'SEARCH_'+type(exc).__name__,[]


def run(companies,manual_rows=None,reader=None,search=None,ch=None,source_hints=None,mode='live',limit=0,output=None,
        max_pages=6,max_searches=3):
    reader=reader or WebReader()
    search=search or WebSearch()
    ch=ch or CompanyHouse('')
    manual_by_number={x['company_number'].strip():x for x in (manual_rows or []) if x.get('company_number')}
    hints_by_number={}
    for h in source_hints or []:
        if h.get('company_number') and h.get('source_url'):
            hints_by_number.setdefault(h['company_number'].strip(),[]).append(h['source_url'].strip())
    results=[]; candidates=[]; logs=[]
    if limit: companies=companies[:limit]
    stamp=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for i,company in enumerate(companies,1):
        sid=str(company.get('sample_id') or i).strip();name=(company.get('company_name') or '').strip()
        number=(company.get('company_number') or '').strip();post=(company.get('registered_postcode') or '').strip()
        if not name:continue
        info=ch.resolve(name,number,post)
        number=info['number'];post=info['postcode']
        logs.append(dict(sample_id=sid,company_name=name,stage='ENTITY_RESOLUTION',request_or_url=number or name,
                         status=info['status'],details=info['source']))
        # Identifiers must be unambiguous before assigning a number to a legal entity.
        identity_ok=info['status'] in ('INPUT_NOT_API_VERIFIED','CH_VERIFIED') and bool(number)
        urls=[];types={};seen=set();query_errors=0
        if identity_ok:
            if mode=='demo':
                if number in DEMO_HTML:
                    urls=['fixture://'+number];types[urls[0]]='LOCAL_FIXTURE'
            else:
                for u in hints_by_number.get(number,[]):
                    normalized=safe_url(u)
                    if normalized and normalized not in seen:urls.append(normalized);seen.add(normalized);types[normalized]='MANUAL_SOURCE_HINT'
                purl=directory_url(post)
                if purl and purl not in seen:urls.append(purl);seen.add(purl);types[purl]='POSTCODE_DIRECTORY'
                for q in web_search_queries(info['name'],number,post)[:max_searches]:
                    st,found=search.query(q)
                    logs.append(dict(sample_id=sid,company_name=name,stage='SEARCH',request_or_url=q,status=st,details=str(len(found))+' result URLs'))
                    if st!='OK':query_errors+=1
                    for u in found:
                        if u not in seen:urls.append(u);seen.add(u);types[u]='WEB_SEARCH'
        ok=blocked=0;found={}
        for u in urls[:max_pages]:
            if u.startswith('fixture://'):
                st='OK';page=html_to_text(DEMO_HTML[number])
            else:st,page=reader.fetch(u)
            logs.append(dict(sample_id=sid,company_name=name,stage='PAGE',request_or_url=u,status=st,details=types.get(u,'')))
            if st!='OK':blocked+=1;continue
            ok+=1
            for vat,e in find_candidates(page,info['name'],post).items():
                key=(vat,u)
                if key in found:continue
                found[key]=e
                candidates.append(dict(sample_id=sid,company_name=name,company_number=number,vat_candidate=vat,
                                       source_url=u,source_type=types[u],evidence_text=e['evidence_text'],
                                       postcode_nearby=e['postcode_nearby'],discovery_mode='DEMO_FIXTURE' if mode=='demo' else 'LIVE_WEB',
                                       observed_at_utc=stamp,verification_status='NOT_VERIFIED_BY_HMRC_API'))
        unique_vats=sorted({vat for vat,_ in found})
        if not identity_ok:flag='ENTITY_UNRESOLVED'
        elif len(unique_vats)>1:flag='MULTIPLE_CANDIDATES_REVIEW'
        elif len(unique_vats)==1:flag='CANDIDATE_UNVERIFIED'
        elif query_errors and not ok:flag='SEARCH_OR_ACCESS_FAILED'
        elif blocked:flag='ACCESS_LIMITED_NO_CANDIDATE'
        elif not ok:flag='NO_ACCESSIBLE_SOURCES'
        else:flag='NO_CANDIDATE_IN_SCANNED_SOURCES'
        manual=manual_by_number.get(number,{})
        confirmed=(manual.get('vat_number') or '').strip()
        best=unique_vats[0] if len(unique_vats)==1 else ''
        comparison='MANUAL_CHECK_EXISTS_NOT_DISCOVERED' if confirmed else 'NO_MANUAL_CHECK'
        if confirmed and best:comparison='MATCHES_INDEPENDENT_HMRC_CHECK' if best==confirmed else 'CONFLICT_REVIEW'
        r=dict(sample_id=sid,company_name=name,company_number=number,registered_postcode=post,
               identity_status=info['status'],identity_source=info['source'],vat_candidate=best,candidate_status=flag,
               source_url=next((u for (v,u) in found if v==best),'') if best else '',
               source_type=next((types[u] for (v,u) in found if v==best),'') if best else '',
               pages_ok=ok,pages_blocked=blocked,search_errors=query_errors,manual_hmrc_confirmed_vat=confirmed,
               manual_hmrc_evidence=manual.get('evidence_file',''),comparison_to_manual=comparison,
               note=('DEMO only; NOT a real web discovery' if mode=='demo' else 'No HMRC API checks were performed by this script'))
        results.append(r)
        print('{}/{} {} | {} | {}'.format(i,len(companies),name[:33].ljust(33),flag,', '.join(unique_vats) or '(none)'),flush=True)
    if output:
        output=Path(output);output.mkdir(parents=True,exist_ok=True)
        write_rows(output/'tabel_final.csv',results,RESULT_FIELDS)
        write_rows(output/'vat_candidati.csv',candidates,CAND_FIELDS)
        write_rows(output/'jurnal_audit.csv',logs,LOG_FIELDS)
        stats={'mode':mode,'run_utc':stamp,'companies_processed':len(results),
               'python_unique_candidates':sum(bool(x['vat_candidate']) for x in results),
               'multiple_candidates':sum(x['candidate_status']=='MULTIPLE_CANDIDATES_REVIEW' for x in results),
               'independent_manual_hmrc_confirmations':sum(bool(x['manual_hmrc_confirmed_vat']) for x in results),
               'hmrc_api_checks_performed':0,
               'status_counts':{status:sum(x['candidate_status']==status for x in results) for status in sorted({x['candidate_status'] for x in results})}}
        (output/'sumar_experiment.json').write_text(json.dumps(stats,indent=2,ensure_ascii=False),encoding='utf-8')
        print('\nPython candidates: {} / {} (NOT checked by HMRC API)'.format(stats['python_unique_candidates'],len(results)))
        print('Independent prior HMRC confirmations: {}'.format(stats['independent_manual_hmrc_confirmations']))
        print('Saved CSV and JSON in: {}'.format(output.resolve()))
    return results,candidates,logs


def main():
    p=argparse.ArgumentParser(description='Combined name -> Companies House -> postcode -> web VAT candidate PoC')
    p.add_argument('--demo',action='store_true',help='Use local fixture pages; no live web requests')
    p.add_argument('--live',action='store_true',help='Search public internet (real sources)')
    p.add_argument('--input',type=Path,default=ROOT/'companies.csv',help='CSV with company_name, optional company_number/postcode')
    p.add_argument('--limit',type=int,default=0,help='Run only the first N companies')
    p.add_argument('--max-pages',type=int,default=6)
    p.add_argument('--max-searches',type=int,default=3)
    p.add_argument('--out',type=Path,default=None)
    args=p.parse_args()
    if args.demo==args.live:p.error('Choose either --demo or --live')
    if args.limit<0 or args.max_pages<1 or args.max_searches<0:p.error('Invalid limits')
    if not args.input.exists():p.error('No input file: '+str(args.input))
    companies=read_rows(args.input)
    manual=read_rows(ROOT/'hmrc_confirmed_manual.csv') if (ROOT/'hmrc_confirmed_manual.csv').exists() else []
    hint_file=ROOT/'source_hints.csv'
    hints=read_rows(hint_file) if hint_file.exists() else []
    out=args.out or ROOT/('rezultate_demo' if args.demo else 'rezultate_live')
    try:
        run(companies,manual_rows=manual,ch=CompanyHouse('' if args.demo else os.environ.get('COMPANIES_HOUSE_API_KEY','')),
            source_hints=hints,mode='demo' if args.demo else 'live',limit=args.limit,
            output=out,max_pages=args.max_pages,max_searches=args.max_searches)
    except KeyboardInterrupt:
        print('Interrupted: experiment not completed',file=sys.stderr)
        sys.exit(130)


if __name__=='__main__':main()
