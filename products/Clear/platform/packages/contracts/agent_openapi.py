"""Agent paths generated beside the same strict request contracts."""


def agent_paths(ref, body, parameter):
    paths = {}
    prefix = '/api/platform/v1'
    definitions = [
        ('/agent/health', 'get', None),
        ('/agent/connection', 'get', None), ('/agent/connection', 'put', 'ControllerDraft'),
        ('/agent/connection', 'delete', None), ('/agent/connection/probe', 'post', 'ControllerDraft'),
        ('/agent/connection/models', 'post', 'ControllerModelsDraft'),
        ('/agent/integrations', 'get', None), ('/agent/integrations', 'put', 'Integrations'),
        ('/workspaces/{workspace_id}/agent/capabilities', 'get', None),
        ('/workspaces/{workspace_id}/agent/references', 'post', 'AgentReferenceRequest'),
        ('/workspaces/{workspace_id}/agent/sessions', 'get', None),
        ('/workspaces/{workspace_id}/agent/sessions', 'post', 'AgentSessionCreate'),
        ('/agent/sessions/{session_id}', 'get', None),
        ('/agent/skills', 'get', None),
        ('/agent/sessions/{session_id}/skills/activate', 'post', 'AgentSkillActivate'),
        ('/agent/sessions/{session_id}/skills/deactivate', 'post', 'AgentSkillDeactivate'),
        ('/agent/sessions/{session_id}/skills/{name}/resources', 'get', None),
        ('/agent/sessions/{session_id}/messages', 'post', 'AgentMessage'),
        ('/agent/sessions/{session_id}/direct/image-text', 'post', 'ImageTextRequest'),
        ('/agent/sessions/{session_id}/direct/pdf-text', 'post', 'PdfTextRequest'),
        ('/agent/sessions/{session_id}/direct/data-action', 'post', 'DataActionRequest'),
        ('/agent/sessions/{session_id}/direct/classification-predict', 'post', 'PredictRequest'),
        ('/agent/sessions/{session_id}/jobs', 'get', None),
        ('/agent/sessions/{session_id}/results/text', 'get', None),
        ('/agent/sessions/{session_id}/events', 'get', None),
        ('/agent/sessions/{session_id}/stream', 'get', None),
        ('/workspaces/{workspace_id}/audit-tasks', 'get', None),
        ('/workspaces/{workspace_id}/audit-profile', 'get', None),
        ('/workspaces/{workspace_id}/audit-profile', 'put', 'AuditProjectProfile'),
        ('/workspaces/{workspace_id}/audit-overview', 'get', None),
        ('/agent/sessions/{session_id}/task', 'get', None),
        ('/agent/sessions/{session_id}/task', 'put', 'AgentTaskWrite'),
        ('/agent/sessions/{session_id}/task/control', 'post', 'AgentTaskControl'),
        ('/agent/sessions/{session_id}/manual-results', 'get', None),
        ('/agent/sessions/{session_id}/manual-return', 'post', 'AgentManualReturn'),
        ('/agent/sessions/{session_id}/native-results', 'get', None),
        ('/agent/sessions/{session_id}/native-results/attach', 'post', 'AgentNativeAttach'),
        ('/workspaces/{workspace_id}/native-results/{binding_id}', 'get', None),
        ('/agent/sessions/{session_id}/findings', 'get', None),
        ('/agent/sessions/{session_id}/findings', 'put', 'AuditDraftWrite'),
        ('/agent/sessions/{session_id}/findings/{draft_id}/revisions/{revision}', 'get', None),
        ('/agent/sessions/{session_id}/evidence', 'post', 'DirectEvidenceRequest'),
        ('/agent/sessions/{session_id}/evidence/{evidence_id}', 'get', None),
        ('/agent/sessions/{session_id}/workpaper', 'post', 'AuditDraftExport'),
        ('/agent/runs/{run_id}', 'get', None), ('/agent/runs/{run_id}/cancel', 'post', 'AgentCancel'),
        ('/agent/runs/{run_id}/resume', 'post', None), ('/agent/decisions/{decision_id}/reply', 'post', 'AgentReply'),
        ('/agent/runs/{run_id}/pause', 'post', None), ('/agent/sessions/{session_id}/compact', 'post', None),
    ]
    import re
    for path, method, schema in definitions:
        parameters = [parameter(name) for name in re.findall(r'{([^}]+)}', path)]
        if path.endswith('/revisions/{revision}'):
            for item in parameters:
                if item['name'] == 'revision':
                    item['schema'] = {'type': 'integer', 'minimum': 1}
            parameters.append({'name': 'sha256', 'in': 'query', 'required': True,
                               'schema': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}})
        if path == '/agent/skills':
            parameters.extend([
                {'name': 'workspace_id', 'in': 'query', 'required': True, 'schema': {'type': 'string'}},
                {'name': 'session_id', 'in': 'query', 'schema': {'type': 'string'}},
                {'name': 'keyword', 'in': 'query', 'schema': {'type': 'string', 'maxLength': 100, 'default': ''}},
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'default': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 20}},
            ])
        if path.endswith('/resources'):
            parameters.extend([
                {'name': 'path', 'in': 'query', 'schema': {'type': 'string', 'maxLength': 200, 'default': 'SKILL.md'}},
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'default': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 16000, 'default': 4000}},
            ])
        if path.endswith('/events') or path.endswith('/stream'):
            parameters.append({'name': 'after', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'default': 0}})
        if path.endswith('/events'):
            parameters.append({'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}})
        if path.endswith('/manual-results'):
            parameters.extend([
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'default': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 50}},
                {'name': 'keyword', 'in': 'query', 'schema': {'type': 'string', 'maxLength': 100, 'default': ''}},
            ])
        if path.endswith('/native-results'):
            parameters.extend([
                {'name': 'application', 'in': 'query', 'schema': {'type': 'string', 'enum': ['ocr', 'classification']}},
                {'name': 'link_id', 'in': 'query', 'schema': {'type': 'string'}},
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'default': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 50}},
            ])
        if path.endswith('/native-results/{binding_id}'):
            parameters.extend([
                {'name': 'session_id', 'in': 'query', 'schema': {'type': 'string'}},
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'default': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 20}},
            ])
        if path.endswith('/sessions') and method == 'get':
            parameters.append({'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'default': 0}})
            parameters.append({'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 50}})
        if path.endswith('/results/text'):
            parameters.extend([
                {'name': 'reference', 'in': 'query', 'required': True, 'schema': {'type': 'string', 'maxLength': 16000}},
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'default': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 32000, 'default': 8000}},
            ])
        response_type = 'text/event-stream' if path.endswith('/stream') else 'application/json'
        status = '201' if schema == 'AgentSessionCreate' else '202' if '/direct/' in path or path.endswith(('/messages', '/compact')) else '200'
        operation = {'tags': ['platform-agent'], 'parameters': parameters,
            'responses': {status: {'description': 'Durable scoped Agent projection',
                'content': {response_type: {'schema': {'type': 'string'} if response_type == 'text/event-stream' else {'type': 'object'}}}}}}
        if schema:
            operation['requestBody'] = body(schema)
        elif path.endswith(('/resume', '/pause', '/compact')):
            operation['requestBody'] = {'required': True, 'content': {'application/json': {
                'schema': {'type': 'object', 'additionalProperties': False}}}}
        paths.setdefault(prefix + path, {})[method] = operation
    return paths
