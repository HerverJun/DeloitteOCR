import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ocr_workbench.documents import Documents
from ocr_workbench.store import Store
from ocr_workbench.pdf_export import build_pdf_export, positioned_content
from ocr_workbench.geometry import bind_manual
from ocr_workbench.coordinates import box_polygon

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'build/document-workflow/bundle'
FIXTURES = ROOT / 'build/document-workflow/fixtures'


class PdfContentTests(unittest.TestCase):
    def test_v2_text_extent_cannot_bypass_cell_preflight(self):
        from ocr_workbench.geometry_contract import evidence_v2
        table={'rows':1,'columns':1,'cells':[{'row':0,'column':0,'row_span':1,'column_span':1,'text':'00123'}]}
        result={'original':{'text':'','tables':[table],'blocks':[]},'edited':{'text':'','tables':[table]}}
        poly=box_polygon([5,5,50,20]);details=evidence_v2(content_polygons=[poly],display_polygon=poly,origin='text_extent')
        evidence=[{'target':{'kind':'cell','table':0,'row':0,'column':0},'source':'paddle-table-local-v2','polygon':poly,'details':details}]
        details['level']='cell'
        units,missing,_=positioned_content(result,evidence,{'width':100,'height':50})
        self.assertEqual(units,[]);self.assertEqual(len(missing),1)

    def test_duplicate_cell_ranges_do_not_create_duplicate_pdf_units(self):
        table={'rows':1,'columns':2,'cells':[{'row':0,'column':i,'row_span':1,'column_span':1,'text':str(i)} for i in range(2)]}
        result={'original':{'text':'','tables':[table],'blocks':[]},'edited':{'text':'','tables':[table]}}
        evidence=[{'target':{'kind':'cell','table':0,'row':0,'column':i},'source':'legacy','polygon':box_polygon([0,0,100,40]),'details':{'level':'cell'}} for i in range(2)]
        units,missing,_=positioned_content(result,evidence,{'width':100,'height':50})
        self.assertEqual(len(units),1);self.assertEqual(len(missing),1)

    def test_partial_manual_binding_does_not_duplicate_native_visible_text(self):
        raw = {'text': 'Account 00123', 'tables': [], 'blocks': [{'text': 'Account 00123', 'source': 'pdf-native', 'polygon': box_polygon([10,10,190,30])}]}
        evidence = [{'target': {'kind':'text','start':8,'end':13},'source':'manual','polygon':box_polygon([100,10,190,30])}]
        units,missing,changed = positioned_content({'original':raw,'edited':{'text':raw['text'],'tables':[]}},evidence,{'width':200,'height':100})
        self.assertFalse(changed)
        self.assertEqual(missing,[])
        self.assertEqual(len(units),1)
        self.assertTrue(units[0]['native_preserved'])

    def test_removed_native_table_forces_rebuild_even_with_other_native_text(self):
        raw = {'text':'Footer','tables':[{'rows':1,'columns':1,'cells':[{'row':0,'column':0,'row_span':1,'column_span':1,'text':'Removed'}]}],
               'blocks':[{'text':'Footer','source':'pdf-native','polygon':box_polygon([10,60,100,80])}],
               'document':{'table_native_cells':{'0:0:0':{'text':'Removed','units':[]}}}}
        units,missing,changed=positioned_content({'original':raw,'edited':{'text':'Footer','tables':[]}},[],{'width':200,'height':100})
        self.assertTrue(changed)
        self.assertEqual(missing,[])
        self.assertEqual([u['text'] for u in units],['Footer'])

    def test_new_unbound_text_is_listed_and_manual_binding_covers_it(self):
        raw = {'text': '00123', 'tables': [], 'blocks': [{'text': '00123', 'polygon': box_polygon([10,10,90,25])}]}
        result = {'original': raw, 'edited': {'text': '00123\nAdded', 'tables': []}}
        units, missing, _ = positioned_content(result, [], {'width': 200, 'height': 100})
        self.assertEqual(len(units), 1)
        self.assertEqual(missing[0]['text'], 'Added')
        evidence = [{'target': {'kind': 'text', 'start': 6, 'end': 11}, 'source': 'manual', 'polygon': box_polygon([10,30,90,50])}]
        self.assertEqual(positioned_content(result, evidence, {'width': 200, 'height': 100})[1], [])

    def test_whole_table_fallback_cannot_masquerade_as_cell_geometry(self):
        table = {'rows': 1, 'columns': 1, 'cells': [{'row': 0, 'column': 0, 'row_span': 1, 'column_span': 1, 'text': '00123'}]}
        result = {'original': {'text': '', 'tables': [table], 'blocks': []}, 'edited': {'text': '', 'tables': [table]}}
        evidence = [{'target': {'kind': 'cell', 'table': 0, 'row': 0, 'column': 0}, 'source': 'paddle-table-v2', 'polygon': box_polygon([0,0,100,50]), 'details': {'level': 'region'}}]
        self.assertEqual(len(positioned_content(result, evidence, {'width': 100, 'height': 50})[1]), 1)


