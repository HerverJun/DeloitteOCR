"""Exercise the frozen header rule against a data-only vocabulary matrix.

This is a component counterexample with explicit synthetic OCR readings. The
grid pixels are real; no model qualification or accuracy claim is made.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from common import fpath, frozen_json
from controlled_recovery import bound, require_environment


def main():
    require_environment()
    output=fpath(sys.argv[1])
    if output.exists():raise ValueError('A new immutable counterexample identity is required')
    output.mkdir(parents=True)
    root=fpath('F:/OCR/output/platform-specialized-agent-20261007')
    rule_path=root/'evaluation/field-header-scope-policy-v2-001/rule.py'
    policy_path=rule_path.with_name('policy.json')
    policy=json.loads(policy_path.read_text('utf-8'))
    if bound(rule_path)!=policy['rule']:raise ValueError('Frozen header rule changed')
    sys.path.insert(0,str(root/'worktrees/ocr/src'))
    from ocr_workbench import screen_diagnostics as native
    from PIL import Image,ImageDraw,ImageFont
    font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',20)
    image=Image.new('RGB',(900,560),'white');draw=ImageDraw.Draw(image)
    draw.text((100,60),'字段词汇样本（无表头，每格均为待核业务值）',fill='black',font=font)
    xs=[100,280,500,780];ys=[150,240,340,460]
    for x in xs:draw.line((x,ys[0],x,ys[-1]),fill='black',width=2)
    for y in ys:draw.line((xs[0],y,xs[-1],y),fill='black',width=2)
    literals=[['项目','部门','科目'],['金额','数量','单价'],['日期','账号','余额']]
    cells=[];readings=[]
    for r,row in enumerate(literals):
        for c,literal in enumerate(row):
            draw.text((xs[c]+20,ys[r]+25),literal,fill='black',font=font)
            cells.append({'row':r,'column':c,'text':literal,'row_span':1,'column_span':1})
            readings.append({'text':literal,'box':[xs[c]+20,ys[r]+25,xs[c]+65,ys[r]+48],
                'confidence':.999,'source':'PP-OCRv6-synthetic-component-reading','kind':'text'})
    path=output/'data-only-no-header.png';image.save(path)
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    evidence={'text':readings,'provenance':{'image_version':'counterexample-v1','contains_reference_input':False}}
    prep={'input_path':str(path),'input_sha256':sha,'width':900,'height':560,
        'quality':{'flags':[]},'business_regions':[{'id':'one','table_box':[xs[0],ys[0],xs[-1],ys[-1]],
            'grid_reliable':True,'observed_grid_rows':3,'observed_grid_columns':3,
            'observed_grid':{'horizontal_lines':ys,'vertical_lines':xs,'source':'pixel_grid',
                'range_semantics':'observed_line_positions','coordinate_space':'exif_normalized_original'}}]}
    raw={'tables':[{'rows':3,'columns':3,'cells':cells}],'project_image_version':'counterexample-v1',
        'screen':{'input_sha256':sha,'prepared':prep,
            'business_metadata_evidence':{'auxiliary_evidence':evidence,'auxiliary_binding_verified':True}}}
    record={'id':'data-only-vocabulary','status':'success','image_sha256':sha,
        'input_version_id':'counterexample-v1','candidate':{'original':raw}}
    frozen_json(output/'source-observations.json',record)
    spec=importlib.util.spec_from_file_location('frozen_header_v2',rule_path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    before=deepcopy(record)
    decision=module.propose(record,native,policy)
    if record!=before:raise ValueError('Scope rule mutated the source')
    if not decision['eligible'] or len(decision['released'])!=3:
        raise ValueError('Counterexample no longer reproduces; review the frozen rule binding')
    report={'format':'header-role-semantic-counterexample-v1','rule':bound(rule_path),'policy':bound(policy_path),
        'source_pixels':bound(path),'observations':bound(output/'source-observations.json'),
        'synthetic_ocr_readings':True,'model_executed':False,'qualification_evidence':False,
        'expected_role':'All nine cells are business values in a vocabulary matrix; no header row exists.',
        'incorrectly_released_data_cells':decision['released'],'decision':decision,
        'grid_verified_from_pixels':decision['grid']['cell_count']==9,
        'header_v2_adoption_rejected':True,'reason':'Closed field words and first-row topology do not prove semantic header role.',
        'native_review_state_changed':False,'holdout_read':False}
    frozen_json(output/'review.json',report)
    print({'incorrectly_released_data_cells':len(decision['released']),'header_v2_adoption_rejected':True})


if __name__=='__main__':main()
