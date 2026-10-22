"""Deterministic, offline report rendering. Model text is always escaped."""
from __future__ import annotations

from html import escape
import json

from .compare import STATUS_LABELS

TEMPLATE_VERSION = 'clear-comparison-html-v1'
STYLE = '''
:root{color-scheme:light;--ink:#252c36;--muted:#606d7e;--navy:#173252;--line:#dce2e9;--paper:#fff}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f1f3f6;color:var(--ink);font:15px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1180px;margin:32px auto;background:var(--paper);padding:48px 52px;border:1px solid var(--line)}
header{border-top:6px solid var(--navy);padding-top:22px;margin-bottom:30px}.eyebrow{color:var(--muted);font-size:12px;letter-spacing:.12em}
h1{font-size:30px;line-height:1.35;color:var(--navy);margin:10px 0 14px}h2{color:var(--navy);font-size:21px;margin:40px 0 16px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:22px 0 8px}p{margin:8px 0 14px}a{color:#174d89;text-underline-offset:3px}nav{border-block:1px solid var(--line);padding:12px 0;display:flex;flex-wrap:wrap;gap:8px 22px}
table{width:100%;border-collapse:collapse;margin:14px 0 22px;font-size:14px;table-layout:fixed}th,td{text-align:left;vertical-align:top;padding:11px 12px;border:1px solid var(--line);overflow-wrap:anywhere;white-space:pre-wrap}
th{background:#eef2f7;color:var(--navy);font-weight:600}thead{display:table-header-group}.meta th{width:24%}.comparison th:nth-child(1){width:19%}.comparison th:nth-child(4){width:11%}.comparison th:last-child{width:27%}
.status{display:inline-block;font-size:12px;font-weight:600;padding:3px 7px;border:1px solid currentColor;border-radius:3px;white-space:nowrap}
.consistent{color:#26613e;background:#f0f8f2}.different{color:#9b332c;background:#fff3f1}.only_a,.only_b{color:#285a8a;background:#f0f6fc}.insufficient{color:#805f19;background:#fff9e9}
.metrics{display:grid;grid-template-columns:repeat(5,1fr);border:1px solid var(--line);margin:16px 0}.metric{padding:16px;border-right:1px solid var(--line)}.metric:last-child{border:0}.metric strong{display:block;font-size:28px;color:var(--navy)}
.muted{color:var(--muted)}.note{padding:12px 16px;background:#f7f9fc;border-left:3px solid var(--navy);margin:14px 0}.warning{border-color:#a96f16;background:#fff9e9}.evidence{margin:12px 0 20px;padding:14px 16px;border:1px solid var(--line);break-inside:avoid}.evidence blockquote{white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0 0;border-left:2px solid var(--line);padding-left:12px}
.evidence small{color:var(--muted)}.refs{font-size:12px}.empty{padding:20px;background:#f7f9fc;color:var(--muted)}footer{border-top:1px solid var(--line);margin-top:36px;padding-top:14px;font-size:12px;color:var(--muted)}
@media(max-width:800px){main{margin:0;padding:24px 16px;border:0}h1{font-size:25px}.metrics{grid-template-columns:repeat(2,1fr)}.table-scroll{overflow:auto}.comparison{min-width:850px}}
@media print{@page{size:A4 landscape;margin:14mm}body{background:white;font-size:10pt}main{margin:0;max-width:none;padding:0;border:0}nav{display:none}h2{break-after:avoid;margin-top:22px}tr,.metric{break-inside:avoid}th,td{padding:7px}a{color:inherit;text-decoration:none}.table-scroll{overflow:visible}.comparison{min-width:0}.evidence{break-inside:auto}.metrics{grid-template-columns:repeat(5,1fr)}footer{font-size:8pt}}
'''


def _e(value):
    if value is None:
        return '—'
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return escape(str(value), quote=True)


def _table(headers, rows, css=''):
    head = ''.join('<th scope="col">' + _e(value) + '</th>' for value in headers)
    body = ''.join('<tr>' + ''.join('<td>' + value + '</td>' for value in row) + '</tr>' for row in rows)
    return '<div class="table-scroll"><table class="' + _e(css) + '"><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>'


def _source_name(document):
    source = document.get('source') or {}
    return document.get('title') or document.get('name') or source.get('original_name') or source.get('filename') or source.get('name') or document.get('document_id') or '未命名材料'


