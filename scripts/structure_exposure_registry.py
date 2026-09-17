"""Conservative historical exposure index, never a claim of new-test eligibility."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from geometry_eval_common import sha,write_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    roots=['build/document-workflow/geometry-holdout/frozen','build/table-matching-v2/dataset',
        'build/table-matching-v2/pages','build/tableformer-expanded-20260914/dataset',
        'build/tableformer-next-20260915/dataset','build/tableformer-next-20260915/public-pdfs',
        'build/structure-workflow-20260916/public-pdfs-02','audit/fusion-20260912/data']
    identities=defaultdict(set);records=[]
    def walk(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if k in ('original_document_id','document_id','group_id','source_pdf_sha256','source_sha256','sha256','id','url') and isinstance(v,str):
                    identities[k].add(v)
                elif isinstance(v,(list,dict)):walk(v)
        elif isinstance(value,list):
            for v in value:walk(v)
    for relative in roots:
        root=ROOT/relative
        paths=sorted(set([*root.glob('*.json'),*root.glob('inputs/*.json'),*root.glob('sources/*/receipt.json'),*root.glob('canonical/*.json')]))
        for path in paths:
            value=json.loads(path.read_text('utf-8'));walk(value)
            records.append({'path':path.relative_to(ROOT).as_posix(),'sha256':sha(path)})
    current=json.loads((ROOT/roots[6]/'inputs/development.json').read_text('utf-8'))['samples']
    duplicates=defaultdict(list)
    for s in current:duplicates[s['source_pdf_sha256']].append(s['original_document_id'])
    write_json(a.output,{'scope':'All listed directories and every recorded original/template are excluded from future independent tests',
        'historical_roots':roots,'source_metadata_files':records,'identities':{k:sorted(v) for k,v in identities.items()},
        'current_named_documents':len({s['original_document_id'] for s in current}),
        'current_unique_pdf_hashes':len(duplicates),'current_pages':len(current),'current_unique_page_images':len({s['sha256'] for s in current}),
        'exact_duplicate_documents':[sorted(set(v)) for v in duplicates.values() if len(set(v))>1],
        'independent_test_eligibility':False,'near_duplicate_new_test_check':'not run because no new sealed independent set exists; protocol requires pHash and layout/text comparison before sealing'})


if __name__=='__main__':main()
