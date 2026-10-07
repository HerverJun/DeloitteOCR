"""Regression scoring cannot hide cell shifts, numeric changes, spans or failures."""
from pathlib import Path
import sys
import copy
import random
import json
import pytest

import numpy as np
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/screen_benchmark'))
from generate import homography,points,corners,render_screen,chinese,DEVELOPMENT_FAMILIES,TEST_FAMILIES
from score import score_sample,summarize
from run import first_pass_images,aggregate_results,validate_context_binding,digest,write
from context import verify_context_record
from review_context import coverage,inspect_preparation
from acceptance import readback

def truth():
    return {'id':'test','cohort':'chinese','level':'normal','group_id':'templateA','source_id':'sourceA','tables':[
        {'rows':2,'columns':2,'cells':[{'row':0,'column':0,'row_span':1,'column_span':2,'text':'金额（元）'},
        {'row':1,'column':0,'row_span':1,'column_span':1,'text':'000001'},
        {'row':1,'column':1,'row_span':1,'column_span':1,'text':'-1,234.56'}]}],
        'context_targets':[],'ui_regions':[{'text':'Excel SUM 100.00'}]}

def result():return {'tables':copy.deepcopy(truth()['tables'])}

def test_transform_maps_whole_ui_and_cells_with_inverse():
    h=homography([[0,0],[100,0],[100,80],[0,80]],[[10,12],[97,4],[92,91],[3,81]])
    for polygon in [corners([2,4,30,32]),corners([40,50,90,75])]:
        assert np.allclose(points(np.linalg.inv(h),points(h,polygon)),polygon,atol=.001)

def test_split_families_and_all_twelve_have_different_structure():
    assert not set(DEVELOPMENT_FAMILIES)&set(TEST_FAMILIES)
    signatures=[]
    for family in DEVELOPMENT_FAMILIES+TEST_FAMILIES:
        _,tables,context,_,_=chinese(family,0,'development' if family in DEVELOPMENT_FAMILIES else 'test')
        signatures.append(tuple((t['rows'],t['columns'],tuple((c['row'],c['column'],c['row_span'],c['column_span'],c['text']) for c in t['cells'] if c['row']==0)) for t in tables))
    assert len(set(signatures))==12

def test_exact_good_and_failure_denominator():
    good=score_sample(truth(),result());failed=score_sample(truth(),None,'failed')
    assert good['metrics']['perfect_images']==1
    report=summarize([good,failed]);assert report['totals']['images']==2
    assert report['ratios']['whole_image_correct_rate']==.5
    assert failed['metrics']['critical_exact_cells']==0
    assert report['independent_source_count']==1

def test_swapped_identifiers_and_amounts_count_as_errors_even_same_text_set():
    value=result();cells=value['tables'][0]['cells'];cells[1]['text'],cells[2]['text']=cells[2]['text'],cells[1]['text']
    scored=score_sample(truth(),value)
    assert scored['metrics']['critical_exact_cells']==0
    assert scored['metrics']['critical_error_images']==1
    assert scored['metrics']['perfect_images']==0

def test_leading_zero_negative_decimal_changes_never_normalized_away():
    value=result();value['tables'][0]['cells'][1]['text']='1';value['tables'][0]['cells'][2]['text']='1,23456'
    assert score_sample(truth(),value)['metrics']['critical_exact_cells']==0

def test_spans_omissions_and_extra_ui_are_separate():
    value=result();value['tables'][0]['cells'][0]['column_span']=1
    value['tables'][0]['cells'].pop();value['tables'][0]['cells'].append({'row':3,'column':0,'text':'Excel'})
    scored=score_sample(truth(),value)['metrics']
    assert scored['exact_span_cells']==1
    assert scored['omitted_cells']==1 and scored['extra_cells']==1 and scored['ui_intrusion_cells']==1
    assert scored['perfect_images']==0

def test_occluded_values_excluded_only_from_visible_denominator():
    sample=truth();sample['tables'][0]['cells'][1]['visibility']='occluded'
    value=result();value['tables'][0]['cells'][1]['text']=''
    metrics=score_sample(sample,value)['metrics']
    assert metrics['critical_cells']==2 and metrics['visible_critical_cells']==1
    assert metrics['critical_exact_cells']==1 and metrics['visible_critical_exact_cells']==1

