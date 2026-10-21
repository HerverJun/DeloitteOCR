"""Native object admission claims shared by direct actions and analysis."""
import json

from .errors import DirectActionFault
FAMILIES = {'image-text-v1': 'ocr', 'pdf-text-v1': 'ocr',
            'classification-predict-v1': 'classification', 'data-action-v1': 'data'}


def resolve_resource(runtime, session, driver, request_id, link_id=None):
    family = FAMILIES[driver]
    if family == 'data':
        return request_id, None  # Each action creates an independent native data session.
    store = runtime.services.workspace_store
    if not hasattr(store, 'list_links'):
        return '*', link_id
    links = [link for link in store.list_links(session['workspace_id']) if link.app_id == family]
    if link_id:
        links = [link for link in links if link.link_id == link_id]
        if len(links) != 1 or links[0].role == 'viewer':
            raise DirectActionFault('原生项目关联不属于当前项目或为只读')
    if len(links) > 1:
        raise DirectActionFault('存在多个原生项目，请明确选择本次使用的项目')
    return ('project:' + links[0].module_project_id, links[0].link_id) if links else ('*', None)


def resolve_harness_scope(runtime, session):
    """The server's accessible write scope, never a model supplied project ID."""
    store = runtime.services.workspace_store
    if not hasattr(store, 'list_links'):
        return {'version': 1, 'workspace_id': session['workspace_id'], 'unknown': True, 'projects': {}}
    projects = {}
    for link in store.list_links(session['workspace_id']):
        if link.role != 'viewer':
            projects.setdefault(link.app_id, []).append(link.module_project_id)
    return {'version': 1, 'workspace_id': session['workspace_id'],
            'projects': {app: sorted(set(values)) for app, values in projects.items()}}


def merge_scope(previous, current):
    """Keep accepted objects protected if a link was removed during execution."""
    output = {**current, 'projects': {app: list(values) for app, values in current.get('projects', {}).items()}}
    for app, values in (previous or {}).get('projects', {}).items():
        output['projects'][app] = sorted(set(output['projects'].get(app, [])) | set(values))
    return output


def _state(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def native_projects(state):
    prepared = state.get('receipts', {}).get('prepared', {})
    prepared = prepared.get('data', prepared)
    if prepared.get('project_id'):
        return {prepared['project_id']}
    resource = state.get('resource_object', '*')
    if resource.startswith('project:'):
        return {resource[8:]}
    if state.get('scope_native_projects'):
        return set(state['scope_native_projects'])
    return None


def conflicts(driver, state, other):
    other_state = _state(other.get('driver_state'))
    if driver == 'data-action-v1' or other['driver'] == 'data-action-v1':
        # Fixed data actions create a run-private native session.
        if driver == 'harness' or other['driver'] == 'harness':
            return False
    if driver == 'harness' and other['driver'] == 'harness':
        left, right = state.get('scope_claims'), other_state.get('scope_claims')
        if not left or not right or left.get('unknown') or right.get('unknown'):
            return True
        return (left['workspace_id'] == right['workspace_id'] or
                any(set(values) & set(right.get('projects', {}).get(app, []))
                    for app, values in left.get('projects', {}).items()))
    if driver == 'harness' or other['driver'] == 'harness':
        scope, direct, native_driver = ((state.get('scope_claims'), other_state, other['driver'])
            if driver == 'harness' else (other_state.get('scope_claims'), state, driver))
        if not scope or scope.get('unknown') or native_driver not in FAMILIES:
            return True
        projects = native_projects(direct)
        workspace = direct.get('scope_workspace_id')
        # Unresolved creation/legacy link claims cover their actual workspace;
        # missing workspace identity remains conservative for historical rows.
        if projects is None:
            return workspace is None or workspace == scope['workspace_id']
        return bool(projects & set(scope.get('projects', {}).get(FAMILIES[native_driver], [])))
    if driver not in FAMILIES or other['driver'] not in FAMILIES:
        return True
    if FAMILIES[driver] != FAMILIES[other['driver']]:
        return False
    current = state.get('resource_object', '*')
    previous = other_state.get('resource_object', '*')
    if FAMILIES[driver] != 'data':
        left, right = native_projects(state), native_projects(other_state)
        if left is not None and right is not None:
            return bool(left & right)
        # New native project creation is private to its actual workspace.
        # Historical rows without an actual object or bounded workspace stay
        # conservative until their live links/prepare receipt resolve them.
        left_workspace, right_workspace = state.get('scope_workspace_id'), other_state.get('scope_workspace_id')
        if left_workspace and right_workspace:
            return left_workspace == right_workspace
        return True
    return current == '*' or previous == '*' or current == previous
