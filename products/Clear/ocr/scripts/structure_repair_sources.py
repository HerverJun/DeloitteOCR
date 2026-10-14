"""Bounded public-source discovery; acquired inputs are never quality labels."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
import argparse
import time
import requests
from structure_repair_run import AUDIT, BUILD, save, sha

SOURCES = [
    ('hk-treasury-index','https://www.try.gov.hk/internet/eharch_acct.html','native_financial_discovery'),
    ('hk-budget-2024','https://www.budget.gov.hk/2024/chi/pdf/c_budget_speech_2024-25.pdf','native_financial_pdf'),
    ('hk-budget-2023','https://www.budget.gov.hk/2023/chi/pdf/c_budget_speech_2023-24.pdf','native_financial_pdf'),
    ('hk-audit-86-index','https://www.aud.gov.hk/chi/pubpr_arpt/rpt_86.htm','native_full_page_discovery'),
    ('wtw-repository','https://api.github.com/repos/wangwen-whu/WTW-Dataset/git/trees/main?recursive=1','real_photo_discovery'),
    ('wtw-download','https://tianchi.aliyun.com/dataset/108587','real_photo_discovery'),
    ('icdar2019-repository','https://api.github.com/repos/cndplab-founder/ICDAR2019_cTDaR/git/trees/master?recursive=1','real_scan_discovery'),
]

class Links(HTMLParser):
    def __init__(self): super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        self.links.extend(v for k,v in attrs if k in ('href','src') and v)


def fetch(source):
    key,url,category=source
    folder=BUILD/'sources'/key; folder.mkdir(parents=True,exist_ok=False)
    start=time.monotonic()
    record={'id':key,'url':url,'category':category,'started_utc':datetime.now(timezone.utc).isoformat(),
            'split':'development_or_regression_only','labels':'none','attempt':1}
    try:
        session=requests.Session();session.trust_env=False
        with session.get(url,timeout=(10,20),stream=True) as response:
            record.update(http_status=response.status_code,final_url=response.url)
            response.raise_for_status()
            path=folder/('source.pdf' if category.endswith('_pdf') else 'source.txt')
            with path.open('wb') as stream:
                for chunk in response.iter_content(262144):
                    if time.monotonic()-start>75 or stream.tell()>40*1024*1024: raise TimeoutError('source budget')
                    stream.write(chunk)
            if category.endswith('_pdf') and path.read_bytes()[:5]!=b'%PDF-':raise ValueError('not PDF')
            record.update(status='downloaded',path=str(path),sha256=sha(path),bytes=path.stat().st_size)
            if not category.endswith('_pdf'):
                parser=Links();parser.feed(path.read_text('utf-8',errors='replace'))
                record['links']=parser.links[:200]
    except Exception as error:record.update(status='failed',error=type(error).__name__+': '+str(error))
    record['seconds']=time.monotonic()-start
    save(folder/'receipt.json',record)
    print(key,record['status'],flush=True)
    return record


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.parse_args()
    with ThreadPoolExecutor(max_workers=3) as pool: records=list(pool.map(fetch,SOURCES))
    save(AUDIT/'acquisition.json',{'sources':records,'initial_budget_minutes':45,'generative_calls':0})