def render_report(result):
    """Render a frozen result; no current timestamps, remote assets or scripts."""
    if hasattr(result, 'model_dump'):
        result = result.model_dump(mode='json')
    title = result.get('title') or '文档主题对比报告'
    profile = result.get('profile') or {}
    documents = result.get('documents') or {'a': result.get('left') or {}, 'b': result.get('right') or {}}
    items = result.get('items') or []
    evidence = {}
    for item in items:
        for entry in item.get('evidence', []):
            evidence.setdefault(entry['id'], entry)
    evidence_order = {identifier: index + 1 for index, identifier in enumerate(evidence)}
    def links(item):
        return ' '.join('<a href="#evidence-' + str(evidence_order[entry['id']]) + '">[' + str(evidence_order[entry['id']]) + ']</a>'
                        for entry in item.get('evidence', []))
    def badge(status):
        status = status if status in STATUS_LABELS else 'insufficient'
        return '<span class="status ' + status + '">' + STATUS_LABELS[status] + '</span>'
    sections = [('materials', '报告信息与双方材料'), ('theme', '比较主题与口径'), ('summary', '结论摘要'),
                ('comparison', '逐项对照'), ('evidence', '差异说明与原文证据'), ('coverage', '未匹配项与处理覆盖'), ('versions', '版本与执行信息')]
    html = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; img-src data:; base-uri &#39;none&#39;; form-action &#39;none&#39;">',
            '<title>', _e(title), '</title><style>', STYLE, '</style></head><body><main><header>',
            '<div class="eyebrow">CLEAR · DOCUMENT COMPARISON</div><h1>', _e(title), '</h1>',
            '<p class="muted">', _e(profile.get('name', '自定义主题')), ' · 固定模板 ', TEMPLATE_VERSION, '</p></header>',
            '<nav aria-label="报告目录">', ''.join('<a href="#' + identifier + '">' + name + '</a>' for identifier, name in sections), '</nav>']
    if result.get('source_updated'):
        html.append('<p class="note warning">来源已更新。本报告保留生成时的解析修订，请重新比较以使用最新内容。</p>')
    html.append('<section id="materials"><h2>01 / 报告信息与双方材料</h2>')
    rows = [[_e('生成时间'), _e(result.get('created_at', '结果快照未附生成时间'))],
            [_e('比较任务'), _e(result.get('id') or result.get('comparison_id') or '离线结果快照')]]
    for side, label in [('a', '材料 A'), ('b', '材料 B')]:
        doc = documents.get(side) or {}
        rows.extend([[_e(label), _e(_source_name(doc))], [_e(label + '解析修订'), _e(doc.get('revision', '—'))]])
    html.extend([_table(['项目', '内容'], rows, 'meta'), '</section><section id="theme"><h2>02 / 比较主题与口径</h2>',
                 '<p>', _e(profile.get('instructions', '')), '</p>'])
    field_rows = [[_e(field.get('label', field.get('key'))), _e({'text': '文字', 'date': '日期', 'number': '数值', 'percentage': '百分比'}.get(field.get('type'), field.get('type'))),
                   _e(field.get('tolerance', 0)), _e('、'.join(field.get('aliases', [])) or '—')] for field in profile.get('fields', [])]
    html.append(_table(['字段', '类型', '容差', '字段别名'], field_rows))
    html.extend(['<p class="note">日期规则：', _e('要求双方截止日期一致' if profile.get('date_policy', 'same_as_of') == 'same_as_of' else '按配置忽略截止日期差异'),
                 '；A：', _e(profile.get('as_of_a') or '以原文为准'), '；B：', _e(profile.get('as_of_b') or '以原文为准'), '。</p></section>',
                 '<section id="summary"><h2>03 / 结论摘要</h2><div class="metrics">'])
    for status, label in STATUS_LABELS.items():
        count = sum(item.get('status') == status for item in items)
        html.extend(['<div class="metric"><strong>', str(count), '</strong>', label, '</div>'])
    html.extend(['</div><p class="muted">共 ', str(len(items)), ' 个对照项。结论限定于选定材料、解析修订和已验证证据。单侧提及表示本次处理范围中的抽取结果。</p></section>',
                 '<section id="comparison"><h2>04 / 逐项对照</h2>'])
    rows = []
    for item in items:
        identity = str(item.get('entity') or '') + (' / ' + str(item['work_item']) if item.get('work_item') else '')
        rows.append([_e(identity) + '<br><span class="muted">' + _e(item.get('field_label') or item.get('field')) + '</span>',
                     _e(item.get('a_value') or '未提取到'), _e(item.get('b_value') or '未提取到'), badge(item.get('status')),
                     _e(item.get('explanation')) + '<br><span class="refs">' + links(item) + '</span>'])
    html.append(_table(['项目 / 字段', '材料 A', '材料 B', '结论', '说明与证据'], rows, 'comparison') if rows else '<p class="empty">未提取到可对齐的主题事实。请检查主题配置和处理覆盖情况。</p>')
    html.append('</section><section id="evidence"><h2>05 / 差异说明与原文证据</h2>')
    for item in items:
        if item.get('status') == 'different':
            html.extend(['<h3>', _e(item.get('entity')), ' · ', _e(item.get('field_label') or item.get('field')), '</h3><p>', _e(item.get('explanation')), ' ', links(item), '</p>'])
    for identifier, entry in evidence.items():
        number = evidence_order[identifier]
        precision = {'model_estimated': '模型估计坐标', 'approximate': '近似坐标', 'page_only': '页级定位'}.get(entry.get('precision'), '原文块定位')
        html.extend(['<article class="evidence" id="evidence-', str(number), '"><strong>[', str(number), '] 材料 ',
            _e(str(entry.get('side', '')).upper()), ' · 第 ', _e(entry.get('page_number')), ' 页</strong><br><small>',
            '修订 ', _e(entry.get('revision')), ' · 块 ', _e(entry.get('block_id')), ' · ', _e(precision),
            '</small><blockquote>', _e(entry.get('quote')), '</blockquote></article>'])
    if not evidence:
        html.append('<p class="empty">暂无通过验证的原文证据。</p>')
    html.append('</section><section id="coverage"><h2>06 / 未匹配项与处理覆盖</h2>')
    rows = []
    for side in ('a', 'b'):
        coverage = (result.get('coverage') or {}).get(side) or {}
        rows.append([side.upper(), _e(coverage.get('total_pages', 0)), _e(coverage.get('parsed_pages', 0)),
                     _e(str(coverage.get('extracted_chunks', 0)) + ' / ' + str(coverage.get('total_chunks', 0))),
                     _e('完整' if coverage.get('complete') else '不完整')])
    html.append(_table(['材料', '范围页数', '解析成功页', '完成抽取分块', '覆盖情况'], rows))
    unresolved = [item for item in items if item.get('status') in {'only_a', 'only_b', 'insufficient'}]
    if unresolved:
        html.append(_table(['项目 / 字段', '状态', '原因'], [[_e(item.get('entity')) + ' / ' + _e(item.get('field_label') or item.get('field')), badge(item.get('status')), _e(item.get('explanation'))] for item in unresolved]))
    if result.get('diagnostics'):
        html.append(_table(['阶段 / 页码', '诊断'], [[_e(str(entry.get('stage') or entry.get('side') or '') + ' / ' + str(entry.get('page_number') or '—')), _e(entry.get('message') or entry.get('code'))] for entry in result['diagnostics']]))
    html.append('</section><section id="versions"><h2>07 / 版本与执行信息</h2>')
    rows = [[_e('结果结构'), _e(result.get('schema_version', '1.0'))], [_e('HTML 模板'), TEMPLATE_VERSION],
            [_e('比较配置'), _e(str(profile.get('id', '')) + ' / v' + str(profile.get('version', 1)))]]
    for side in ('a', 'b'):
        doc = documents.get(side) or {}
        rows.append([_e('材料 ' + side.upper() + ' 解析器'), _e(doc.get('parser') or doc.get('engine') or '参见解析文档的逐页来源')])
    for label, value in (result.get('versions') or {}).items():
        rows.append([_e(label), _e(value)])
    models = sorted({str(record.get('model')) for record in result.get('models', []) if record.get('model')})
    rows.append([_e('比较模型'), _e('、'.join(models) or '参见任务调用记录')])
    usage = [record.get('usage') or {} for record in result.get('models', [])]
    for token_kind, label in [('input_tokens', '已记录输入 token'), ('output_tokens', '已记录输出 token')]:
        values = [record.get(token_kind) for record in usage]
        rows.append([_e(label), _e(sum(values) if values and all(type(value) is int for value in values) else '供应商未提供完整用量')])
    html.extend([_table(['项目', '版本 / 数值'], rows, 'meta'), '</section><footer>Clear 文档主题对比 · 页面样式由本地固定模板生成 · 原文摘录和模型结果均按文本安全呈现</footer></main></body></html>'])
    return ''.join(html)


