"""OpenAPI declarations for durable per-file upload and recovery."""


def upload_paths(ref, response, parameter, binding_model):
    path = '/api/platform/v1/workspaces/{workspace_id}/assets/uploads/{request_id}'
    parameters = [parameter('workspace_id'), parameter('request_id')]
    properties = binding_model.model_json_schema()['properties']
    metadata = []
    for name in ('name', 'sha256', 'size_bytes'):
        schema = {key: value for key, value in properties[name].items()
                  if key not in ('title', 'default')}
        metadata.append({'name': name, 'in': 'query', 'required': True, 'schema': schema})
    return {path: {
        'post': {
            'operationId': 'uploadWorkspaceAssetWithRequest',
            'parameters': parameters + metadata,
            'description': (
                'The path request ID and exact binding identify one upload. Every POST, '
                'including a committed replay, validates the actual body size and SHA-256. '
                'Committed status returns the immutable original receipt; pending status '
                'does not prove registration. A rejected status requires transaction proof '
                'of no registration. A missing GET does not prove absence of side effects.'),
            'requestBody': {'required': True, 'content': {
                'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}},
            'responses': {
                '201': response('UploadStatus', 'Newly committed upload'),
                '200': response('UploadStatus', 'Original durable upload status'),
                '400': response('Error', 'Invalid metadata, content type or body'),
                '403': response('Error', 'Origin not permitted'),
                '404': response('Error', 'Unknown workspace'),
                '409': response('Error', 'Request binding or body conflict'),
                '413': response('Error', 'Upload exceeds 128 MB'),
                '503': response('Error', 'Store unavailable; preserve request identity')}},
        'get': {
            'operationId': 'getWorkspaceAssetUpload',
            'parameters': parameters,
            'description': 'Read the original durable request status without uploading or registering.',
            'responses': {
                '200': response('UploadStatus'),
                '403': response('Error', 'Host not permitted'),
                '404': response('Error', 'Request not currently found; effect is not inferred'),
                '503': response('Error', 'Store unavailable')}}}}
