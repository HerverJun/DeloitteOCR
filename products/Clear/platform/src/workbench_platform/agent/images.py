"""Verified host image projections for the two supported native protocols."""
import base64
from copy import deepcopy
from .providers import ProviderFault


def blocks(text, images, protocol):
    result = [{'type': 'text', 'text': text or 'Image from the selected task file.'}]
    if len(images) > 8:
        raise ProviderFault('image_budget', '一次最多发送8张图片')
    total = 0
    for image in images:
        media_type = image['media_type']
        data = image['data']
        if media_type not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}:
            raise ProviderFault('image_format', '图片格式不受支持')
        total += len(data)
        if total > 12 * 1024 * 1024:
            raise ProviderFault('image_budget', '图片超过当前单次请求大小限制')
        try:
            base64.b64decode(data, validate=True)
        except ValueError:
            raise ProviderFault('image_format', '图片编码无效') from None
        if protocol == 'openai_chat_completions':
            result.append({'type': 'image_url', 'image_url': {'url': f'data:{media_type};base64,{data}', 'detail': 'auto'}})
        else:
            result.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': data}})
    return result


def budget_payload(payload):
    projected = deepcopy(payload)
    count = 0
    def walk(item):
        nonlocal count
        if isinstance(item, dict):
            if item.get('type') == 'image_url':
                count += 1
                return {'type': 'image_url', 'image_url': {'url': '[verified image bytes]'}}
            if item.get('type') == 'image' and isinstance(item.get('source'), dict):
                count += 1
                return {'type': 'image', 'source': {'type': 'base64', 'data': '[verified image bytes]'}}
            return {key: walk(value) for key, value in item.items()}
        if isinstance(item, list):
            return [walk(value) for value in item]
        return item
    return walk(projected), count
