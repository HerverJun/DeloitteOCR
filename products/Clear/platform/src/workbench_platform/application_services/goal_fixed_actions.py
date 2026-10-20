"""Complete local goals over real column/model catalogs; no controller loop."""
import re
import unicodedata

from .errors import DirectActionFault


def normalized(text):
    return unicodedata.normalize('NFKC', text).strip()


def polite(text):
    return re.sub(r'谢谢(?:你|您)?|麻烦(?:你|您)?|劳驾|请(?:帮我|为我)?|帮我|为我|一下', '', normalized(text))


def cleaning_steps(text, columns):
    """Consume the complete goal after matching names to the actual headers."""
    text = polite(text)
    headers = {}
    for column in columns:
        headers.setdefault(normalized(str(column['header'])), []).append(column['column_id'])
    for header in sorted(headers, key=len, reverse=True):
        if not header or header not in text:
            continue
        if len(headers[header]) != 1:
            raise DirectActionFault('列名“' + header + '”在表内重复，请明确使用第几列')
        text = text.replace(header, '@' + headers[header][0] + '@')
    text = re.sub(r'[\s“”"\'「」。.!！?？]', '', text)
    text = re.sub(r'(?:并|然后|最后)?(?:提供|生成|导出|下载|保存)(?:为|成)?(?:csv|CSV|表格)', '', text)
    text = re.sub(r'所选|选中的|选中|这份|这个|表格|数据|文件|中的|里面|中|里面的', '', text)
    clauses = [item for item in re.split(r'并且|然后|接着|再|并|[，,;；]', text) if item]
    if not clauses or len(clauses) > 16:
        return None
    steps = []
    for clause in clauses:
        tokens = re.findall(r'@c[1-9][0-9]{0,2}@', clause)
        if not tokens:
            return None
        rest = re.sub(r'@c[1-9][0-9]{0,2}@', '', clause)
        rest = re.sub(r'将|把|对|按|根据|列|的|、|和|及|与', '', rest)
        trim = re.fullmatch(r'(?:去除|清除|删除|去掉|清理)?(?:首尾|前后|两端)空(?:格|白)(?:去掉|去除|清除|删除)?', rest)
        if trim:
            steps.extend({'step': {'operator': 'trim', 'column': token[1:-1]}} for token in dict.fromkeys(tokens))
        else:
            return None
    return steps if len(steps) <= 32 else None


def prediction_goal(content, media):
    if not media or not all(item.startswith('image/') for item in media):
        return None
    text = polite(content)
    if re.search(r'训练|微调|更新|标注|接受|比较|分析|解释|筛选|ocr|提取|不要|不使用', text, re.I):
        return None
    name = None
    named = re.search(r'模型[“"「]([^”"」]{1,100})[”"」]', text)
    if named:
        name = named[1]
        text = text[:named.start()] + '已有模型' + text[named.end():]
    if not re.search(r'分类|预测', text) or not re.search(r'已有|现有|当前|活动', text):
        return None
    text = re.sub(r'[\s,，。.!！?？]', '', text)
    text = re.sub(r'分类预测|进行分类|进行预测|分类|预测|使用|采用|通过|已有|现有|当前|活动|模型|所选|选中|这些|这批|图片|照片|图像|对|给|用|的|做', '', text)
    if text:
        return None
    return {'use_existing_model': True, 'model_name': name}


def choose_sheet(content, sheets):
    """Only an explicit worksheet qualifier can resolve multiple sheets."""
    matches = []
    for sheet in sheets:
        name = sheet.get('name')
        if not name:
            continue
        quoted = r'[“"「\']' + re.escape(name) + r'[”"」\']'
        pattern = r'(?:工作表(?:名为|为)?\s*' + quoted + r'|(?:名为)?' + quoted + r'(?:的)?工作表)'
        matches.extend((match, sheet) for match in re.finditer(pattern, content))
    if len(matches) > 1:
        raise DirectActionFault('请明确本次只处理哪一个工作表，尚未提交数据操作')
    if matches:
        match, sheet = matches[0]
        return sheet, content[:match.start()] + content[match.end():]
    if len(sheets) != 1:
        raise DirectActionFault('资料包含多个工作表，请明确需要处理哪一个：' + '、'.join(sheet['name'] for sheet in sheets))
    # A single sheet needs no qualifier. Its name may also be a real column
    # name; do not erase that column reference from the user's request.
    return sheets[0], content


async def submit_fixed_goal(runtime, session_id, content, request_id, *, session, identifiers, media):
    prediction = prediction_goal(content, media)
    if prediction is not None:
        from .classification_action import submit
        from packages.contracts.direct_classification import PredictRequest
        return await submit(runtime, session_id, PredictRequest(request_id=request_id, **prediction), source_text=content)
    if len(identifiers) != 1 or not re.search(r'(?:首尾|前后|两端)空(?:格|白)', content):
        return None
    registry = runtime.services.asset_library[0]
    asset = registry.get(session['workspace_id'], identifiers[0])
    suffix = asset.original_name.rsplit('.', 1)[-1].lower()
    if suffix not in {'csv', 'xlsx', 'parquet'}:
        return None
    from .execution import invoke
    service = runtime.services.data_workbench
    if service is None or not service.ready():
        raise DirectActionFault('数据处理运行时尚未配置；该任务需要已有本地数据运行时')
    def discover():
        with registry.stage_verified(session['workspace_id'], asset.asset_id,
                revision=asset.revision, blob_hash=asset.blob_hash) as path:
            return service.inspect_input(path, suffix)
    discovery = await invoke(discover)
    sheet, goal = choose_sheet(content, discovery['sheets'])
    steps = cleaning_steps(goal, sheet['columns'])
    if steps is None:
        return None
    from .data_action import submit
    from packages.contracts.direct_data import DataActionRequest
    return await submit(runtime, session_id, DataActionRequest(request_id=request_id, action='clean',
        left_asset_id=asset.asset_id, left_options={'sheet_name': sheet.get('name')}, steps=steps), source_text=content)
