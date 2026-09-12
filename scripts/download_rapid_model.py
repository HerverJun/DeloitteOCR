import hashlib
from pathlib import Path
import requests
target=Path(__file__).resolve().parents[1]/'build/document-workflow/rapid-reference/slanet-plus.onnx'
session=requests.Session();session.trust_env=False
url='https://www.modelscope.cn/models/RapidAI/RapidTable/resolve/v2.0.0/slanet-plus.onnx'
with session.get(url,stream=True,timeout=(30,120)) as response:
    response.raise_for_status()
    with target.with_suffix('.part').open('wb') as output:
        for data in response.iter_content(1024*1024):output.write(data)
sha=hashlib.sha256(target.with_suffix('.part').read_bytes()).hexdigest()
assert sha=='d57a942af6a2f57d6a4a0372573c696a2379bf5857c45e2ac69993f3b334514b',sha
target.with_suffix('.part').replace(target)
print(target.stat().st_size,sha)
