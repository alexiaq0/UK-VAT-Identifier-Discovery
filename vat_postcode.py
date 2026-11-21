#!/usr/bin/env python3
"""UK VAT postcode-directory experiment (Python 3.9+, standard library only).

Input: legal name, Companies House number, and known postcode(s).
Output: source-backed VAT *candidates*, NEVER HMRC-verified by this script.
No VAT numbers are seeded into the live lookup.

This deliberately tests a DIFFERENT source-discovery strategy from ddgs:
read a known directory's page for the company postcode, extract the number
appearing immediately after that company's full name, and preserve the URL.
The postcode strategy is not a solution for firms with no known address and
may miss old addresses, other VAT registration addresses and group VAT.
"""
from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parent
UA = 'Veridion-UK-VAT-PoC/1.0 (educational, robots-aware research)'
DOMAIN = 'www.dataindex.online'
MAX_BYTES = 2_000_000
TIMEOUT = 15


class VisibleHTML(HTMLParser):
    SEPARATORS = {'div','p','li','tr','td','br','h1','h2','h3','h4','section','article','footer'}
    SKIP = {'script','style','svg','noscript'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out=[]; self.skipped=[]
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP: self.skipped.append(tag)
        if tag in self.SEPARATORS: self.out.append(' ')
    def handle_endtag(self, tag):
        if self.skipped and tag==self.skipped[-1]: self.skipped.pop()
        if tag in self.SEPARATORS: self.out.append(' ')
    def handle_data(self, data):
        if not self.skipped: self.out.append(data)
    def text(self): return ' '.join(' '.join(self.out).split())


def visible_text(html):
    parser=VisibleHTML();parser.feed(html);return parser.text()


def canonical_company_pattern(name):
    pieces=re.findall(r'[A-Z0-9]+',name.upper())
    if not pieces: return re.compile(r'(?!)')
    words=['(?:LTD|LIMITED)' if x in ('LTD','LIMITED') and i==len(pieces)-1 else re.escape(x)
           for i,x in enumerate(pieces)]
    return re.compile(r'(?<![A-Z0-9])'+r'[^A-Z0-9]+'.join(words)+r'(?![A-Z0-9])')


VAT_RE = re.compile(r'(?<![A-Z0-9])GB\s*[.:-]?\s*(?P<digits>(?:\d[.\s-]*){8}\d)(?!\d)',re.I)
LEGAL_RE = re.compile(r'\b(?:LTD|LIMITED|PLC|LLP)\b',re.I)


def find_vats(text, company_name):
    """Full legal name MUST precede GB VAT nearby, without another legal name."""
    normalized=' '.join(text.split()); upper=normalized.upper()
    found={}
    for nm in canonical_company_pattern(company_name).finditer(upper):
        for v in VAT_RE.finditer(upper, nm.end(), min(len(upper), nm.end()+140)):
            between=upper[nm.end():v.start()]
            if LEGAL_RE.search(between): continue
            digits=re.sub(r'\D','',v.group('digits'))
            if len(digits)!=9:continue
            context=normalized[max(nm.start()-15,0):min(len(normalized),v.end()+40)]
            found[digits]=context
    return sorted(found.items())


def read_csv(path):
    with path.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        wr=csv.DictWriter(f,fieldnames=columns);wr.writeheader();wr.writerows(rows)


def postcode_url(postcode):
    code=''.join(str(postcode).upper().split())
    if not re.fullmatch(r'(?:GIR0AA|[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2})', code):
        return None
    return f'https://{DOMAIN}/postalcode/postcode/{quote((code[:-3]+"-"+code[-3:]).lower(),safe="")}'


class PostcodeReader:
    """Single-domain, robots-aware fetcher. Never bypass blocks."""
    def __init__(self):
        self.cache={};self.robots_status='UNSET';self.robots=None;self.last=0
    def read_robots(self):
        if self.robots_status!='UNSET':return
        try:
            req=Request('https://'+DOMAIN+'/robots.txt',headers={'User-Agent':UA})
            with urlopen(req,timeout=TIMEOUT) as resp:
                raw=resp.read(200_000).decode('utf-8','replace')
            robot=RobotFileParser();robot.parse(raw.splitlines());self.robots=robot;self.robots_status='OK'
        except HTTPError as exc:
            if exc.code==404:
                robot=RobotFileParser();robot.parse(['User-agent: *','Allow: /']);self.robots=robot;self.robots_status='OK'
            else:self.robots_status=f'ROBOTS_HTTP_{exc.code}'
        except (URLError,TimeoutError,OSError) as exc:
            self.robots_status=f'ROBOTS_UNAVAILABLE_{type(exc).__name__}'
    def fetch(self,url):
        if url in self.cache:return self.cache[url]
        if urlparse(url).hostname != DOMAIN:return 'INVALID_HOST',''
        self.read_robots()
        if self.robots_status != 'OK':return self.robots_status,''
        if not self.robots.can_fetch(UA,url):return 'ROBOTS_DISALLOWED',''
        pause=1.0-(time.monotonic()-self.last)
        if pause>0:time.sleep(pause)
        self.last=time.monotonic()
        try:
            req=Request(url,headers={'User-Agent':UA,'Accept':'text/html'})
            with urlopen(req,timeout=TIMEOUT) as response:
                if urlparse(response.geturl()).hostname != DOMAIN:return 'REDIRECT_OTHER_HOST',''
                if 'text/html' not in response.headers.get('Content-Type','').lower():return 'NOT_HTML',''
                data=response.read(MAX_BYTES+1)
                if len(data)>MAX_BYTES:return 'PAGE_TOO_LARGE',''
                charset=response.headers.get_content_charset() or 'utf-8'
                answer=('OK',visible_text(data.decode(charset,'replace')))
        except HTTPError as exc: answer=(f'HTTP_{exc.code}','')
        except (URLError,TimeoutError,OSError,UnicodeError,ValueError) as exc: answer=(f'NETWORK_{type(exc).__name__}','')
        self.cache[url]=answer
        return answer


def audit(companies,history,reader,selected=None,out_dir=None):
    hmap={}
    for h in history:
        num=(h.get('company_number') or '').strip()
        post=(h.get('postcode') or '').strip()
        if num and post:hmap.setdefault(num,[]).append(h)
    log=[];candidates=[];summary=[]
    for i,company in enumerate(companies,1):
        sid=str(company.get('sample_id',i)).strip()
        if selected and sid not in selected:continue
        name=company['company_name'].strip(); number=company.get('company_number','').strip()
        known=[('CURRENT_REGISTERED_POSTCODE',company.get('registered_postcode',''))]
        known.extend(('HISTORICAL_POSTCODE_MANUALLY_RESEARCHED',h['postcode']) for h in hmap.get(number,[]))
        urls=[]
        for kind,code in known:
            url=postcode_url(code)
            if url and url not in [x[1] for x in urls]:urls.append((kind,url))
        found={};status_list=[]
        for kind,url in urls:
            status,text=reader.fetch(url)
            status_list.append(status)
            log.append({'sample_id':sid,'company_name':name,'source_type':kind,'url':url,'status':status})
            if status!='OK':continue
            for vat,context in find_vats(text,name):
                found[(vat,url)]=(kind,context)
        vat_values=sorted({v for v,_ in found})
        if len(vat_values)==1:flag='CANDIDATE_UNVERIFIED'
        elif len(vat_values)>1:flag='MULTIPLE_CANDIDATES_REVIEW'
        elif not urls:flag='NO_POSTCODE_AVAILABLE'
        elif 'OK' not in status_list:flag='ACCESS_LIMITED'
        elif any(s!='OK' for s in status_list):flag='NO_CANDIDATE_SOME_PAGES_BLOCKED'
        else:flag='NO_CANDIDATE_ON_SCANNED_PAGES'
        for (vat,url),(kind,context) in sorted(found.items()):
            candidates.append({'sample_id':sid,'company_name':name,'company_number':number,'vat_candidate':vat,
                               'source_url':url,'postcode_source_type':kind,'text_evidence':context,
                               'checked_at_utc':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                               'hmrc_status':'NOT_CHECKED_BY_SCRIPT'})
        summary.append({'sample_id':sid,'company_name':name,'company_number':number,
                        'vat_candidate':vat_values[0] if len(vat_values)==1 else '',
                        'status':flag,'sources_scanned':len(urls),'sources_accessible':status_list.count('OK'),
                        'source_url':next((url for (v,url) in found if v==vat_values[0]),'') if len(vat_values)==1 else '',
                        'hmrc_status':'NOT_CHECKED_BY_SCRIPT',
                        'note':'Historical postcode source was selected manually' if number in hmap else ''})
        print(f'{sid:>2} {name[:34]:34} | {flag:34} | {", ".join(vat_values) or "(none)"}',flush=True)
    if out_dir:
        write_csv(out_dir/'postcode_tabel.csv',summary,['sample_id','company_name','company_number','vat_candidate','status','sources_scanned','sources_accessible','source_url','hmrc_status','note'])
        write_csv(out_dir/'postcode_candidati.csv',candidates,['sample_id','company_name','company_number','vat_candidate','source_url','postcode_source_type','text_evidence','checked_at_utc','hmrc_status'])
        write_csv(out_dir/'postcode_jurnal.csv',log,['sample_id','company_name','source_type','url','status'])
        print(f'\nVAT candidates: {sum(bool(x["vat_candidate"]) for x in summary)}/{len(summary)} firms, NOT verified by the script')
        print(f'Output: {out_dir.resolve()}')
    return summary,candidates,log


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'companies.csv')
    parser.add_argument('--history',type=Path,default=ROOT/'historical_postcodes.csv')
    parser.add_argument('--out',type=Path,default=ROOT/'rezultate_postcode')
    parser.add_argument('--only',nargs='+',help='Optional sample IDs, e.g. --only 8 12 28')
    args=parser.parse_args()
    companies=read_csv(args.input)
    history=read_csv(args.history) if args.history.exists() else []
    audit(companies,history,PostcodeReader(),set(args.only) if args.only else None,args.out)

if __name__=='__main__':main()
