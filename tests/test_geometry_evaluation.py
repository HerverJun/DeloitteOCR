import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from geometry_eval_common import ensure_lock,verified_marker,write_json,sha
from diagnose_geometry_legacy import matching_upper_bound
from report_geometry_v2 import score,aggregate
from geometry_reference_identity import associate
from ocr_workbench.coordinates import box_polygon


class EvaluationIntegrityTests(unittest.TestCase):
    def test_replay_matrix_roundtrips_and_resumes_immutable_lock(self):
        from replay_geometry_v2 import candidate_plan
        from ocr_workbench.table_matching import default_policy
        lock={'matrix':candidate_plan(default_policy(),True)}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run-lock.json'
            first=ensure_lock(path,lock)
            self.assertEqual(ensure_lock(path,lock),first)
        self.assertEqual(json.loads(json.dumps(lock)),lock)

    def test_real_result_identity_handles_inserted_row_without_geometry(self):
        target=[{'row':i,'column':0,'row_span':1,'column_span':1,'text':s} for i,s in enumerate(['A','B','C'])]
        actual={'tables':[{'cells':[{'row':i,'column':0,'row_span':1,'column_span':1,'text':s} for i,s in enumerate(['A','Extra','B','C'])]}]}
        mapping=associate(target,actual)
        self.assertEqual(mapping[(0,2,0)],(0,1,0));self.assertNotIn((0,1,0),mapping)

    def test_real_result_symmetric_table_has_no_invented_reference_identity(self):
        target=[{'row':i,'column':0,'row_span':1,'column_span':1,'text':'0'} for i in range(3)]
        self.assertEqual(associate(target,{'tables':[{'cells':target}]}),{})

    def test_changed_run_lock_cannot_reuse_old_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run/run-lock.json';ensure_lock(path,{'code':'v1','input':'a'})
            before=path.read_bytes()
            with self.assertRaises(ValueError):ensure_lock(path,{'code':'v2','input':'a'})
            self.assertEqual(path.read_bytes(),before)

    def test_tampered_output_and_unreceipted_success_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);artifact=folder/'mapping.json';write_json(artifact,{'mappings':[]})
            write_json(folder/'evaluation.json',{'status':'success','run_lock_sha256':'lock','artifact_sha256':{'mapping.json':sha(artifact)}})
            self.assertIsNotNone(verified_marker(folder,'lock',['mapping.json']))
            artifact.write_text('{}','utf-8')
            with self.assertRaises(ValueError):verified_marker(folder,'lock',['mapping.json'])

    def test_oracle_requires_one_to_one_assignment(self):
        self.assertEqual(matching_upper_bound([[0],[0]]),1)
        self.assertEqual(matching_upper_bound([[0,1],[0]]),2)

    def test_all_failed_targets_stay_in_denominator(self):
        targets=[{'row':0,'column':i,'box':[i*10,0,i*10+10,10]} for i in range(2)]
        rows,extra=score({'targets':targets},{'status':'failed','mappings':[]})
        self.assertEqual(sum(r['counts']['targets'] for r in rows),2)
        self.assertEqual(sum(r['counts'].get('no_output',0) for r in rows),2)
        self.assertEqual(extra,0)

    def test_ties_and_extra_cells_are_not_reported_as_success(self):
        targets=[{'row':0,'column':i,'box':[i*10,0,i*10+10,10]} for i in range(2)]
        m={'target':{'kind':'cell','table':0,'row':0,'column':0},'level':'cell','polygon':box_polygon([0,0,20,10])}
        extra={**m,'target':{'kind':'cell','table':0,'row':0,'column':8}}
        rows,extras=score({'targets':targets},{'mappings':[m,extra]})
        self.assertFalse(rows[0]['correct']);self.assertTrue(rows[0]['identity_tie']);self.assertEqual(extras,1)

    def test_text_range_mislabeled_as_cell_fails_independent_scoring(self):
        target={'row':0,'column':0,'box':[0,0,10,10]}
        m={'target':{'kind':'cell','row':0,'column':0},'level':'cell','polygon':box_polygon(target['box']),
           'contract_version':2,'range_semantics':'text_extent','capabilities':{'full_cell':True}}
        rows,_=score({'targets':[target]},{'mappings':[m]})
        self.assertEqual(rows[0]['counts']['offered'],1);self.assertEqual(rows[0]['counts']['correct'],0)
        self.assertEqual(rows[0]['counts']['invalid_geometry'],1)


if __name__=='__main__':unittest.main()
