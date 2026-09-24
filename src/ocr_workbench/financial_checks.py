"""Read-only decimal financial checks. Ambiguous relationships remain uncertain."""
from collections import Counter
from decimal import Decimal, localcontext
import re

VERSION='financial-consistency-v1'
TOTALS={'合计','合計','总计','總計','小计','小計','total','subtotal'}
IDENTIFIERS=re.compile(r'编号|編號|序号|序號|代码|代碼|账号|帳號|account\s*(?:no|number)|\bid\b',re.I)
UNITS={'元':Decimal(1),'千元':Decimal(1000),'万元':Decimal(10000),'萬元':Decimal(10000),
       '百万元':Decimal(1000000),'百萬元':Decimal(1000000),'亿元':Decimal(100000000),'億元':Decimal(100000000)}


def parse_amount(literal):
    if not isinstance(literal,str):return {'kind':'unknown','literal':literal}
    text=literal.strip().replace('−','-').replace('（','(').replace('）',')')
    base={'literal':literal}
    if not text:return {**base,'kind':'blank'}
    if text in {'—','–','-','--','－'}:return {**base,'kind':'dash'}
    if len(text)>100:return {**base,'kind':'unknown'}
    negative=text.startswith('(') and text.endswith(')')
    if negative:text=text[1:-1].strip()
    currency=None
    match=re.match(r'^(USD|HKD|CNY|RMB|HK\$|US\$|人民币|人民幣|港币|港幣|美元|[$¥￥])\s*',text,re.I)
    if match:
        raw=match[1].upper();currency={'US$':'USD','美元':'USD','HK$':'HKD','港币':'HKD','港幣':'HKD','RMB':'CNY','人民币':'CNY','人民幣':'CNY'}.get(raw,raw)
        text=text[match.end():]
    percent=text.endswith(('%','％'))
    if percent:text=text[:-1].strip()
    if not re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:\.\d+)?',text):
        return {**base,'kind':'unknown'}
    clean=text.replace(',','').replace(' ','').replace('\u00a0','')
    if negative and clean.startswith(('-','+')):return {**base,'kind':'unknown'}
    digits=clean.lstrip('+-')
    if len(digits.replace('.',''))>40:return {**base,'kind':'unknown'}
    if len(digits)>1 and digits.startswith('0') and not digits.startswith('0.'):
        return {**base,'kind':'identifier'}
    with localcontext() as ctx:
        ctx.prec=80
        value=Decimal(clean)*(-1 if negative else 1)
        if percent:value/=100
    return {**base,'kind':'number','value':value,'currency':currency,'percent':percent,
        'places':len(digits.split('.')[1]) if '.' in digits else 0,'parentheses':negative}


def _unit(text):
    hits=re.findall(r'(?:单位|單位)\s*[:：]\s*(?:(?:人民币|人民幣|港币|港幣|美元)\s*)?(百万元|百萬元|千元|万元|萬元|亿元|億元|元)',text)
    hits += re.findall(r'[（(]\s*(百万元|百萬元|千元|万元|萬元|亿元|億元|元)\s*[)）]',text)
    return hits[0] if len(set(hits))==1 else None


def check_tables(tables):
    issues=[];checks=0
    for ti,table in enumerate(tables):
        cells=table['cells'];slots={(c['row'],c['column']):c for c in cells}
        header_cells=[c for c in cells if c.get('is_header') or c['row']==0]
        unit=_unit(table.get('caption','')+' '+ ' '.join(c['text'] for c in header_cells))
        headers={col:' '.join(c['text'] for c in header_cells if c['column']<=col<c['column']+c['column_span']) for col in range(table['columns'])}
        def issue(kind,row,col,message,**extra):
            issues.append({'kind':kind,'target':{'kind':'cell','table':ti,'row':row,'column':col},
                'original':slots.get((row,col),{}).get('text',''),'message':message,'unit':unit,
                'changes_text':False,**extra})
        years={col:re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',text) for col,text in headers.items()}
        for col,values in years.items():
            if len(values)==1 and sum(values==v for v in years.values())>1:
                issue('year_columns',0,col,'相同年度出现在多个列，请核对是否为不同指标',status='uncertain',year=values[0])
        for col in range(1,table['columns']):
            numeric=[]
            for row in range(1,table['rows']):
                c=slots.get((row,col))
                if not c or c.get('is_header'):continue
                parsed=parse_amount(c['text'])
                if IDENTIFIERS.search(headers[col]):
                    if parsed['kind']=='number' and (parsed['places'] or parsed['percent']):
                        issue('identifier_format',row,col,'编号列含小数或百分号，请核对字面值',status='uncertain')
                    continue
                if parsed['kind']=='number':numeric.append((row,parsed))
            places=Counter(p['places'] for _,p in numeric if not p['percent'])
            if len(places)>1:
                common=places.most_common(1)[0][0]
                for row,p in numeric:
                    if not p['percent'] and p['places']!=common:
                        issue('decimal_places',row,col,'同列小数位不同，可能是显示精度差异',status='uncertain',places=p['places'],common_places=common)
        for row in range(1,table['rows']):
            label=slots.get((row,0),{}).get('text','').strip().rstrip('：:').lower()
            if label not in TOTALS:continue
            # Nested subtotals, blank row separators, spans and missing labels need a user-defined scope.
            detail_rows=list(range(1,row))
            scope_ok=bool(detail_rows) and all((r,0) in slots and slots[r,0]['text'].strip() and
                slots[r,0]['text'].strip().lower() not in TOTALS and slots[r,0]['row_span']==1 for r in detail_rows)
            for col in range(1,table['columns']):
                if IDENTIFIERS.search(headers[col]):continue
                total=slots.get((row,col))
                if not total:continue
                expected=parse_amount(total['text'])
                if expected['kind']!='number':continue
                terms=[parse_amount(slots.get((r,col),{}).get('text','')) for r in detail_rows]
                known=scope_ok and all(p['kind']=='number' for p in terms)
                compatible=known and all(p['currency']==expected['currency'] and p['percent']==expected['percent'] for p in terms)
                if not compatible or (unit is None and expected['currency'] is None and not expected['percent']):
                    issue('total_relation',row,col,'合计的范围、空值、币种或单位尚不明确，未判定算术错误',status='uncertain',rows=detail_rows)
                    continue
                if any(slots[r,col]['row_span']!=1 or slots[r,col]['column_span']!=1 for r in detail_rows) or total['column_span']!=1:
                    issue('total_relation',row,col,'合并格影响合计范围，需核对关系',status='uncertain',rows=detail_rows)
                    continue
                with localcontext() as ctx:
                    ctx.prec=80
                    summed=sum((p['value'] for p in terms),Decimal(0))
                    difference=expected['value']-summed
                    tolerance=sum((Decimal(10)**(-p['places'])/2/(100 if p['percent'] else 1) for p in terms+[expected]),Decimal(0))
                    checks+=1
                    if difference:
                        issue('total_difference',row,col,'合计与连续明细之和存在差额；保留原文',
                            status='rounding_possible' if abs(difference)<=tolerance else 'suspect',
                            rows=detail_rows,values=[p['literal'] for p in terms],relationship='sum_of_contiguous_detail_rows',
                            expected_sum=str(summed),difference=str(difference),rounding_tolerance=str(tolerance),currency=expected['currency'],percent=expected['percent'])
    return {'version':VERSION,'issues':issues,'checked_relations':checks,'automatic_correction':False,
        'scope':'explicit total labels, contiguous non-nested detail rows, compatible stated units/currencies; uncertainties shown separately'}
