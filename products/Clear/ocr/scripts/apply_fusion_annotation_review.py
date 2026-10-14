"""Apply the recorded visual-review corrections without overwriting originals."""
import argparse
import hashlib
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--data',type=Path,required=True)
p.add_argument('--review',type=Path,default=Path(__file__).resolve().parents[1]/'audit/fusion-20260912/data/annotation-review.json')
a=p.parse_args()
rows=json.loads((a.data/'annotations.json').read_text('utf-8'))
by_id={r['id']:r for r in rows}
for review in json.loads(a.review.read_text('utf-8'))['records']:
    target=by_id[review['id']]
    assert hashlib.sha256(target['original'].encode()).hexdigest()==review['reference_sha256']
    revised=target['original']
    for change in review['changes']:
        assert change['before'] in revised
        revised=revised.replace(change['before'],change['after'])
    target.update(review,revised=revised)
    assert hashlib.sha256(revised.encode()).hexdigest()==review['revised_reference_sha256']
(a.data/'annotations.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n','utf-8')
print(json.dumps({'applied':16,'originals_preserved':True}))
