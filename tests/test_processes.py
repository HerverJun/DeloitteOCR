import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows job object')
class ProcessTests(unittest.TestCase):
    def test_job_close_terminates_worker_and_descendant(self):
        from ocr_workbench.processes import ProcessJob
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as temp:
            pidfile=Path(temp)/'pid'
            gate=Path(temp)/'gate'
            code='import subprocess,sys,time; from pathlib import Path; p=Path(sys.argv[1]); g=Path(sys.argv[2]);\nwhile not g.exists(): time.sleep(.02)\nc=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); p.write_text(str(c.pid)); time.sleep(60)'
            with ProcessJob() as job:
                proc = subprocess.Popen([sys.executable,'-c',code,str(pidfile),str(gate)])
                job.assign(proc)
                gate.touch()
                deadline=time.monotonic()+10
                while not pidfile.exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue(pidfile.exists())
                handle=api.OpenProcess(0x00100000,False,int(pidfile.read_text()))
                self.assertTrue(handle)
            proc.wait(timeout=10)
            try:
                self.assertEqual(api.WaitForSingleObject(handle,10000),0)
            finally:
                api.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
