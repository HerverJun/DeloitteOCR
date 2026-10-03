"""OpenAPI declarations for the local audit business workflow."""


def audit_paths(ref, response, body, parameter, todo_query_model):
    prefix = '/api/platform/v1'

    def path_parameters(*names):
        return [parameter(name) for name in names]

    def query(name, schema):
        return {'name': name, 'in': 'query', 'required': False, 'schema': schema}

    def nullable_query_properties(model):
        parameters = []
        for name, source in model.model_json_schema()['properties'].items():
            if name in ('schema_version', 'audit_version'):
                continue
            schema = {key: value for key, value in source.items() if key not in ('title', 'default')}
            if 'anyOf' in schema:
                schema = next(part for part in schema['anyOf'] if part.get('type') != 'null')
            parameters.append(query(name, schema))
        return parameters

    page_query = [
        query('limit', {'type': 'integer', 'minimum': 1, 'maximum': 200}),
        query('cursor', {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,256}$', 'maxLength': 256}),
        query('snapshot_token', {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,256}$', 'maxLength': 256}),
    ]

    def read(operation, result, names, queries=()):
        return {'operationId': operation, 'parameters': path_parameters(*names) + list(queries),
                'responses': {'200': response(result), '400': response('Error', 'Invalid query'),
                              '404': response('Error', 'Unknown identity'),
                              '409': response('Error', 'Version or snapshot conflict'),
                              '503': response('Error', 'Store unavailable')}}

    def write(operation, command, result, names, status='200'):
        return {'operationId': operation, 'parameters': path_parameters(*names),
                'requestBody': body(command),
                'responses': {status: response(result), '400': response('Error', 'Invalid request'),
                              '403': response('Error', 'Origin not permitted'),
                              '404': response('Error', 'Unknown identity'),
                              '409': response('Error', 'Revision, request or publication conflict'),
                              '503': response('Error', 'Store unavailable')}}

    project = prefix + '/audit-projects/{project_id}'
    workspace = prefix + '/workspaces/{workspace_id}'
    paths = {
        workspace + '/audit-projects': {
            'get': read('listAuditProjects', 'AuditProjectPage', ['workspace_id'], page_query),
            'post': write('createAuditProject', 'AuditProjectCreate', 'AuditWriteReceipt', ['workspace_id'], '201')},
        project: {
            'get': read('getAuditProject', 'AuditProject', ['project_id']),
            'post': write('writeAuditProject', 'AuditProjectWrite', 'AuditWriteReceipt', ['project_id'])},
        project + '/records': {
            'post': write('recordAuditPreparationOrReview', 'AuditRecordCreate', 'AuditWriteReceipt', ['project_id'], '201')},
        project + '/history': {'get': read('listAuditHistory', 'AuditHistoryPage', ['project_id'], page_query + [
            query('entity_kind', {'type': 'string', 'enum': ['project', 'procedure', 'finding']}),
            query('entity_id', parameter('entity_id')['schema'])])},
        project + '/requests/{request_id}': {
            'get': read('getAuditRequest', 'AuditRequestStatus', ['project_id', 'request_id'])},
        project + '/evidence/{evidence_id}': {
            'get': read('getAuditEvidenceSnapshot', 'AuditEvidenceSnapshot', ['project_id', 'evidence_id'])},
        project + '/workpapers': {
            'post': write('publishAuditWorkpaper', 'WorkpaperRequest', 'WorkpaperBundle', ['project_id'], '201')},
        project + '/workpapers/{workpaper_id}': {
            'get': read('getAuditWorkpaper', 'WorkpaperBundle', ['project_id', 'workpaper_id'])},
        workspace + '/todos': {
            'get': read('listWorkspaceTodos', 'TodoPage', ['workspace_id'], nullable_query_properties(todo_query_model))},
        workspace + '/todos/{todo_id}': {
            'get': read('locateWorkspaceTodo', 'TodoLocate', ['workspace_id', 'todo_id'], nullable_query_properties(todo_query_model))},
        prefix + '/batches/{batch_id}/items/{item_id}/evidence': {
            'get': read('getBatchItemEvidence', 'ItemEvidence', ['batch_id', 'item_id'], [
                {'name': 'batch_revision', 'in': 'query', 'required': True,
                 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 9_007_199_254_740_991}}])},
    }
    for plural, singular in [('procedures', 'Procedure'), ('findings', 'Finding')]:
        entity_id = singular.lower() + '_id'
        queries = list(page_query)
        if singular == 'Finding':
            queries.append(query('procedure_id', parameter('procedure_id')['schema']))
        paths[project + '/' + plural] = {
            'get': read('listAudit' + plural.capitalize(), 'Audit' + singular + 'Page', ['project_id'], queries),
            'post': write('createAudit' + singular, 'Audit' + singular + 'Create', 'AuditWriteReceipt', ['project_id'], '201')}
        paths[project + '/' + plural + '/{' + entity_id + '}'] = {
            'get': read('getAudit' + singular, 'Audit' + singular, ['project_id', entity_id]),
            'post': write('writeAudit' + singular, 'Audit' + singular + 'Write', 'AuditWriteReceipt', ['project_id', entity_id])}
    return paths
