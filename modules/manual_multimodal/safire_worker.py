"""Separate owned entrypoint; the legacy worker and CAT-Net implementation stay intact."""
import json
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker
from worker_protocol import WorkerControl, WorkerStopped


def main():
    worker.deny_network()
    request_path = Path(sys.argv[1]).resolve()
    worker.OUT = request_path.parent
    if request_path.drive.upper() != 'F:':
        raise ValueError('Worker output must remain on F:')
    worker.initialize_temporary_directory(worker.OUT)
    request = json.loads(request_path.read_text('utf-8'))
    worker.CONTROL = WorkerControl(request, worker.save)
    worker.CONTROL.event('ready')
    try:
        worker.CONTROL.wait('start')
        worker.CONTROL.check()
        started = time.monotonic()
        binding = request['model_binding']
        if binding['engine'] == 'safire':
            from safire_processing import analyze
            result = analyze(request, worker)
        elif binding['engine'] == 'catnet':
            result = worker.catnet(request)
            result['engine'] = 'CAT-Net'
        else:
            raise ValueError('Unknown pinned detector engine')
        if request['kind'] == 'document_forensics':
            from safire_processing import classical
            result = classical(request, worker, result)
        worker.CONTROL.check()
        result['elapsed_seconds'] = round(time.monotonic()-started, 3)
        result['input_hashes'] = [row['blob_hash'] for row in request['inputs']]
        result['environment'] = {'python':platform.python_version(),'system':platform.platform(),
            'machine':platform.machine(),'adapter_sha256':worker.digest(__file__),
            'runtime_executable_sha256':worker.digest(sys.executable)}
        result['parameters'] = request['options']
        result['model_binding'] = binding
        result['network_policy'] = 'all_outbound_connections_denied'
        worker.save('result.json', result)
        worker.progress('completed')
        return 0
    except WorkerStopped:
        worker.CONTROL.event('yielded')
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
