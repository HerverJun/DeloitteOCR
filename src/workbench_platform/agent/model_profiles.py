"""Declared capabilities, endpoint metadata, and immutable execution profiles.

Catalog entries are advisory. A model name or successful tool probe never
proves capacity. Gateway aliases require an exact endpoint/protocol match.
"""
from __future__ import annotations

from copy import deepcopy

from .store import digest


PROFILE_VERSION = 'clear-model-profile-v1'
HARNESS_REVISION = '86a402200d95fecef4529d969836f1fe13c6cb0d'
HARNESS_SOURCE = 'https://github.com/deepseek-ai/deepseek-harness/blob/' + HARNESS_REVISION
# Pinned PUBLIC_BASE_URL, DEFAULT_MODELS and DEFAULT_CONTEXT_WINDOW only.
# DEFAULT_MAX_TOKENS is a request default, not a verified model output limit.
OFFICIAL_CATALOG = tuple({
    'base_url': 'https://api.deepseek.com/anthropic', 'protocol': 'anthropic_messages',
    'model': name, 'context_window': 1_000_000,
    'provenance': {'kind': 'advisory_official_catalog', 'revision': HARNESS_REVISION,
                   'sources': [HARNESS_SOURCE + '/packages/llm/llm-deepseek/src/' + source
                               for source in ('models.ts', 'defaults.ts', 'config.ts')]},
} for name in ('deepseek-flash', 'deepseek-v4-pro'))


def positive(value):
    return value if type(value) is int and 0 < value <= 2**53 - 1 else None


def _first_number(containers, keys):
    for container in containers:
        for key in keys:
            value = positive(container.get(key))
            if value is not None:
                return value
    return None


def candidate(raw, identifier=None):
    """Allowlist common bounded metadata, never persist arbitrary provider blobs."""
    if not isinstance(raw, dict):
        return None
    identifier = identifier if identifier is not None else raw.get('id')
    if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256
            or any(ord(char) < 32 for char in identifier)):
        return None
    containers = [raw]
    for name in ('context', 'limits', 'capabilities', 'metadata', 'model_info', 'top_provider'):
        nested = raw.get(name)
        if isinstance(nested, dict):
            containers.append(nested)
            for child in ('context', 'limits'):
                if isinstance(nested.get(child), dict):
                    containers.append(nested[child])
    window = _first_number(containers, ('context_window', 'contextWindow', 'context_length',
                                        'max_context_tokens', 'maxContextTokens'))
    # An input-only limit does not establish the combined context capacity.
    input_limit = _first_number(containers, ('max_input_tokens', 'maxInputTokens'))
    output = _first_number(containers, ('max_output_tokens', 'maxOutputTokens', 'max_completion_tokens',
                                        'maxTokens', 'max_tokens'))
    name = raw.get('name') or raw.get('display_name') or identifier
    name = name if isinstance(name, str) else identifier
    return {'id': identifier, 'name': name[:256], 'context_window': window,
            'max_output_tokens': output, 'max_input_tokens': input_limit,
            'source': 'endpoint_metadata', 'verified_capacity': False}


def catalog_key(config):
    return 'agent-model-catalog:' + digest([config['protocol'], config.get('base_url', '').rstrip('/')])


def _configuration(config):
    """Only fields affecting the profile; no credentials or unrelated policy."""
    local = config.get('runtime_context_cap')
    if local is None:
        local = config.get('context_cap', 65536)
    return deepcopy({
        'model': config['model'], 'protocol': config['protocol'], 'base_url': config.get('base_url', ''),
        'runtime_context_cap': local, 'model_context_window': config.get('model_context_window'),
        'model_max_output_tokens': config.get('model_max_output_tokens'),
        'endpoint_model_metadata': config.get('endpoint_model_metadata') or {},
        'max_output_tokens': config.get('max_output_tokens', 2048),
        'safety_margin_tokens': config.get('safety_margin_tokens'), 'revision': config.get('revision', 0),
    })


def resolve_profile(config, endpoint=None):
    """Keep saved capabilities frozen for the same configuration revision."""
    identity = _configuration(config)
    if endpoint is not None:
        identity['endpoint_model_metadata'] = deepcopy(endpoint)
    fingerprint = digest(identity)
    frozen = config.get('model_profile')
    if (isinstance(frozen, dict) and frozen.get('version') == PROFILE_VERSION
            and frozen.get('configuration_fingerprint') == fingerprint):
        return deepcopy(frozen)
    catalog = next((entry for entry in OFFICIAL_CATALOG
                    if entry['base_url'] == identity['base_url'].rstrip('/')
                    and entry['protocol'] == identity['protocol'] and entry['model'] == identity['model']), {})
    endpoint = identity['endpoint_model_metadata']
    if not isinstance(endpoint, dict) or endpoint.get('id') != identity['model']:
        endpoint = {}
    fields, conflicts = {}, []
    for target, explicit in (('context_window', 'model_context_window'),
                              ('max_output_tokens', 'model_max_output_tokens')):
        options = [(positive(identity.get(explicit)), 'explicit_declaration'),
                   (positive(endpoint.get(target)), 'endpoint_metadata'),
                   (positive(catalog.get(target)), 'identity_matched_catalog')]
        chosen = next(((value, source) for value, source in options if value is not None), (None, 'unknown'))
        fields[target] = {'value': chosen[0], 'source': chosen[1]}
        if len({value for value, _ in options if value is not None}) > 1:
            conflicts.append({'field': target, 'chosen_source': chosen[1],
                              'declarations': {source: value for value, source in options if value is not None}})
    local = positive(identity['runtime_context_cap'])
    reserve = positive(identity['max_output_tokens'])
    if local is None or reserve is None:
        raise ValueError('上下文和输出预算必须是正整数')
    window = fields['context_window']['value']
    effective = min(local, window) if window is not None else local
    margin = identity['safety_margin_tokens']
    if margin is None:
        margin = min(2048, max(256, effective // 32))
    if type(margin) is not int or not 0 <= margin <= 2**53 - 1:
        raise ValueError('上下文安全余量无效')
    profile = {'version': PROFILE_VERSION, 'configuration_fingerprint': fingerprint,
        'model': identity['model'], 'protocol': identity['protocol'], 'context_window': window,
        'model_max_output_tokens': fields['max_output_tokens']['value'],
        'capacity_sources': {key: item['source'] for key, item in fields.items()},
        'capacity_status': 'declared' if window is not None else 'unknown', 'capacity_verified': False,
        'capacity_conflicts': conflicts, 'runtime_context_cap': local,
        'effective_context_window': effective, 'output_reserve': reserve,
        'safety_margin_tokens': margin, 'available_input_tokens': max(0, effective - reserve - margin),
        'connection_revision': identity['revision']}
    if any(item['source'] == 'identity_matched_catalog' for item in fields.values()):
        profile['catalog_provenance'] = deepcopy(catalog['provenance'])
    profile['profile_id'] = digest(profile)
    return profile


def normalize_config(config):
    config = deepcopy(config)
    local = config.get('runtime_context_cap')
    if local is None:
        local = config.get('context_cap', 65536)
    config['runtime_context_cap'] = config['context_cap'] = local
    config['model_profile'] = resolve_profile(config)
    return config
