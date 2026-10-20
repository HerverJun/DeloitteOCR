"""Independent row-level business assertions; never supplied to the model."""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile


NUMBER = re.compile(r'(?<![A-Za-z0-9_.-])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![A-Za-z0-9_.-])')
ROLES = {
    'invoice': r'发票|票据|原票|原值|原始金额|invoice|source.?amount|document.?amount',
    'ledger': r'台账|账面|账载|ledger|book.?amount',
    'delta': r'差额|差异金额|difference|variance|delta|多记|少记',
    'limit': r'上限|合同金额|合同额|合同总价|合同总额|ceiling|contract.?amount|contract.?limit|limit',
    'count': r'次数|笔数|重复数|数量|(?<![A-Za-z])(?:count|frequency|occurrences)(?![A-Za-z])',
}


def numbers(value):
    result = set()
    for match in NUMBER.finditer(str(value)):
        try:
            result.add(Decimal(match.group().replace(',', '')))
        except InvalidOperation:
            pass
    return result


def entry(fields, kind):
    return {'fields': fields, 'text': ' | '.join(str(key) + '=' + str(value) for key, value in fields.items()), 'format': kind}


def _json_entries(value):
    result = []
    def walk(item, inherited=None):
        if isinstance(item, list):
            for child in item:
                walk(child, inherited)
        elif isinstance(item, dict):
            fields = dict(inherited or {})
            def flatten(current, prefix=''):
                for key, child in current.items():
                    name = prefix + str(key)
                    if isinstance(child, dict):
                        flatten(child, name + '.')
                    elif not isinstance(child, list):
                        fields[name] = child
            flatten(item)
            if fields:
                result.append(entry(fields, 'json'))
            direct = {str(key): child for key, child in item.items() if not isinstance(child, (list, dict))}
            for child in item.values():
                if isinstance(child, list):
                    walk(child, direct)
    walk(value)
    return result


class HTMLRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries, self.cells, self.cell = [], None, None
        self.header, self.is_header = [], False
        self.paragraph = None

    def handle_starttag(self, tag, attributes):
        if tag == 'tr':
            self.cells, self.is_header = [], False
        elif tag in {'td', 'th'} and self.cells is not None:
            self.cell = []
            self.is_header |= tag == 'th'
        elif tag in {'p', 'li'} and self.cells is None:
            self.paragraph = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        elif self.paragraph is not None:
            self.paragraph.append(data)

    def handle_endtag(self, tag):
        if tag in {'td', 'th'} and self.cell is not None:
            self.cells.append(' '.join(self.cell).strip())
            self.cell = None
        elif tag == 'tr' and self.cells is not None:
            if self.is_header:
                self.header = self.cells
            else:
                self.entries.append(entry({self.header[index] if index < len(self.header) else 'column'+str(index): value
                                           for index, value in enumerate(self.cells)}, 'html'))
            self.cells = None
        elif tag in {'p', 'li'} and self.paragraph is not None:
            self.entries.append(entry({'text': ' '.join(self.paragraph)}, 'html'))
            self.paragraph = None