def test_no_table_ui_hallucination_counts_extra_table():
    sample={**truth(),'tables':[],'expect_no_table':True}
    assert score_sample(sample,{'tables':[]})['metrics']['no_table_correct']==1
    hallucinated=score_sample(sample,result())['metrics'];assert hallucinated['extra_tables']==1 and hallucinated['perfect_images']==0

def test_ui_intrusion_outside_tables_still_fails_whole_image():
    value={**result(),'text':'Excel 文件 开始 =SUM(C2:C11) 300.00'}
    scored=score_sample(truth(),value)['metrics']
    assert scored['critical_exact_cells']==2 and scored['ui_intrusion_text_markers']>0
    assert scored['perfect_images']==0

def test_automatic_delivery_false_negative_is_visible_in_acceptance_metrics():
    value=result();value['tables'][0]['cells'][1]['text']='00001'
    value['quality']={'automatic_delivery':True,'issues':[]}
    metrics=score_sample(truth(),value)['metrics']
    assert metrics['automatic_delivered_images']==1 and metrics['automatic_error_images']==1
    assert metrics['automatic_critical_error_images']==1

def test_real_screen_challenge_mask_and_geometry():
    base,tables,context,_,_=chinese('detail',0,'development')
    im,mapped,ctx,ui,occ,visibility,params,matrix,screen_matrix=render_screen(base,tables,context,'hard',random.Random(13),'occlusion')
    assert occ.getbbox() is not None and occ.size==im.size and visibility.size==im.size
    assert any(c['visibility']=='occluded' for t in mapped for c in t['cells'])
    assert params['effect_order'][0]=='compose_ui' and params['effect_order'][-1]=='camera_blur_jpeg'
    assert not np.allclose(screen_matrix,np.eye(3))

def test_multiple_detected_regions_aggregate_all_tables():
    meta={'primary_id':'full','prepared_images':[{'id':'full','kind':'rectified','path':'F:/full.png'},
        {'id':'one','kind':'table_crop','path':'F:/one.png'},{'id':'two','kind':'table_crop','path':'F:/two.png'}]}
    chosen=first_pass_images(meta);assert [p['id'] for p in chosen]==['one','two']
    merged=aggregate_results([result(),result()],chosen);assert len(merged['tables'])==2
    assert merged['tables'][1]['prepared_image_id']=='two'

def test_xlsx_readback_detects_digit_loss_formula_and_merge_damage(tmp_path):
    from openpyxl import Workbook
    assert tmp_path.resolve().drive.upper()=='F:'
    book=Workbook();sheet=book.active;sheet.title='Table 1'
    sheet.cell(1,1,'金额（元）').data_type='s';sheet.merge_cells('A1:B1')
    sheet.cell(2,1,'000001').data_type='s';sheet.cell(2,2,'-1,234.56').data_type='s'
    target=tmp_path/'actual.xlsx';book.save(target)
    assert readback(target,result())['passes']
    sheet.cell(2,1,1);sheet.unmerge_cells('A1:B1');book.save(target)
    damaged=readback(target,result());kinds={e['kind'] for e in damaged['errors']}
    assert {'literal_value','unsafe_type','merged_spans'}<=kinds

def test_no_formula_execution_allowed_in_xlsx_readback(tmp_path):
    from openpyxl import Workbook
    assert tmp_path.resolve().drive.upper()=='F:'
    value={'tables':[{'rows':1,'columns':1,'cells':[{'row':0,'column':0,'row_span':1,'column_span':1,'text':'=SUM(1,2)'}]}]}
    book=Workbook();book.active.cell(1,1,'=SUM(1,2)');target=tmp_path/'formula.xlsx';book.save(target)
    check=readback(target,value)
    assert any(e['kind']=='unsafe_type' for e in check['errors'])

