"""Small stdin gate shared by CPU/GPU workers; no model imports before start."""
import hashlib
import json
import os
import queue
import sys
import threading
import time


class WorkerStopped(Exception):
    pass


class WorkerControl:
    def __init__(self, request, publish):
        self.runner = request.get('runner') or {}
        self.epoch = self.runner.get('worker_epoch')
        self.publish = publish
        self.messages = queue.Queue()
        self.enabled = self.runner.get('protocol') == 'clear-gpu-worker-v1'
        self.windows_pipe = None
        self.pending_bytes = b''
        self.pipe_closed = False
        if self.enabled:
            if not isinstance(self.epoch, str) or not self.epoch:
                raise ValueError('runner requires worker_epoch')
            if os.name == 'nt':
                # A background CRT stdin readline can hold a runtime lock while
                # NumPy/Torch/Paddle DLL initialization needs the same CRT. Peek
                # the Win32 pipe and read only available bytes, never block in
                # CRT read during native model imports.
                import ctypes
                import msvcrt
                from ctypes import wintypes
                try:
                    descriptor = sys.stdin.fileno()
                    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
                    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                    peek = kernel.PeekNamedPipe
                    peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                     ctypes.POINTER(wintypes.DWORD)]
                    peek.restype = wintypes.BOOL
                    self.windows_pipe = (descriptor, handle, peek, wintypes.DWORD, ctypes.byref)
                except (OSError, ValueError):
                    pass  # In-memory streams used by protocol tests.
            if self.windows_pipe is None:
                threading.Thread(target=self._read, daemon=True).start()

    def _accept(self, line):
        try:
            value = json.loads(line)
            matched = value.get('worker_epoch') == self.epoch
            self.publish('runner-input.json', {'raw': repr(line), 'worker_epoch': self.epoch,
                'matched': matched, 'command': value.get('command'), 'received_at': time.time()})
            if matched:
                self.messages.put(value.get('command'))
        except (ValueError, AttributeError):
            self.publish('runner-input.json', {'raw': repr(line), 'worker_epoch': self.epoch,
                'matched': False, 'parse_error': True, 'received_at': time.time()})

    def _poll_pipe(self):
        if self.windows_pipe is None or self.pipe_closed:
            return
        descriptor, handle, peek, dword, byref = self.windows_pipe
        while True:
            available = dword()
            if not peek(handle, None, 0, None, byref(available), None):
                self.pipe_closed = True
                self.publish('runner-input.json', {'worker_epoch': self.epoch, 'eof': True,
                                                  'received_at': time.time()})
                self.messages.put('stop')
                return
            if not available.value:
                return
            content = os.read(descriptor, min(available.value, 8192))
            if not content:
                self.pipe_closed = True
                self.messages.put('stop')
                return
            self.pending_bytes += content
            while b'\n' in self.pending_bytes:
                line, self.pending_bytes = self.pending_bytes.split(b'\n', 1)
                self._accept(line.decode('utf-8', errors='replace') + '\n')
            if len(self.pending_bytes) > 65536:
                self._accept(self.pending_bytes.decode('utf-8', errors='replace'))
                self.pending_bytes = b''

    def _read(self):
        for line in sys.stdin:
            self._accept(line)
        self.publish('runner-input.json', {'worker_epoch': self.epoch, 'eof': True,
                                          'received_at': time.time()})
        self.messages.put('stop')  # Parent loss must not leave an orphan GPU task.

    def check(self):
        if not self.enabled:
            return
        self._poll_pipe()
        while True:
            try:
                command = self.messages.get_nowait()
            except queue.Empty:
                return
            self.publish('runner-gate.json', {'worker_epoch': self.epoch, 'operation': 'check',
                'command': command, 'consumed_at': time.time()})
            if command == 'stop':
                raise WorkerStopped('runner requested stop')

    def wait(self, expected):
        if not self.enabled:
            return
        self.publish('runner-gate.json', {'worker_epoch': self.epoch, 'operation': 'wait',
            'expected': expected, 'state': 'waiting', 'published_at': time.time()})
        while True:
            if self.windows_pipe is not None:
                self._poll_pipe()
                try:
                    command = self.messages.get_nowait()
                except queue.Empty:
                    time.sleep(.025)
                    continue
            else:
                command = self.messages.get()
            self.publish('runner-gate.json', {'worker_epoch': self.epoch, 'operation': 'wait',
                'expected': expected, 'command': command, 'state': 'accepted' if command == expected else 'other',
                'consumed_at': time.time()})
            if command == 'stop':
                raise WorkerStopped('runner requested stop')
            if command == expected:
                return

    def event(self, event, **fields):
        self.publish('runner-event.json', {'event': event, 'worker_epoch': self.epoch,
                                         'published_at': time.time(), **fields})

    def committed(self, path, chunk_index, processed_ms, total_ms, event='chunk_committed'):
        with path.open('rb') as stream:
            identity = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.event(event, chunk_index=chunk_index,
                   checkpoint={'path': str(path.resolve()), 'sha256': identity},
                   processed_ms=processed_ms, total_ms=total_ms)
        if event == 'chunk_committed':
            self.wait('continue')
        self.check()
