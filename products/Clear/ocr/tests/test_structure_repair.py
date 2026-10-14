from copy import deepcopy
import json
import sqlite3
import unittest

from test_structure_workflow import table
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.structure_repair import (
    content_baseline, bind_adopted, validate_conservation, make_patch, apply_patch,
)


def sourced(values):
    value=table(values)
    for i,c in enumerate(value['cells']):
        c['structure_source']={'token_ids':['t'+str(i)],'text_source_result':'source','image_version':'v',
            'range_semantics':'full_cell','cell_polygon':box_polygon([c['column']*100,c['row']*40,(c['column']+1)*100,(c['row']+1)*40])}
    return value


def baseline(value):
    return content_baseline(value,result_id='r',revision=3,image_version='v',image_sha256='image-hash')


class AdoptedContentTests(unittest.TestCase):
    def test_native_export_does_not_restore_a_normalized_manual_literal(self):
        from ocr_workbench.structure_export import native_structure_units
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE structure_candidates(id,result_id,version_id,image_sha256,payload)')
            tokens=[{'id':'a','raw_text':'A','source_kind':'native','polygon':box_polygon([0,0,10,10])},
                    {'id':'b','raw_text':'001','source_kind':'native','polygon':box_polygon([12,0,30,10])}]
            db.execute('INSERT INTO structure_candidates VALUES(?,?,?,?,?)',('candidate','r','v','hash',json.dumps({'tokens':tokens})))
            t=table([['A 001']]);t['structure_review']={'candidate_set_id':'candidate'}
            t['cells'][0]['structure_source']={'token_ids':['a','b']}
            result={'id':'r','edited':{'tables':[t]}};version={'id':'v','sha256':'hash'}
            self.assertIn((0,0,0),native_structure_units(db,result,version))
            t['cells'][0]['text']='A  001'
            self.assertEqual(native_structure_units(db,result,version),{})

    def test_legacy_split_cannot_erase_literal_spaces(self):
        from ocr_workbench.structure_diagnostics import preserve_values
        old=table([['A  001']]);old['columns']=2;old['cells'][0]['column_span']=2
        candidate=sourced([['A','001']])
        _,conflicts,_=preserve_values(old,candidate,old,[{'id':'t0','raw_text':'A'},{'id':'t1','raw_text':'001'}])
        self.assertTrue(conflicts)

    def test_legacy_merge_cannot_discard_constituent_identity(self):
        from ocr_workbench.structure_diagnostics import preserve_values
        old=sourced([['100','100']]);candidate=sourced([['100 100']])
        candidate['columns']=2;candidate['cells'][0]['column_span']=2
        candidate['cells'][0]['structure_source']['token_ids']=['t0','t1']
        _,conflicts,_=preserve_values(old,candidate,old,[{'id':'t0','raw_text':'100'},{'id':'t1','raw_text':'100'}])
        self.assertTrue(conflicts)

    def test_same_values_are_distinct_and_namespace_shared_across_candidates(self):
        b=baseline(sourced([['001','001']]))
        self.assertNotEqual(b['contents'][0]['id'],b['contents'][1]['id'])
        one=deepcopy(b['table']);two=deepcopy(one);two['cells'][0]['is_header']=True
        a,errors,_=bind_adopted(b,one);c,errors2,_=bind_adopted(b,two)
        self.assertFalse(errors+errors2)
        self.assertEqual([x['content_fragments'] for x in a['cells']],[x['content_fragments'] for x in c['cells']])
        self.assertTrue(validate_conservation(b,c))

    def test_same_short_id_from_another_source_cannot_move_contents(self):
        b=baseline(sourced([['001','001']]))
        candidate=deepcopy(b['table'])
        for c in candidate['cells']:c['structure_source']['text_source_result']='foreign'
        _,errors,_=bind_adopted(b,candidate)
        self.assertTrue(errors)

    def test_manual_edit_and_clear_override_candidate_ocr(self):
        old=sourced([['009.00','']]);old['cells'][1]['manual_state']='cleared'
        b=baseline(old);candidate=deepcopy(old)
        candidate['cells'][0]['text']='9';candidate['cells'][1]['text']='RESTORED'
        out,errors,_=bind_adopted(b,candidate)
        self.assertFalse(errors);self.assertEqual([c['text'] for c in out['cells']],['009.00',''])
        self.assertTrue(validate_conservation(b,out))

    def test_matching_but_stale_sources_cannot_authorize_current_image_moves(self):
        old=sourced([['A','B']])
        for c in old['cells']:c['structure_source']['image_version']='stale-image'
        b=baseline(old);candidate=deepcopy(old)
        for c in candidate['cells']:c['column']=1-c['column']
        for method in ('source','geometry','adjacency'):
            _,conflicts,_=bind_adopted(b,candidate,method=method)
            self.assertTrue(conflicts,method)
        # A previously serialized patch must also fail acceptance revalidation.
        valid=baseline(sourced([['A','B']]))
        current=deepcopy(valid['table'])
        for c in current['cells']:c['column']=1-c['column']
        out,_,moves=bind_adopted(valid,current)
        for c in out['cells']:c['structure_source']['image_version']='stale-image'
        with self.assertRaisesRegex(ValueError,'correspondence'):
            make_patch(valid,out,moves)

    def test_header_proposals_require_native_adoption_and_preserve_manual_literals(self):
        from ocr_workbench.structure_repair import native_header_patches
        old=sourced([['Name','Amount'],['Account A','0001'],['Account B','-0.01']])
        patches,rejected=native_header_patches(baseline(old))
        self.assertFalse(patches)
        self.assertEqual(rejected[0]['kind'],'native_adoption_evidence_missing')
        for c in old['cells']:
            c['native_content']={'version':'native-adopted-fragments-v1','image_version':'v',
                'text_source_result':'source','literal':c['text'],'fragments':[]}
        old['cells'][3]['text']='0099';old['cells'][3]['manual_state']='edited'
        b=baseline(old);patches,_=native_header_patches(b)
        self.assertTrue(patches)
        for item in patches:
            patch=item['patch'];out=apply_patch(b,patch,revision=3,image_version='v',image_sha256='image-hash')
            self.assertEqual([c['text'] for c in out['cells']],[c['text'] for c in old['cells']])
            self.assertEqual(out['cells'][3]['manual_state'],'edited')
            self.assertLessEqual(len(patch['affected_content_ids']),8)

    def test_unique_text_and_symmetric_repeats_do_not_prove_location(self):
        for values in [[['Unique','Other']],[['100','100']]]:
            b=baseline(table(values));candidate=deepcopy(b['table']);candidate['cells'][0]['column'],candidate['cells'][1]['column']=1,0
            _,errors,_=bind_adopted(b,candidate,method='geometry')
            self.assertTrue(errors)
        self.assertTrue(all(not c['source'] for c in baseline(table([['x']]))['contents']))

    def test_normalization_cannot_hide_changed_literal(self):
        b=baseline(sourced([['Ａ 001','−2.00']]))
        out,_,_=bind_adopted(b,b['table'])
        out['cells'][0]['text']='A001'
        with self.assertRaisesRegex(ValueError,'display'):validate_conservation(b,out)

    def test_fragment_duplicate_and_source_mutation_are_rejected(self):
        b=baseline(sourced([['100','100']]))
        out,_,_=bind_adopted(b,b['table'])
        out['cells'][1]['content_fragments']=deepcopy(out['cells'][0]['content_fragments'])
        with self.assertRaisesRegex(ValueError,'identity'):validate_conservation(b,out)
        out,_,_=bind_adopted(b,b['table'])
        out['cells'][0]['content_fragments'][0]['source']['token_ids']=['foreign']
        with self.assertRaisesRegex(ValueError,'source'):validate_conservation(b,out)

    def test_split_requires_real_fragment_boundary(self):
        old=sourced([['Account Amount']]);old['columns']=2;old['cells'][0]['column_span']=2
        b=baseline(old);candidate=sourced([['Account','Amount']])
        for c in candidate['cells']:c['structure_source']['token_ids']=['t0']
        _,errors,_=bind_adopted(b,candidate)
        self.assertTrue(errors)

    def test_geometry_binding_preserves_literal_without_ocr_vote(self):
        old=sourced([['人工编号0001']]);old['cells'][0]['structure_source']['token_ids']=[]
        b=baseline(old);candidate=sourced([['wrong']])
        out,errors,moves=bind_adopted(b,candidate,method='geometry')
        self.assertFalse(errors);self.assertEqual(out['cells'][0]['text'],'人工编号0001')
        self.assertEqual(moves[0]['evidence'],'independent_full_cell_geometry')

    def test_text_extent_and_foreign_image_cannot_be_full_cell(self):
        for mutate in [{'range_semantics':'text_extent'},{'image_version':'foreign'}]:
            old=sourced([['x']]);old['cells'][0]['structure_source']['token_ids']=[]
            old['cells'][0]['structure_source'].update(mutate)
            _,errors,_=bind_adopted(baseline(old),sourced([['x']]),method='geometry')
            self.assertTrue(errors)

    def test_unchanged_slots_support_multilevel_header_metadata_only(self):
        b=baseline(table([['Parent','Parent'],['A','B']]))
        candidate=deepcopy(b['table']);candidate['cells'][0]['is_header']=True;candidate['cells'][0]['header_role']='column'
        out,errors,moves=bind_adopted(b,candidate,method='adjacency')
        self.assertFalse(errors)
        patch=make_patch(b,out,moves)
        self.assertEqual(patch['affected_content_ids'],[b['contents'][0]['id']])

    def test_revision_image_and_patch_tampering_rejected(self):
        b=baseline(sourced([['x','y']]))
        candidate=deepcopy(b['table']);candidate['cells'][0]['is_header']=True
        out,_,moves=bind_adopted(b,candidate);patch=make_patch(b,out,moves)
        args={'revision':3,'image_version':'v','image_sha256':'image-hash'}
        self.assertEqual(apply_patch(b,patch,**args),out)
        for override in [{'revision':4},{'image_version':'w'},{'image_sha256':'changed'}]:
            with self.assertRaisesRegex(ValueError,'stale'):apply_patch(b,patch,**{**args,**override})
        patch['affected_content_ids']=[]
        with self.assertRaisesRegex(ValueError,'evidence'):apply_patch(b,patch,**args)

    def test_slot_transform_and_ownership_tampering_rejected(self):
        b=baseline(sourced([['001','001']]))
        candidate=deepcopy(b['table']);candidate['cells'][0]['is_header']=True
        out,_,moves=bind_adopted(b,candidate)
        moves[0]['to']=[9,9,1,1]
        with self.assertRaisesRegex(ValueError,'correspondence'):make_patch(b,out,moves)
        out,_,moves=bind_adopted(b,candidate)
        out['cells'][0]['content_fragments'],out['cells'][1]['content_fragments']=out['cells'][1]['content_fragments'],out['cells'][0]['content_fragments']
        with self.assertRaisesRegex(ValueError,'correspondence'):make_patch(b,out,moves)

    def test_title_footnote_and_new_blank_are_not_adopted_content(self):
        b=baseline(sourced([['Account','Amount']]))
        for text in ['2026 Annual report','Footnote: amounts in HKD','']:
            candidate=sourced([['Account','Amount'],[text,'']])
            _,errors,_=bind_adopted(b,candidate)
            self.assertTrue(any(e['kind']=='unowned_destination' for e in errors))

    def test_independent_boundary_blocks_cross_table_absorption(self):
        b=baseline(sourced([['A','B']]))
        candidate=deepcopy(b['table']);candidate['cells'][0]['is_header']=True
        out,_,moves=bind_adopted(b,candidate)
        region={'source':'independent-detector','image_version':'v','polygon':box_polygon([0,0,100,40])}
        with self.assertRaisesRegex(ValueError,'outside'):make_patch(b,out,moves,independent_region=region)
        region['polygon']=box_polygon([0,0,200,40])
        self.assertTrue(make_patch(b,out,moves,independent_region=region))

    def test_full_grid_and_combination_conflicts(self):
        b=baseline(sourced([['A','B']]))
        candidate=deepcopy(b['table']);candidate['cells'][0]['column_span']=2
        _,errors,_=bind_adopted(b,candidate)
        self.assertEqual(errors[0]['kind'],'invalid_grid')
        candidate=deepcopy(b['table']);candidate['cells'].pop()
        _,errors,_=bind_adopted(b,candidate)
        self.assertEqual(errors[0]['kind'],'invalid_grid')

    def test_reindexing_includes_all_moved_contents_and_budget(self):
        b=baseline(sourced([[str(i)] for i in range(10)]))
        candidate=deepcopy(b['table'])
        for c in candidate['cells']:c['row']=9-c['row']
        out,errors,moves=bind_adopted(b,candidate)
        self.assertFalse(errors)
        with self.assertRaisesRegex(ValueError,'impact budget'):make_patch(b,out,moves)
        patch=make_patch(b,out,moves,local=False)
        self.assertEqual(len(patch['affected_content_ids']),10)
        self.assertTrue(all(m['from']!=m['to'] for m in patch['slot_transforms']))


if __name__=='__main__':unittest.main()