def saved_context(tmp_path):
    assert tmp_path.resolve().drive.upper()=='F:'
    sample={'id':'input','image_sha256':'sha','width':100,'height':80}
    raw={'status':'success','image_version':'sha','coordinate_space':'exif_normalized_original','image':{'width':100,'height':80}}
    evidence={'layout':[],'text':[],'provenance':{'contains_reference_input':False}}
    write(tmp_path/'context.json',raw);write(tmp_path/'evidence.json',evidence)
    record={'id':'input','status':'success','image_sha256':'sha','context_sha256':digest(tmp_path/'context.json'),'evidence_sha256':digest(tmp_path/'evidence.json')}
    write(tmp_path/'record.json',record)
    return sample,raw,evidence,record

def test_context_identity_hash_and_normalized_evidence_verified(tmp_path):
    sample,raw,evidence,record=saved_context(tmp_path)
    assert verify_context_record(tmp_path,sample,normalize=lambda value:evidence)[1]==raw
    write(tmp_path/'evidence.json',{**evidence,'text':[{'text':'fake'}]})
    with pytest.raises(ValueError,match='Saved evidence changed'):verify_context_record(tmp_path,sample)

def test_context_failed_dimensions_image_version_and_reference_input_rejected(tmp_path):
    sample,raw,evidence,record=saved_context(tmp_path)
    record['status']='failed';write(tmp_path/'record.json',record)
    with pytest.raises(ValueError,match='not successful'):verify_context_record(tmp_path,sample)
    record['status']='success';raw['image_version']='wrong';write(tmp_path/'context.json',raw)
    record['context_sha256']=digest(tmp_path/'context.json');write(tmp_path/'record.json',record)
    with pytest.raises(ValueError,match='image version differs'):verify_context_record(tmp_path,sample)
    raw['image_version']='sha';raw['image']['width']=99;write(tmp_path/'context.json',raw)
    record['context_sha256']=digest(tmp_path/'context.json');write(tmp_path/'record.json',record)
    with pytest.raises(ValueError,match='dimensions differ'):verify_context_record(tmp_path,sample)
    raw['image']['width']=100;write(tmp_path/'context.json',raw);evidence['provenance']['contains_reference_input']=True
    write(tmp_path/'evidence.json',evidence);record.update(context_sha256=digest(tmp_path/'context.json'),evidence_sha256=digest(tmp_path/'evidence.json'));write(tmp_path/'record.json',record)
    with pytest.raises(ValueError,match='isolated from references'):verify_context_record(tmp_path,sample)

def test_context_mismatched_worker_or_dataset_cannot_mix():
    binding={'inputs_sha256':'inputs','worker_source_sha256':'worker','engine_config_sha256':'config'}
    context={**binding,'stage':'screen-context','geometry_provider':'context','labels_or_ideal_regions_provided':False}
    validate_context_binding(context,binding)
    for field in binding:
        with pytest.raises(ValueError,match=field):validate_context_binding({**context,field:'wrong'},binding)
    with pytest.raises(ValueError,match='inference context'):validate_context_binding({**context,'labels_or_ideal_regions_provided':True},binding)

def test_offline_crop_review_preserves_full_field_identity_and_ui():
    selected=[{'id':'crop','kind':'table_crop','source_polygon':[[10,10],[80,10],[80,80],[10,80]]}]
    assert coverage({'polygon':[[20,20],[30,20],[30,30],[20,30]]},selected)==1
    assert coverage({'polygon':[[70,20],[90,20],[90,30],[70,30]]},selected)==.5
    reference={'id':'sample','tables':[{'cells':[{'row':0,'column':0,'text':'001','polygon':[[70,20],[90,20],[90,30],[70,30]],'visibility':'visible'}]}],
               'context_targets':[{'kind':'unit','text':'万元','polygon':[[1,1],[9,1],[9,9],[1,9]]}],
               'ui_regions':[{'kind':'toolbar','polygon':[[10,10],[80,10],[80,20],[10,20]]}]}
    metadata={'primary_id':'crop','prepared_images':selected,'business_regions':[],'excluded_regions':[],'quality':{}}
    report=inspect_preparation(reference,metadata)
    assert report['metrics']['images_with_visible_cell_crop_loss']==1
    assert report['metrics']['images_with_context_crop_loss']==1
    assert report['metrics']['images_with_ui_geometry']==1
    assert report['lost_cells'][0]['geometric_coverage']==.5