def extract_entries(name, payload):
    suffix = Path(name).suffix.lower()
    if suffix == '.csv':
        text = payload.decode('utf-8-sig')
        return [entry(dict(row), 'csv') for row in csv.DictReader(io.StringIO(text))]
    if suffix == '.json':
        return _json_entries(json.loads(payload.decode('utf-8-sig')))
    if suffix == '.docx':
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            document = ET.fromstring(archive.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        def text(node):
            return ''.join(node.itertext()) if node.tag.endswith('}t') else ''.join(node.findall('.//w:t', ns)[index].text or '' for index in range(len(node.findall('.//w:t', ns))))
        result = []
        body = document.find('w:body', ns)
        for child in body if body is not None else []:
            if child.tag.endswith('}tbl'):
                rows = [[text(cell) for cell in row.findall('w:tc', ns)] for row in child.findall('w:tr', ns)]
                if rows:
                    headers = rows[0]
                    for row in rows[1:]:
                        result.append(entry({headers[index] if index < len(headers) else 'column'+str(index): value
                                             for index, value in enumerate(row)}, 'docx'))
            elif child.tag.endswith('}p'):
                result.append(entry({'text': text(child)}, 'docx'))
        return result
    if suffix in {'.html', '.htm'}:
        parser = HTMLRows()
        parser.feed(payload.decode('utf-8-sig'))
        return parser.entries
    if suffix in {'.md', '.txt'}:
        text = payload.decode('utf-8-sig')
        lines, result, index = text.splitlines(), [], 0
        while index < len(lines):
            line = lines[index]
            if ('|' in line and index + 1 < len(lines)
                    and re.fullmatch(r'\s*\|?[\s:|\-]+\|?\s*', lines[index+1])):
                headers = [value.strip() for value in line.strip().strip('|').split('|')]
                index += 2
                while index < len(lines) and '|' in lines[index]:
                    values = [value.strip() for value in lines[index].strip().strip('|').split('|')]
                    result.append(entry(dict(zip(headers, values)), 'markdown'))
                    index += 1
                continue
            # A line or bullet is one assertion. Never combine scattered file text.
            if line.strip():
                result.append(entry({'text': line.strip()}, suffix.lstrip('.')))
            index += 1
        return result
    return []


def role_values(item, role):
    pattern = ROLES[role]
    result = set()
    for key, value in item['fields'].items():
        if role == 'delta' and re.search(r'threshold|阈值|容差', key, re.IGNORECASE):
            continue
        if re.search(pattern, key, re.IGNORECASE):
            result.update(numbers(value))
    # Natural prose is allowed when amounts have explicit adjacent role labels.
    nearby = re.compile('(?:' + pattern + r')[^\d\n]{0,16}(' + NUMBER.pattern + ')', re.IGNORECASE)
    prose = '\n'.join(str(value) for key,value in item['fields'].items()
                      if key.casefold() in {'text','description','details','analysis','说明','内容','事实','分析','结论'})
    for match in nearby.finditer(prose):
        if role == 'delta' and re.search(r'threshold|阈值|容差', match.group(0), re.IGNORECASE):
            continue
        result.update(numbers(match.group(1)))
    return {abs(value) for value in result} if role == 'delta' else result


def audit_relationships(scenario, files):
    if scenario not in {'audit-01', 'audit-02', 'audit-03'}:
        return []
    index = int(scenario[-1])
    invoice, contract = f'INV-{index}002', f'CT-{index}024'
    ledger_total = {1: Decimal('2050'), 2: Decimal('2250'), 3: Decimal('3250')}[index]
    entries = [item for file in files for item in file.get('entries', [])]
    checks = []
    def check(name, predicate, expected):
        matched = [number for number, item in enumerate(entries) if predicate(item)]
        checks.append({'name': name, 'passed': bool(matched), 'detail': {'expected': expected, 'matching_entries': matched}})
    check('invoice_amount_relationship', lambda item: invoice in item['text']
          and Decimal('800') in role_values(item, 'invoice')
          and Decimal('850') in role_values(item, 'ledger')
          and Decimal('50') in role_values(item, 'delta'),
          {'invoice': invoice, 'invoice_amount': '800.00', 'ledger_amount': '850.00', 'difference': '50.00'})
    check('contract_ceiling_relationship', lambda item: contract in item['text'] and Decimal('2000') in role_values(item, 'limit'),
          {'contract': contract, 'ceiling': '2000.00'})
    check('aggregate_amount_relationship', lambda item: bool(re.search(r'合计|总计|汇总|total|sum', item['text'], re.IGNORECASE))
          and Decimal('2000') in role_values(item, 'invoice') and ledger_total in role_values(item, 'ledger')
          and ledger_total - Decimal('2000') in role_values(item, 'delta'),
          {'valid_invoice_total': '2000.00', 'ledger_total': str(ledger_total), 'difference': str(ledger_total-Decimal('2000'))})
    if index == 2:
        check('missing_invoice_relationship', lambda item: 'INV-2999' in item['text']
              and bool(re.search(r'缺|未提供|未找到|无对应|missing|not.?provided|not.?found', item['text'], re.IGNORECASE))
              and Decimal('200') in role_values(item, 'ledger'),
              {'invoice': 'INV-2999', 'state': 'missing', 'ledger_amount': '200.00'})
    if index == 3:
        check('duplicate_invoice_relationship', lambda item: 'INV-3001' in item['text']
              and bool(re.search(r'重复|duplicate', item['text'], re.IGNORECASE))
              and (Decimal('2') in role_values(item, 'count') or bool(re.search(r'两[次笔条]|2\s*[次笔条]', item['text']))),
              {'invoice': 'INV-3001', 'count': 2, 'state': 'duplicate'})
    return checks
