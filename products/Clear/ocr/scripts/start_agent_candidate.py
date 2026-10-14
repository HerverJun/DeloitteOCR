"""Explicit experimental entry, isolated workspace and bundled service runtime."""
import argparse
import msvcrt
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import webbrowser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--data', type=Path, default=Path(os.environ['LOCALAPPDATA']) / 'OfflineOCR-Agent-Experimental/Workspace')
    parser.add_argument('--review-only', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(args.bundle.resolve() / 'app'))
    from ocr_workbench.service import create_app
    import uvicorn
    args.data.mkdir(parents=True, exist_ok=True)
    with (args.data / 'service.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        token = secrets.token_urlsafe(32)
        app = create_app(args.bundle, args.data, token, review_only=args.review_only, agent_enabled=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen(128)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False, timeout_graceful_shutdown=8))
            app.state.shutdown = lambda: setattr(server, 'should_exit', True)
            if not args.no_browser:
                def open_browser():
                    while not server.started and not server.should_exit:
                        time.sleep(.05)
                    if server.started:
                        webbrowser.open(f'http://127.0.0.1:{port}/#token={token}')
                threading.Thread(target=open_browser, daemon=True).start()
            print('Experimental workbench uses a separate workspace. Press Ctrl+C to stop.', flush=True)
            server.run(sockets=[listener])


if __name__ == '__main__':
    main()
