"""Record the Agent's actual visual audit; retain rejected label attempts."""
import json
from complex_table_run import ROOT,BUILD,AUDIT,save,sha,update

# These page crops and all generated cells were inspected in this task.
REJECTED={'hk-budget-2025-p78-t0':'unruled partial table selected; missing logical identity and incomplete grid',
 'hk-audit-85-ch1-p24-t0':'shading creates false slots (12 columns vs 6 visually); duplicated text in native crops',
 'hk-audit-85-ch4-p30-t0':'shading/merged rows yield incomplete grid'}
MULTI_HEADERS={'hk-audit-85-ch2-p17-t0':2,'hk-audit-85-ch3-p19-t0':3,'hk-audit-85-ch4-p28-t0':2}


def main():
    manifest=json.loads((BUILD/'dataset/manifest.json').read_text('utf-8'))
    audits=[]
    for sample in manifest['samples']:
        key=sample['id'];path=ROOT/sample['label'];label=json.loads(path.read_text('utf-8'))
        if key in REJECTED:
            audits.append({'id':key,'label_level':'weak','reason':REJECTED[key],'human_signoff':False})
            continue
        # Each promoted label's visible row/column spans, amounts, signs, units,
        # merged notes and text were checked against the source crop.
        rows=MULTI_HEADERS.get(key,1)
        for c in label['cells']:
            c['is_header']=c['row']<rows
            c['header_ids']=[h['id'] for h in label['cells'] if h['row']<rows and h['id']!=c['id']
                and h['column']<=c['column']<h['column']+h['column_span'] and h['row']<c['row']]
        label.update(label_level='agent_audited',human_signoff=False,
            audit_scope='Visual topology and literal values; native PDF spacing is retained. Not independent official truth.',
            reference_independent_of_pdfplumber=False)
        save(path,label);sample.update(label_level='agent_audited',label_sha256=sha(path))
        audits.append({'id':key,'label_level':'agent_audited','source_crop':sample['crop'],'human_signoff':False,
            'checks':['full visible grid','spans','header hierarchy','literal values and signs','notes and units'],
            'independent_official_reference':False})
    save(BUILD/'dataset/manifest.json',manifest)
    save(BUILD/'dataset/label-audit.json',{'items':audits,'all_candidates_retained':True,'formal_qualified':False})
    save(BUILD/'dataset/qualified-manifest.json',{'agent_audited':[s for s in manifest['samples'] if s['label_level']=='agent_audited'],
        'official_verified':[],'weak':[s for s in manifest['samples'] if s['label_level']=='weak'],
        'formal_quality':'not_met','human_signoff':False})
    update('A05','done_with_gaps',[f'build/{BUILD.name}/dataset/{n}.json' for n in ('label-audit','qualified-manifest')],
        engineering='passed',quality='not_met',next_step='Run exploratory native PDF comparison and historical same-input replay; no formal accuracy claim.')


if __name__=='__main__':main()
