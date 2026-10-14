from pathlib import Path
from html.parser import HTMLParser
import re

root = Path(__file__).resolve().parent
class Readable(HTMLParser):
    def __init__(self):
        super().__init__(); self.skip=0; self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','svg'): self.skip+=1
        if not self.skip and tag in ('p','div','li','tr','br','h1','h2','h3','h4','section','article'): self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','svg'): self.skip=max(0,self.skip-1)
        if not self.skip and tag in ('p','div','li','tr','h1','h2','h3','h4'): self.parts.append('\n')
    def handle_data(self,data):
        if not self.skip: self.parts.append(data)
for path in (root/'sources').glob('*.txt'):
    if path.name.endswith('.readable.txt'): continue
    text=path.read_text(encoding='utf-8',errors='replace')
    if '<html' not in text[:4000].lower(): continue
    parser=Readable(); parser.feed(text)
    lines=[re.sub(r'\s+',' ',line).strip() for line in ''.join(parser.parts).splitlines()]
    path.with_suffix('.readable.txt').write_text('\n'.join(line for line in lines if line)+'\n',encoding='utf-8')
