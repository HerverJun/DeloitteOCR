"""Two fixed, readable representations of the same audit snapshot."""
import json


def _actor(record):
    return record['actor']['display_name'] + '（' + record['actor']['id'] + '）'


def _records(entity):
    result = []
    for record in entity['records']:
        result.append('- ' + ('编制' if record['role'] == 'prepared' else '复核') + '：' + _actor(record)
                      + '；时间 ' + record['created_at'] + '；内容版本 ' + str(record['content_revision'])
                      + '；项目上下文版本 ' + str(record['project_context_revision']) + '；说明：' + record['note'])
    result.append('- 当前版本记录：' + ('已完成' if entity['records_current'] else '未完成'))
    return result


def render_workpaper(operation):
    snapshot, project = operation['snapshot'], operation['snapshot']['project']
    fields = project['project']
    lines = ['# ' + fields['title'] + ' · 审计工作底稿', '',
             '材料性质：' + ('正式底稿' if snapshot['request']['kind'] == 'final' else '中间底稿'),
             '底稿编号：' + operation['workpaper_id'], '项目编号：' + project['project_id'],
             '项目版本：' + str(project['revision']), '形成时间：' + operation['created_at'],
             '发布操作人：' + snapshot['request']['actor']['display_name'],
             '快照 SHA256：' + operation['snapshot_hash'], '',
             '## 审计对象与目标', '', '审计对象：' + fields['audit_object'],
             '期间：' + fields['period_start'] + ' 至 ' + fields['period_end'], '', fields['objective'], '',
             *_records(project), '', '## 工作程序', '']
    for procedure in snapshot['procedures']:
        value = procedure['procedure']
        lines.extend(['### ' + value['title'], '', '程序编号：' + procedure['procedure_id'],
                      '内容版本：' + str(procedure['content_revision']), '',
                      '依据：' + value['basis'], '', '范围：' + value['scope'], '',
                      '程序说明：' + value['note'], '', '结论：' + value['conclusion'], '',
                      *_records(procedure), '', '关联处理批次：'])
        lines.extend('- ' + batch['batch_id'] + '；版本 ' + str(batch['batch_revision'])
                     + '；模板 ' + batch['template_id'] + ' / ' + str(batch['template_version'])
                     + '；配置 SHA256 ' + batch['config_hash'] for batch in procedure['batches'])
        lines.append('')
    lines.extend(['## 规则与覆盖', ''])
    for batch in snapshot['batches']:
        lines.extend(['### ' + batch['template_name'], '',
                      '批次：' + batch['batch_id'] + ' / ' + str(batch['batch_revision']),
                      '执行状态：' + batch['execution_status'] + '；业务复核完成：' + str(batch['review_complete']),
                      '模板：' + batch['template_id'] + ' / ' + str(batch['template_version']),
                      '配置 SHA256：' + batch['config_hash'], '', '固定规则：', '',
                      '```json', json.dumps(batch['rules'], ensure_ascii=False, sort_keys=True, indent=2), '```', '',
                      '覆盖：' + '；'.join(key + '=' + str(value) for key, value in batch['coverage'].items()), ''])
    lines.extend(['## 调查事项、解释与结论', ''])
    evidence = {item['evidence_id']: item['snapshot'] for item in snapshot['evidence']}
    for finding in snapshot['findings']:
        value = finding['finding']
        lines.extend(['### ' + value['title'], '', '事项编号：' + finding['finding_id'],
                      '所属程序：' + finding['procedure_id'], '状态：' + ('已解决' if value['status'] == 'resolved' else '草稿'),
                      '优先级：' + value['priority'], '', '解释：' + value['explanation'], '',
                      '结论：' + value['conclusion'], '', *_records(finding), ''])
        for ref in finding['evidence_refs']:
            item = evidence[ref['evidence_id']]
            if ref.get('scenario') == 'image_forensics':
                inspection = item['inspection']
                review = inspection['review']
                lines.extend(['#### 图像取证证据 ' + ref['evidence_id'], '',
                    '原件：' + inspection['original_name'] + '；SHA256：' + inspection['blob_hash'],
                    '取证任务：' + inspection['inspection_id'] + '；固定审阅版本：' + str(inspection['revision']),
                    '报告 SHA256：' + inspection['report_hash'], '证据 SHA256：' + ref['snapshot_hash'],
                    '人工判断：' + review['assessment'] + '；审阅人：' + review['actor']['display_name'],
                    '人工说明：' + review['note'], '', '工具线索仅供人工调查，热图不是篡改概率，不合并工具分数。'])
                for tool in inspection['report']['evidence']:
                    lines.append('- ' + tool['tool'] + '；状态 ' + tool['status'] + '；版本 ' + tool['version']
                        + '；耗时 ' + str(tool['elapsed_ms']) + ' ms；原因 ' + str(tool['reason']))
                    lines.extend('  - 限制：' + text for text in tool['limitations'])
                lines.extend('- 固定文件：' + file['name'] + '；SHA256 ' + file['sha256'] for file in inspection['files'])
                lines.append('')
                continue
            lines.extend(['#### 证据 ' + ref['evidence_id'], '',
                          '批次：' + ref['batch_id'] + '；事项：' + ref['item_id']
                          + '；批次版本：' + str(ref['batch_revision']),
                          '证据 SHA256：' + ref['snapshot_hash'], '采集时间：' + ref['captured_at'],
                          '规则：' + item['rule_summary'], ''])
            for field in item['fields']:
                lines.append('- ' + field['name'] + '；规则 ' + field['rule_kind'] + '；结果 ' + field['outcome'])
                for entry in field['values']:
                    lines.append('  - ' + entry['role'] + '：类型 ' + entry['kind'] + '；原值 '
                                 + json.dumps(entry['raw'], ensure_ascii=False) + '；处理值 '
                                 + json.dumps(entry['value'], ensure_ascii=False))
                if field['delta'] is not None:
                    lines.append('  - 差额 ' + field['delta'] + '；容差 ' + str(field['tolerance'])
                                 + '；容差内 ' + str(field['within_tolerance']))
            for source in item['sources']:
                location = '资料 ' + source['asset_id'] + ' / ' + str(source['asset_revision'])
                if source.get('sheet_name'):
                    location += '；工作表 ' + source['sheet_name']
                if source.get('row_number'):
                    location += '；行 ' + str(source['row_number'])
                if source.get('column_number'):
                    location += '；列 ' + str(source['column_number'])
                lines.append('- 来源：' + location + '；SHA256 ' + source['blob_hash'])
            lines.append('')
    lines.extend(['## 明确采用的复核成果', ''])
    for ref in snapshot['review_refs']:
        file = ref['results_xlsx']
        lines.append('- ' + ref['bundle_id'] + '；批次 ' + ref['batch_id'] + ' / ' + str(ref['batch_revision'])
                     + '；results.xlsx 成果 ' + file['artifact_id'] + ' / ' + str(file['revision'])
                     + '；文件 SHA256 ' + file['blob_hash'])
    lines.extend(['', '## 未完成事项', '', '未完成数：' + str(snapshot['unresolved_count'])])
    lines.extend('- ' + reason for reason in snapshot['unresolved_reasons'])
    lines.append('')
    document = {'schema_version': '1.0', 'audit_version': '1.0', 'workpaper_id': operation['workpaper_id'],
                'workspace_id': operation['workspace_id'], 'project_id': operation['project_id'],
                'request_id': operation['request_id'], 'payload_hash': operation['payload_hash'],
                'snapshot_hash': operation['snapshot_hash'], 'created_at': operation['created_at'], **snapshot}
    return {'workpaper.md': '\n'.join(lines).encode('utf-8'),
            'workpaper.json': (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2,
                                          allow_nan=False) + '\n').encode('utf-8')}
