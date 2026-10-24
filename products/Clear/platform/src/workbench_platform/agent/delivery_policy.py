"""User-authoritative delivery requirements, independent of model summaries."""
import re

from .store import digest

POLICIES = {'auto', 'draft', 'reviewed'}
_FORMAL = re.compile(r'正式[^，。；！？\n]{0,8}(?:报告|成果|结果|底稿|交付|字段|版本|材料|文档|结论|版)|(?:核准|人工确认|人工复核|用户确认|我确认)(?:通过)?(?:后|之后|以后)[^，。；！？\n]{0,20}(?:交付|生成|出具|导出|形成)|(?:交付|生成|出具|导出)[^，。；！？\n]{0,12}(?:已核准|已复核|人工核准|用户核准|人工确认)|\b(?:formal\s+(?:report|result|deliverable)|human[- ](?:reviewed|approved)\s+(?:report|result|output)|after\s+(?:human|user)\s+(?:approval|review))\b', re.I)
_NEGATED = re.compile(r'(?:(?:不要|不需要|无需|不用|不生成|不做|暂不)[^，。；！？\n]{0,4}|非|(?:no|not|without)\s+)$', re.I)
_REQUEST = re.compile(r'(?:生成|交付|出具|导出|形成|输出|提交|要求|需要|希望|想要|最终要|做|作为|须|要)[^，。；！？\n]{0,12}$|\b(?:produce|deliver|create|need|want)\s+(?:a\s+)?$', re.I)
_REVIEW_CONDITION = re.compile(r'(?:必须|需要|需|须|要求|等待|另待|待)[^，。；！？\n]{0,28}(?:核准|人工确认|人工复核|用户确认|复核通过)')
_DRAFT = re.compile(r'(?:仅|只)(?:需|要|提供|生成|交付|输出|做|完成|用于|供)?[^，。；！？\n]{0,8}草稿|草稿(?:即可|就好|够了)|\b(?:draft\s+only|only\s+(?:a\s+)?draft)\b', re.I)
_REVIEW_GATE = re.compile(r'(?:(?:等待|等|另待|待)[^，。；！？\n]{0,20})?(?:核准|人工确认|人工复核|用户确认|我确认|本人确认)(?:通过)?(?:之后|以后|后)[^，。；！？\n]{0,12}(?:定稿|交付|生成|出具|导出|形成|发布)')
_QUESTION = re.compile(r'是否|能否|要不要|需不需要|是不是|为什么|如何|什么意思|[吗么？?]')
_NEGATED_REVIEW = re.compile(r'(?:不要|不需要|无需|不用|不必|无须|不须|不应|不能|不再|不想)[^，。；！？\n]{0,8}$')
_NEGATED_ACTION = re.compile(r'(?:不|无|未)[^，。；！？\n]{0,3}(?:定稿|交付|生成|出具|导出|形成|发布)')


def _review_request(text, start, end):
    """A condition in a question, explanation or negated clause is not a gate."""
    separators = '，。；！？\n'
    left = max(text.rfind(mark, 0, start) for mark in separators) + 1
    right = min((position for mark in separators if (position := text.find(mark, end)) >= 0), default=len(text))
    prefix, clause = text[left:start], text[left:right + 1]
    return not (_QUESTION.search(clause) or _NEGATED_REVIEW.search(prefix)
                or _NEGATED_ACTION.search(text[start:end])
                or re.match(r'\s*(?:请|帮我)?(?:解释|说明|介绍)', prefix))


def infer_user_delivery_policy(content, *, source='user_goal', request_id=None, legacy=False):
    """Recognize explicit user requirements; uncertain text leaves draft allowed.

    A two-stage request (draft first, formal after approval) retains reviewed as
    its final requirement. A plain continuation cannot weaken a prior review
    requirement. This function accepts only server-saved user text at callers.
    """
    text = content if isinstance(content, str) else ''
    if not legacy:
        for match in _REVIEW_GATE.finditer(text):
            if _review_request(text, match.start(), match.end()):
                return {'policy': 'reviewed', 'source': source, 'content_sha256': digest(text),
                        'evidence': match.group(0)[:120], **({'request_id': request_id} if request_id else {})}
    for match in _FORMAL.finditer(text):
        prefix = text[max(0, match.start() - 24):match.start()]
        if _NEGATED.search(prefix) or not legacy and not _review_request(text, match.start(), match.end()):
            continue
        suffix = text[match.end():match.end() + 64]
        condition = _REVIEW_CONDITION.search(suffix)
        if condition and (re.search(r'是否|能否|为什么|如何', suffix[:condition.start()]) or
                          re.search(r'不|无|不用|无需|不需要', suffix[max(0, condition.start() - 3):condition.start()])):
            condition = None
        if condition and not legacy and not _review_request(text, match.end() + condition.start(), match.end() + condition.end()):
            condition = None
        if (match.group(0).startswith('正式') or match.group(0).lower().startswith('formal')) and not _REQUEST.search(prefix) and not condition and text.strip() != match.group(0):
            continue
        explicit = re.search(r'核准|人工确认|人工复核|用户确认|我确认|human[- ]|after\s+(?:human|user)', match.group(0), re.I) or condition
        if not legacy and not explicit:
            continue
        return {'policy': 'reviewed', 'source': source, 'content_sha256': digest(text),
                'evidence': match.group(0)[:120], **({'request_id': request_id} if request_id else {})}
    match = _DRAFT.search(text)
    if match:
        return {'policy': 'draft', 'source': source, 'content_sha256': digest(text),
                'evidence': match.group(0)[:120], **({'request_id': request_id} if request_id else {})}
    return None


def effective_policy(task):
    """Return require_review/reviewed, selected policy, resolved policy and source."""
    selected = task.get('delivery_policy', 'auto')
    if selected not in POLICIES:
        raise ValueError('交付策略无效')
    if selected != 'auto':
        return {'policy': selected, 'resolved_policy': selected, 'require_review': selected == 'reviewed',
                'reviewed': selected == 'reviewed', 'source': 'user_setting'}
    inference = task.get('delivery_policy_inference') or infer_user_delivery_policy(task.get('goal', ''), source='saved_user_goal', legacy=task.get('delivery_policy_version', 1) < 2)
    resolved = inference['policy'] if inference and inference.get('policy') in {'draft', 'reviewed'} else 'draft'
    return {'policy': 'auto', 'resolved_policy': resolved, 'require_review': resolved == 'reviewed',
            'reviewed': resolved == 'reviewed', 'source': inference['source'] if inference else 'unspecified',
            **({'evidence': inference.get('evidence'), 'content_sha256': inference.get('content_sha256')} if inference else {})}


def adopt_explicit_policy(task):
    """Freeze the prior interpretation before accepting newly worded goals."""
    if task.get('delivery_policy_version', 1) >= 2:
        return False
    if effective_policy(task)['require_review'] and not task.get('delivery_policy_inference'):
        task['delivery_policy_inference'] = infer_user_delivery_policy(task.get('goal', ''), source='saved_user_goal', legacy=True)
    task['delivery_policy_version'] = 2
    return True
