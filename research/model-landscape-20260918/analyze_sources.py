from html.parser import HTMLParser
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
class Tables(HTMLParser):
    def __init__(self):
        super().__init__(); self.tables=[]; self.table=None; self.row=None; self.cell=None
    def handle_starttag(self, tag, attrs):
        if tag == 'table': self.table=[]
        elif tag == 'tr' and self.table is not None: self.row=[]
        elif tag in ('td','th') and self.row is not None: self.cell=[]
    def handle_data(self,data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split())); self.cell=None
        elif tag == 'tr' and self.row is not None:
            self.table.append(self.row); self.row=None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table); self.table=None

parser=Tables(); parser.feed((ROOT/'sources/omnidocbench-readme.md').read_text('utf-8'))
(ROOT/'benchmark-tables.json').write_text(json.dumps(parser.tables,ensure_ascii=False,indent=2),'utf-8')
for index,table in enumerate(parser.tables):
    if any(any('TeleOCR' in cell or 'PaddleOCR-VL-1.6' in cell for cell in row) for row in table):
        print(json.dumps({'table_index':index,'rows':table[:32]},ensure_ascii=False))
for name in ('paddle','qwen36','granite','olmocr','chandra'):
    values=json.loads((ROOT/'sources'/f'{name}-models.json').read_text('utf-8'))
    print(json.dumps({'source':name,'models':[{k:item.get(k) for k in ('id','sha','lastModified')} for item in values
        if name!='paddle' or item['id'].endswith(('PaddleOCR-VL-1.6','PP-OCRv6_medium_det','PP-OCRv6_medium_rec'))]},ensure_ascii=False))
