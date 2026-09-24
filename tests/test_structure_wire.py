from copy import deepcopy
import unittest

from ocr_workbench.structure_wire import pack,unpack,encode,decode_response,restore,VERSION
from ocr_workbench.geometry_contract import fingerprint


class WireTests(unittest.TestCase):
    def test_roundtrip_reserved_keys_unicode_null_float_and_repeated_identity(self):
        value={'object':[1,2],'ref':7,'text':'0001  −0.01\n收支',
               'cells':[{'literal':'001','blank':'','polygon':[[1.125,2],[3,4]]}]*10,'unknown':None,'flag':False}
        archived=pack(value)
        self.assertEqual(unpack(archived),value)
        for bad in ({'r':-1},{'r':True},{'r':999},{'o':[999]}):
            with self.assertRaises(ValueError):unpack({**archived,'data':bad})
        with self.assertRaises(ValueError):unpack({'schemas':[],'shared':[{'r':0}],'data':{'r':0},'sha256':'x'})

    def fixture(self):
        cells=[{'row':0,'column':i,'row_span':1,'column_span':1,'text':'001',
                'structure_source':{'token_ids':[f'independent-source-{i}']}} for i in range(2)]
        table={'rows':1,'columns':2,'cells':cells}
        candidate=deepcopy(table);candidate['cells'][0]['is_header']=True
        s={'version':'structure-arbitration-v1','table_index':0,'current_table':table,
            'image_sha256':'hash','image_version':'v','candidates':[{'id':'candidate-1','table':candidate,
                'token_ids':['independent-source-0','independent-source-1'],
                'tokens':[{'id':f'independent-source-{i}'} for i in range(2)]}]}
        s['wire_sha256']=fingerprint(encode(s))
        return s

    def test_equal_values_keep_distinct_ids_and_alias_response_hard_checks(self):
        s=self.fixture();w=encode(s)
        self.assertEqual(restore(w['archive'])['current_table'],s['current_table'])
        self.assertEqual(w['candidates'][0]['token_ids'],['t0','t1'])
        self.assertEqual(len(w['candidates'][0]['changes']),1)
        response={'version':VERSION,'decision':'select','candidate_id':'candidate-1','token_ids':['t0','t1'],'reason':'可见标题'}
        self.assertEqual(decode_response(response,s)['token_ids'],s['candidates'][0]['token_ids'])
        for change in ({'token_ids':['t0','t0']},{'token_ids':['t0']},{'token_ids':['t0','t9']},
                       {'candidate_id':'foreign'},{'span':[1,2]},{'decision':'abstain'}):
            with self.assertRaises(ValueError):decode_response({**response,**change},s)
        s['image_version']='stale'
        with self.assertRaises(ValueError):decode_response(response,s)

    def test_changed_archive_and_missing_fields_fail(self):
        archive=pack({'a':'001','b':'001'})
        archive['sha256']='tampered'
        with self.assertRaises(ValueError):unpack(archive)


if __name__=='__main__':unittest.main()
