"""Save official landing pages and print bounded download-link discovery."""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin
import requests
from complex_table_run import BUILD, save, sha

URLS = [
 'https://www.aud.gov.hk/chi/pubpr_arpt/rpt_85.htm',
 'https://www.try.gov.hk/internet/eharch_acct.html',
 'https://www.microsoft.com/investor/reports/ar25/index.html',
 'https://huggingface.co/datasets/docling-project/FinTabNet_OTSL/raw/main/README.md',
 'https://api.github.com/repos/wangwen-whu/WTW-Dataset/contents',
]


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        self.links += [v for k,v in attrs if k in ('href','src') and v]


def discover(pair):
    i,url=pair
    record={'url':url}
    try:
        s=requests.Session();s.trust_env=False
        r=s.get(url,timeout=(10,25));r.raise_for_status()
        path=BUILD/'raw'/f'discovery-{i}.txt';path.write_text(r.text,'utf-8')
        parser=Links();parser.feed(r.text)
        record.update(status=r.status_code,path=str(path),sha256=sha(path),links=[urljoin(r.url,u) for u in parser.links if any(x in u.lower() for x in ('.pdf','acct','download','zip','.tar','.gz'))][:70])
        if not parser.links: record['excerpt']=r.text[:5000]
    except Exception as e:record['error']=str(e)
    save(BUILD/'raw'/f'discovery-{i}.json',record)
    return record


if __name__=='__main__':
    with ThreadPoolExecutor(max_workers=3) as p:
        for r in p.map(discover,enumerate(URLS)):print(r,flush=True)
