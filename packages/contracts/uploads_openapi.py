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
    paths = {path: {
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
    audio = '/api/platform/v1/workspaces/{workspace_id}/assets/audio-uploads/{request_id}'
    audio_responses = {
        '200': response('AudioUploadStatus'),
        '400': response('Error', 'Invalid metadata or content type'),
        '403': response('Error', 'Origin or host not permitted'),
        '404': response('Error', 'Unknown workspace or request'),
        '409': response('Error', 'Binding, chunk hash or offset conflict'),
        '413': response('Error', 'Chunk exceeds 8 MiB'),
        '507': response('Error', 'Insufficient F drive space; preserve request identity'),
        '503': response('Error', 'Store unavailable; query the original request before retrying'),
    }
    paths[audio] = {
        'post': {
            'operationId': 'beginWorkspaceAudioUpload', 'parameters': parameters,
            'description': 'Reserve or replay a fixed audio upload of at most 4 GiB. Only supported audio filenames are accepted. Other upload endpoints retain their 128 MiB limit.',
            'requestBody': {'required': True, 'content': {'application/json': {'schema': ref('AudioUploadBinding')}}},
            'responses': {**audio_responses, '201': response('AudioUploadStatus', 'Reserved audio upload')}},
        'get': {
            'operationId': 'getWorkspaceAudioUpload', 'parameters': parameters,
            'description': 'Read durable next_offset. After a lost response, read this status before retransmitting. Reloading the browser requires reselecting and hashing the same original file.',
            'responses': audio_responses},
    }
    paths[audio + '/chunks'] = {'put': {
        'operationId': 'appendWorkspaceAudioChunk',
        'parameters': parameters + [
            {'name': 'offset', 'in': 'query', 'required': True, 'schema': {'type': 'integer', 'minimum': 0, 'maximum': 4294967296}},
            {'name': 'sha256', 'in': 'query', 'required': True, 'schema': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}},
        ],
        'description': 'Append exactly 8 MiB at next_offset, except the shorter final chunk. Fsync and chunk SHA-256 precede durable acknowledgement. Exact committed chunk replays are idempotent; gaps and altered replays cannot overwrite bytes.',
        'requestBody': {'required': True, 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}},
        'responses': audio_responses,
    }}
    for action in ('complete', 'cancel'):
        paths[audio + '/' + action] = {'post': {
            'operationId': ('complete' if action == 'complete' else 'cancel') + 'WorkspaceAudioUpload',
            'parameters': parameters,
            'description': ('Verify the full file SHA-256 and atomically commit the existing asset registration and immutable receipt.'
                            if action == 'complete' else 'Cancel an uncommitted upload and remove only its temporary bytes. A committed upload keeps its original receipt.'),
            'requestBody': {'required': True, 'content': {'application/json': {'schema': {'type': 'object', 'maxProperties': 0, 'additionalProperties': False}}}},
            'responses': audio_responses,
        }}
    return paths