@unittest.skipUnless((BUNDLE / 'fonts/NotoSansSC-Regular.ttf').exists(), 'Prepare the locked PDF runtime and fonts')
class PdfExportRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'data')
        self.project = self.store.project('PDF 导出')
        self.manager = Documents(self.store, BUNDLE)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def process(self, filename):
        doc = self.manager.import_document(self.project['id'], filename, FIXTURES / filename, dpi=72)
        page = self.store.document_pages(doc['id'])[0]
        stage = self.manager.process(page['id'], 'native')
        self.manager.step()
        finished = self.store.one('document_stages', stage)
        self.assertEqual(finished['status'], 'succeeded', finished['error'])
        return doc, page, json.loads(finished['output'])['result_id']

    def inspect(self, pdf, source=None):
        program = '''import json,sys,pypdfium2 as f,pikepdf
from PIL import ImageChops
pdf=f.PdfDocument(sys.argv[1]); p=pdf[0]; text=p.get_textpage(); data={'text':text.get_text_range(),'pages':len(pdf)}
raw=f.raw;data['characters']=[{'text':chr(raw.FPDFText_GetUnicode(text.raw,i)),'box':list(text.get_charbox(i))} for i in range(text.count_chars())];text.close()
if len(sys.argv)>2:
 original=f.PdfDocument(sys.argv[2]); q=original[0]; a=p.render(scale=1).to_pil();b=q.render(scale=1).to_pil();data['same_pixels']=a.size==b.size and ImageChops.difference(a.convert('RGB'),b.convert('RGB')).getbbox() is None;q.close();original.close()
p.close();pdf.close()
with pikepdf.open(sys.argv[1]) as pdf:data['sources']=json.loads(pdf.attachments['OCR-sources.json'].get_file().read_bytes())
print(json.dumps(data,ensure_ascii=False))'''
        command = [str(BUNDLE / 'runtimes/pdf/python.exe'), '-X', 'utf8', '-c', program, str(pdf)]
        if source: command.append(str(source))
        completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_multiple_documents_zip_has_independent_pdfs_and_source_manifests(self):
        import zipfile
        first,_,_=self.process('native.pdf')
        second,_,_=self.process('blank.pdf')
        archive=build_pdf_export(self.store,self.manager,{'document_ids':[first['id'],second['id']]})
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(len([n for n in z.namelist() if n.endswith('.pdf')]),2)
            self.assertEqual(len([n for n in z.namelist() if n.endswith('.sources.json')]),2)
            for name in z.namelist():
                if name.endswith('.sources.json'):self.assertEqual(len(json.loads(z.read(name))['pages']),1)

    def test_tiff_page_rebuild_keeps_selected_image_and_search_text(self):
        from ocr_workbench.imaging import transform
        doc=self.manager.import_document(self.project['id'],'multipage.tiff',FIXTURES/'multipage.tiff',dpi=72)
        page=self.store.document_pages(doc['id'])[1];image=self.manager.ensure_rendered(page['id'])
        version=transform(self.store,image['active_version'],{'kind':'rotate','degrees':90})
        task=self.store.enqueue(self.project['id'],[version['id']],['ppocr'])[0];self.store.claim()
        self.store.complete(task,{'engine':'ppocr','text':'00123','tables':[],'blocks':[{'text':'00123','polygon':box_polygon([5,5,70,25])}]})
        pdf=build_pdf_export(self.store,self.manager,{'page_ids':[page['id']]})
        data=self.inspect(pdf)
        self.assertIn('00123',data['text'])
        self.assertEqual(data['sources']['pages'][0]['mode'],'rebuilt')
        self.assertEqual(data['sources']['pages'][0]['version_id'],version['id'])
        program='''import sys,pypdfium2 as f
from PIL import Image,ImageChops
p=f.PdfDocument(sys.argv[1]);q=p[0];a=q.render(scale=1).to_pil().convert('RGB');b=Image.open(sys.argv[2]).convert('RGB');assert a.size==b.size;assert ImageChops.difference(a,b).getbbox() is None;q.close();p.close()'''
        check=subprocess.run([str(BUNDLE/'runtimes/pdf/python.exe'),'-X','utf8','-c',program,str(pdf),str(self.store.file(version['path']))],capture_output=True,text=True,timeout=60)
        self.assertEqual(check.returncode,0,check.stderr)

    def test_encrypted_export_requires_ephemeral_password_and_missing_glyph_fails(self):
        password='temporary-fixture-secret'
        doc=self.manager.import_document(self.project['id'],'encrypted.pdf',FIXTURES/'encrypted.pdf',dpi=72,password=password)
        page=self.store.document_pages(doc['id'])[0];stage=self.manager.process(page['id'],'native');self.manager.step()
        result=json.loads(self.store.one('document_stages',stage)['output'])['result_id']
        pdf=build_pdf_export(self.store,self.manager,{'document_ids':[doc['id']]})
        self.assertTrue(self.inspect(pdf)['text'])
        self.manager.passwords.clear()
        with self.assertRaises(ValueError):build_pdf_export(self.store,self.manager,{'document_ids':[doc['id']]})
        self.manager.unlock(doc['id'],password)
        value=self.store.result(result);text=value['edited']['text']+'\n\U0010ffff'
        self.store.save(result,{'text':text,'tables':[]},0)
        bind_manual(self.store,result,{'revision':1,'version_id':value['original']['project_image_version'],'target':{'kind':'text','start':len(text)-1,'end':len(text)},'polygon':box_polygon([10,120,80,145])})
        with self.assertRaisesRegex(ValueError,'U\\+10FFFF'):build_pdf_export(self.store,self.manager,{'document_ids':[doc['id']]})

    def test_export_capture_does_not_follow_later_edits(self):
        from ocr_workbench.pdf_export import capture_pdf
        doc,_,result=self.process('native.pdf')
        folder=Path(self.temp.name)/'snapshot';folder.mkdir()
        captured=capture_pdf(self.store,{'document_ids':[doc['id']]},folder)
        spec=Path(captured['documents'][0]['pages'][0]);before=spec.read_bytes()
        value=self.store.result(result)
        self.store.save(result,{'text':value['edited']['text']+'\nLATER','tables':[]},0)
        self.assertEqual(spec.read_bytes(),before)
        self.assertEqual(json.loads(before)['revision'],0)
        self.assertNotIn('LATER',before.decode())

    def test_native_export_preserves_render_and_long_identifiers_and_is_idempotent(self):
        doc, page, result = self.process('native.pdf')
        self.assertEqual(self.manager.process(page['id'], 'native'), self.store.rows('SELECT id FROM document_stages')[0]['id'])
        self.assertTrue(build_pdf_export(self.store, self.manager, {'document_ids': [doc['id']]}, preflight=True)['ready'])
        pdf = build_pdf_export(self.store, self.manager, {'document_ids': [doc['id']]})
        data = self.inspect(pdf, FIXTURES / 'native.pdf')
        self.assertTrue(data['same_pixels'])
        self.assertIn('00123456789012345678', data['text'])
        self.assertEqual(data['sources']['pages'][0]['mode'], 'original-page-with-text-layer')

    def test_native_edit_rebuilds_only_page_and_chinese_is_searchable(self):
        doc, page, result_id = self.process('native.pdf')
        result = self.store.result(result_id)
        edit = result['edited']
        edit['text'] += '\n中文金额 ￥001.00'
        saved = self.store.save(result_id, edit, result['revision'])
        preflight = build_pdf_export(self.store, self.manager, {'page_ids': [page['id']]}, preflight=True)
        self.assertFalse(preflight['ready'])
        target = {'kind': 'text', 'start': len(result['original']['text'])+1, 'end': len(edit['text'])}
        bind_manual(self.store, result_id, {'revision': saved['revision'], 'version_id': self.store.one('pages', page['id'])['image_id'] and self.store.rows('SELECT active_version FROM images')[0]['active_version'],
                  'target': target, 'polygon': box_polygon([20,200,220,225])})
        pdf = build_pdf_export(self.store, self.manager, {'result_ids': [result_id]})
        data = self.inspect(pdf)
        self.assertIn('中文金额', data['text'])
        self.assertIn('001.00', data['text'])

    def test_changed_visible_native_text_rebuilds_and_removes_old_search_text(self):
        doc, page, result_id = self.process('native.pdf')
        result = self.store.result(result_id)
        text = result['edited']['text']
        changed = text.replace('00123456789012345678', '00999999999999999999')
        self.store.save(result_id, {'text': changed, 'tables': []}, 0)
        preflight = build_pdf_export(self.store, self.manager, {'result_ids': [result_id]}, preflight=True)
        if not preflight['ready']:
            # Changing part of a line needs an explicit current binding when
            # the old word range cannot be conservatively transferred.
            for item in preflight['failures'][0]['items']:
                target = item.get('target')
                if target and target['kind'] == 'text':
                    bind_manual(self.store, result_id, {'revision': 1, 'version_id': self.store.rows('SELECT active_version FROM images')[0]['active_version'], 'target': target, 'polygon': box_polygon([20,60,280,90])})
        pdf = build_pdf_export(self.store, self.manager, {'result_ids': [result_id]})
        data = self.inspect(pdf)
        self.assertIn('00999999999999999999', data['text'])
        self.assertNotIn('00123456789012345678', data['text'])
        self.assertEqual(data['sources']['pages'][0]['mode'], 'rebuilt')

    def test_crop_rotations_overlay_readback_and_visible_pixels(self):
        for angle in (0, 90, 180, 270):
            with self.subTest(angle=angle):
                name = f'crop-{angle}.pdf'
                doc, page, result_id = self.process(name)
                result = self.store.result(result_id)
                version = self.store.one('versions', result['original']['project_image_version'])
                edit = result['edited']; edit['text'] += '\n000NEW'
                saved = self.store.save(result_id, edit, 0)
                bind_manual(self.store, result_id, {'revision': saved['revision'], 'version_id': version['id'],
                    'target': {'kind': 'text', 'start': len(edit['text'])-6, 'end': len(edit['text'])},
                    'polygon': box_polygon([10,10,min(110,version['width']-1),30])})
                pdf = build_pdf_export(self.store, self.manager, {'result_ids': [result_id]})
                data = self.inspect(pdf, FIXTURES/name)
                self.assertIn('000NEW', data['text'])
                self.assertTrue(data['same_pixels'])
                from ocr_workbench.coordinates import bounds, polygon
                from ocr_workbench.geometry import iou
                characters = data['characters']
                position = ''.join(c['text'] for c in characters).index('000NEW')
                boxes = [c['box'] for c in characters[position:position+6]]
                pdf_box = [min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
                matrix = json.loads(self.store.page_for_version(version['id'])['pdf_to_pixel'])
                actual = bounds(polygon(matrix, box_polygon(pdf_box)))
                self.assertGreater(iou(actual,[10,10,min(110,version['width']-1),30]), .5, (angle,actual))

    def test_old_ocr_layer_is_removed_while_the_scan_pixels_stay_identical(self):
        from ocr_workbench.page_processing import complete_region_task, finalize_waiting_pages
        name = 'old-ocr.pdf'
        doc = self.manager.import_document(self.project['id'], name, FIXTURES/name, dpi=72)
        page = self.store.document_pages(doc['id'])[0]
        stage = self.manager.process(page['id'])
        self.manager.step()
        while task := self.store.claim():
            complete_region_task(self.store, task, {'engine': 'ppocr', 'text': 'NEW00123', 'tables': [],
                'blocks': [{'text': 'NEW00123', 'polygon': box_polygon([1,1,30,8])}]})
        finalize_waiting_pages(self.manager)
        result_id = json.loads(self.store.one('document_stages', stage)['output'])['result_id']
        pdf = build_pdf_export(self.store, self.manager, {'result_ids': [result_id]})
        data = self.inspect(pdf, FIXTURES/name)
        self.assertIn('NEW00123', data['text'])
        self.assertIn('Page 001', data['text'])
        self.assertNotIn('OLD WRONG OCR', data['text'])
        self.assertEqual(data['sources']['pages'][0]['mode'], 'original-page-with-text-layer')
        self.assertTrue(data['same_pixels'])

    def test_user_unit_render_matches_metadata_and_export_highlight(self):
        doc, page, result = self.process('user-unit.pdf')
        version = self.store.one('versions', self.store.result(result)['original']['project_image_version'])
        metadata = json.loads(self.store.one('pages', page['id'])['render_parameters'])
        self.assertEqual((version['width'],version['height']), (metadata['width'], metadata['height']))
        pdf = build_pdf_export(self.store, self.manager, {'result_ids': [result]})
        self.assertTrue(self.inspect(pdf, FIXTURES/'user-unit.pdf')['same_pixels'])

    def test_partial_export_keeps_page_order_metadata_bookmarks_and_valid_links(self):
        source = Path(self.temp.name)/'navigation.pdf'
        program = '''import sys,pikepdf
from fpdf import FPDF
p=FPDF(unit='pt',format=(300,400));p.set_auto_page_break(False);p.set_title('Navigation fixture')
for n in range(3):p.add_page();p.set_font('Helvetica',size=12);p.text(30,50,'Page '+str(n+1))
p.output(sys.argv[1]+'.base')
with pikepdf.open(sys.argv[1]+'.base') as d:
 with d.open_outline() as o:
  for n in range(3):o.root.append(pikepdf.OutlineItem('Page '+str(n+1),n))
 annotations=[]
 for n in (1,2):annotations.append(d.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot,Subtype=pikepdf.Name.Link,Rect=[10,10,100,25],Dest=[d.pages[n].obj,pikepdf.Name.Fit])))
 annotations.append(d.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot,Subtype=pikepdf.Name.Link,Rect=[10,30,100,45],A=pikepdf.Dictionary(S=pikepdf.Name.URI,URI='https://example.org'))))
 d.pages[0].obj.Annots=pikepdf.Array(annotations);d.save(sys.argv[1])'''
        completed = subprocess.run([str(BUNDLE/'runtimes/pdf/python.exe'), '-X','utf8','-c',program,str(source)], capture_output=True,text=True,timeout=60)
        self.assertEqual(completed.returncode,0,completed.stderr)
        doc = self.manager.import_document(self.project['id'], source.name, source, dpi=72)
        for page in self.store.document_pages(doc['id']):
            self.manager.process(page['id'],'native');self.manager.step()
        pdf = build_pdf_export(self.store,self.manager,{'document_ids':[doc['id']],'page_numbers':[3,1]})
        inspect = '''import sys,json,pikepdf,pypdfium2 as f
with pikepdf.open(sys.argv[1]) as d:
 with d.open_outline() as o:titles=[i.title for i in o.root]
 data={'title':str(d.docinfo.Title),'bookmarks':titles,'links':len(d.pages[0].obj.Annots),'destination_is_second':d.pages[0].obj.Annots[0].Dest[0].objgen==d.pages[1].obj.objgen}
p=f.PdfDocument(sys.argv[1]);data['text']=[]
for i in range(len(p)):
 q=p[i];t=q.get_textpage();data['text'].append(t.get_text_range());t.close();q.close()
p.close();print(json.dumps(data))'''
        completed = subprocess.run([str(BUNDLE/'runtimes/pdf/python.exe'),'-X','utf8','-c',inspect,str(pdf)],capture_output=True,text=True,timeout=60)
        self.assertEqual(completed.returncode,0,completed.stderr)
        data=json.loads(completed.stdout)
        self.assertEqual(data['bookmarks'],['Page 1','Page 3'])
        self.assertEqual(data['links'],2)
        self.assertTrue(data['destination_is_second'])
        self.assertEqual(data['title'],'Navigation fixture')
        self.assertEqual([v.strip() for v in data['text']],['Page 1','Page 3'])
