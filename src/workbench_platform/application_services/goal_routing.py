"""Conservative complete-goal parsing for the fixed business services.

The grammar consumes every token: an unrecognized qualifier stays with the
reasoning path. Matching a verb alone can never discard a second requirement.
"""
import re
import unicodedata


def extraction_intent(content, media_types):
    if not media_types:
        return None
    text = unicodedata.normalize('NFKC', content).lower()
    # Remove only semantically neutral courtesies, source nouns and delivery
    # forms already guaranteed by the text service. All other words survive.
    text = re.sub(r'谢谢(?:你|您)?|麻烦(?:你|您)?|劳驾|请(?:帮我|为我)?|帮我|为我|一下|可以|能否', '', text)
    text = re.sub(r'[\s“”"\'（）()]', '', text)
    pages = []
    if all(value == 'application/pdf' for value in media_types) and len(media_types) == 1:
        match = re.search(r'第?([0-9]+(?:(?:、|,|，|和|及|与|至|到|-)[0-9]+)*)页', text)
        if not match:
            return None
        page_text = match.group(1)
        if re.fullmatch(r'\d+(?:至|到|-)\d+', page_text):
            start, end = map(int, re.split(r'至|到|-', page_text))
            if not 1 <= start <= end <= 1000 or end - start >= 100:
                return None
            pages = list(range(start, end + 1))
        else:
            pages = [int(value) for value in re.split(r'、|,|，|和|及|与', page_text)]
            if len(pages) > 100 or len(pages) != len(set(pages)) or any(not 1 <= p <= 1000 for p in pages):
                return None
        text = text[:match.start()] + text[match.end():]
        source = r'pdf|文档|文件'
        kind, params = 'pdf', {'page_numbers': sorted(pages)}
    elif all(value.startswith('image/') for value in media_types):
        source = r'图片|照片|图像|图'
        kind, params = 'image', {}
    else:
        return None
    text = re.sub(r'[,，。.!！?？:：;；]', '', text)
    # Full text/TXT is the service contract, so no additional model step is
    # required to copy, preserve or offer a download of those exact contents.
    has_action = bool(re.search(r'提取|识别|抄出|抄写|读出|读取|转录|(?:转成|转换成|转为)(?:txt|文本)', text))
    text = re.sub(r'(?:并|再|然后|并且)?(?:提供|生成|导出|下载|保存)(?:为|成)?(?:txt|文本|全文)', '', text)
    text = re.sub(r'(?:转成|转换成|转为)(?:txt|文本)', '', text)
    if not has_action:
        return None
    text = re.sub(r'提取出|提取|识别|抄出来|抄出|抄写|读出|读取|转录', '', text)
    text = re.sub(source, '', text)
    text = re.sub(r'所选|选中|这张|这些|这份|这个|所有|全部|完整|文字|文本|原文|中的|里的|上的|里面|上面|内容|把|将|的|中|里|上|字', '', text)
    return (kind, params) if not text else None


async def submit_local_goal(runtime, session_id, content, request_id):
    session = runtime.store.session(session_id)
    task = runtime.tasks.get(session_id)
    if session.get('initial_refs') or task.get('delivery_requirements') or task.get('manual'):
        return None
    # Pending scope is applied by the business submitter, never guessed here.
    identifiers = (task.get('pending_scope') or {}).get('selected_assets', session['selected_assets'])
    if not identifiers:
        return None
    registry = runtime.services.asset_library[0]
    media = [registry.get(session['workspace_id'], item).media_type for item in identifiers]
    intent = extraction_intent(content, media)
    analysis = False
    if intent is None:
        intent = extraction_with_analysis(content, media)
        analysis = intent is not None
    if intent is None:
        from .goal_fixed_actions import submit_fixed_goal
        return await submit_fixed_goal(runtime, session_id, content, request_id,
            session=session, identifiers=identifiers, media=media)
    kind, params = intent
    if analysis:
        from workbench_platform.resources.reader_features import require_features
        require_features(runtime.store.root.parent, 'same-goal-continuation-v1')
    if kind == 'image':
        from .image_text import ImageTextRequest, submit
        run = await submit(runtime, session_id, ImageTextRequest(request_id=request_id), source_text=content,
                           analysis_goal=content if analysis else None)
    else:
        from .pdf_text import PdfTextRequest, submit
        run = await submit(runtime, session_id, PdfTextRequest(request_id=request_id, **params), source_text=content,
                           analysis_goal=content if analysis else None)
    return run


def extraction_with_analysis(content, media_types):
    """Prepare explicit extraction once, retaining the *entire* analysis goal.

    Unsupported engine/region/version qualifications stay in the open path.
    Only an explicit extraction prefix and explicit page scope are admitted.
    """
    if re.search(r'引擎|模型|glm|ppocr|paddle|hunyuan|区域|右侧|左侧|版式|手写|不要(?:提取|识别|读取)|不需要(?:提取|识别)|不提取', content, re.I):
        return None
    analysis = re.search(r'比较|对比|分析|解释|翻译|制表|汇总|核对', content)
    if not analysis:
        return None
    before = re.sub(r'\s+', '', unicodedata.normalize('NFKC', content[:analysis.start()]))
    before = re.sub(r'第?(\d+)页(?:至|到|-)第?(\d+)页', r'第\1至\2页', before)
    if not re.search(r'提取|识别|读取|读出|抄出', before):
        return None
    if media_types and all(value == 'application/pdf' for value in media_types) and len(media_types) == 1:
        groups = re.findall(r'第?([0-9]+(?:(?:、|,|，|和|及|与|至|到|-)[0-9]+)*)页', before)
        if not groups:
            return None
        pages = set()
        for group in groups:
            intent = extraction_intent('读取PDF第' + group + '页文字', media_types)
            if intent is None:
                return None
            pages.update(intent[1]['page_numbers'])
        return ('pdf', {'page_numbers': sorted(pages)}) if len(pages) <= 100 else None
    # Keep the prefix complete, stripping only the join with the next action.
    before = re.sub(r'(?:并且|然后|之后|接着|再|并|后|，|,|、|和|与)+$', '', before)
    return extraction_intent(before, media_types)
