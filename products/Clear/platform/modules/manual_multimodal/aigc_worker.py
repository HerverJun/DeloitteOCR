"""Separate gated worker for pinned whole-image AIGC detectors."""
import json
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
import worker
from worker_protocol import WorkerControl, WorkerStopped


def main():
    worker.deny_network()
    request_path=Path(sys.argv[1]).resolve()
    worker.OUT=request_path.parent
    worker.require_path(request_path, 'AIGC request', role='read')
    worker.require_path(worker.OUT, 'AIGC output', role='write')
    worker.initialize_temporary_directory(worker.OUT)
    request=json.loads(request_path.read_text('utf-8'))
    if request.get('kind')!='aigc_image':
        raise ValueError('AIGC worker accepts only aigc_image tasks')
    worker.CONTROL=WorkerControl(request,worker.save)
    worker.CONTROL.event('ready')
    try:
        worker.CONTROL.wait('start')
        worker.CONTROL.check()
        started=time.monotonic()
        from aigc_processing import analyze
        result=analyze(request,worker)
        worker.CONTROL.check()
        result.update(elapsed_seconds=round(time.monotonic()-started,3),
            input_hashes=[row['blob_hash'] for row in request['inputs']],
            parameters=request['options'],model_binding=request['model_binding'],
            network_policy='all_outbound_connections_denied',
            environment={'python':platform.python_version(),'system':platform.platform(),
                'machine':platform.machine(),'adapter_sha256':worker.digest(__file__),
                'runtime_executable_sha256':worker.digest(sys.executable)})
        worker.save('result.json',result)
        worker.progress('completed')
        return 0
    except WorkerStopped:
        worker.CONTROL.event('yielded')
        return 75


if __name__=='__main__':
    raise SystemExit(main())
