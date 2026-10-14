from datetime import datetime
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[2]
output=root/'results'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
print('第一周兼容性验收：四个真实引擎依次识别文字与表格样本。',flush=True)
print(f'结果目录：{output}',flush=True)
result=subprocess.run([sys.executable,'-X','utf8','-I',str(root/'tools/run_acceptance.py'),
                       '--bundle',str(root),'--output',str(output)])
print('验收通过。' if result.returncode==0 else '验收失败，请查看结果目录中的日志。',flush=True)
raise SystemExit(result.returncode)