def document_markdown(document):
    """Readable export preserving page boundaries and source revision."""
    lines = ['# ' + escape(str(_source_name(document)).replace('\n', ' '), quote=False), '',
             '> clear.document · 解析修订 ' + str(document.get('revision', 1)), '']
    for page in document.get('pages', []):
        lines.extend(['## 第 ' + str(page['page_number']) + ' 页', '',
                      '<!-- page:' + str(page['page_number']) + ' -->', ''])
        if page.get('status') not in {None, 'succeeded', 'completed', 'complete'}:
            lines.extend(['> 本页处理未完成：' + str(page.get('error') or page.get('status')), ''])
        for block in page.get('blocks', []):
            text = str(block.get('text') or '')
            table = block.get('table') or {}
            if not text and table.get('cells'):
                rows = {}
                for cell in table['cells']:
                    rows.setdefault(cell.get('row', 0), {})[cell.get('column', 0)] = str(cell.get('text', ''))
                columns = max((max(cells, default=-1) + 1 for cells in rows.values()), default=0)
                for index, row in enumerate(sorted(rows)):
                    lines.append('| ' + ' | '.join(escape(rows[row].get(column, ''), quote=False).replace('|', '\\|').replace('\n', '<br>') for column in range(columns)) + ' |')
                    if index == 0:
                        lines.append('| ' + ' | '.join('---' for _ in range(columns)) + ' |')
                lines.append('')
                continue
            text = text.replace('<', '&lt;').replace('>', '&gt;')
            if block.get('type') in {'title', 'heading'}:
                text = '#' * min(6, max(3, int(block.get('level') or 1) + 2)) + ' ' + text
            if text:
                lines.extend([text, ''])
    return '\n'.join(lines).rstrip() + '\n'


def document_json(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
