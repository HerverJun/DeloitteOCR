"""Screen preparation tests use pixels/inference boxes, never OCR references."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from ocr_workbench.coordinates import point
from ocr_workbench.screen_imaging import prepare_screen_image, detect_business_regions


class ScreenImagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def grid(self, image, box, rows=6, columns=4):
        draw = ImageDraw.Draw(image)
        x0,y0,x1,y1 = box
        for row in range(rows+1):
            y = round(y0+(y1-y0)*row/rows)
            draw.line((x0,y,x1,y),fill="black",width=2)
        for col in range(columns+1):
            x = round(x0+(x1-x0)*col/columns)
            draw.line((x,y0,x,y1),fill="black",width=2)
        for row in range(rows):
            for col in range(columns):
                draw.text((x0+8+(x1-x0)*col/columns,y0+8+(y1-y0)*row/rows),f"{row}-{col}",fill="black")

    def run_image(self, image, evidence=None, **kwargs):
        source = self.root/"input.png"
        image.save(source)
        return prepare_screen_image(source,self.root/"prepared",evidence,**kwargs)

    def test_pixel_grids_detect_two_business_tables_not_window_border(self):
        image = Image.new("RGB",(1000,800),"#dddfe4")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20,20,980,775),outline="black",width=2)
        draw.rectangle((20,20,980,95),fill="#434a60")
        draw.text((40,45),"Save 1000 Copy Sum 245.99",fill="white")
        draw.rectangle((65,120,700,420),fill="white")
        draw.rectangle((65,470,700,735),fill="white")
        self.grid(image,(100,160,660,385))
        self.grid(image,(100,510,660,690),rows=4)
        result = self.run_image(image,perspective=False)
        self.assertEqual(len(result["business_regions"]),2)
        self.assertTrue(all(r["table_box"][1] > 140 for r in result["business_regions"]))
        self.assertEqual(result["primary_id"],"original")
        self.assertEqual(len([c for c in result["prepared_images"] if c["kind"]=="table_crop"]),2)

    def test_layout_text_evidence_preserves_title_unit_and_note_excludes_ui(self):
        image = Image.new("RGB",(1000,800),"white")
        self.grid(image,(100,220,800,650))
        evidence = {"layout":[{"kind":"table","box":[100,220,800,650],"score":.94},
                             {"kind":"toolbar","box":[0,0,1000,115]},
                             {"kind":"formula_bar","box":[0,120,1000,150]},
                             {"kind":"title","box":[130,170,770,198],"text":"Sales summary"},
                             {"kind":"unit","box":[650,202,800,216],"text":"unit: yuan"},
                             {"kind":"note","box":[110,680,750,700],"text":"Visible source only"}]}
        result = self.run_image(image,evidence,perspective=False)
        self.assertEqual(len(result["business_regions"]),1)
        region = result["business_regions"][0]
        self.assertLessEqual(region["box"][1],170)
        self.assertGreaterEqual(region["box"][3],700)
        self.assertGreaterEqual(region["box"][1],150)
        self.assertEqual({item["kind"] for item in region["ancillary"]},{"title","unit","note"})
        self.assertEqual(result["primary_id"],"region-1-crop")
        candidate = next(c for c in result["prepared_images"] if c["id"]==result["primary_id"])
        for original in [[100,220],[800,650],[220,400]]:
            roundtrip = point(candidate["prepared_to_source"],point(candidate["source_to_prepared"],original))
            np.testing.assert_allclose(roundtrip,original,atol=1e-7)

    def test_borderless_table_requires_repeated_column_positions(self):
        image = Image.new("RGB",(800,600),"white")
        text = [{"text":str(row)+"A","box":[100,180+row*35,160,200+row*35]} for row in range(6)]
        text += [{"text":str(row)+"B","box":[300,180+row*35,355,200+row*35]} for row in range(6)]
        regions,_,_ = detect_business_regions(image,{"text":text})
        self.assertEqual(len(regions),1)
        self.assertIn("aligned_text",regions[0]["sources"])
        self.assertEqual(regions[0]["observed_text_rows"],6)

    def test_pixel_title_and_note_preserved_without_text_answers(self):
        image = Image.new("RGB",(1000,800),"white")
        self.grid(image,(100,220,800,650))
        draw = ImageDraw.Draw(image)
        draw.text((180,178),"Department revenue summary, unit yuan",fill="black")
        draw.text((120,679),"Note: all amounts are visible source values",fill="black")
        result = self.run_image(image,perspective=False)
        region = result["business_regions"][0]
        self.assertLessEqual(region["box"][1],178)
        self.assertGreaterEqual(region["box"][3],689)
        self.assertTrue(any(item.get("source")=="near_table_pixel_text" for item in region["ancillary"]))

    def test_perspective_has_invertible_explicit_mapping(self):
        image = Image.new("RGB",(900,700),"#454545")
        points = [[80,90],[800,130],[850,630],[120,590]]
        ImageDraw.Draw(image).polygon(points,fill="white")
        result = self.run_image(image,{"screen_polygon":points})
        item = next(c for c in result["prepared_images"] if c["kind"]=="perspective")
        for original in points + [[400,300]]:
            np.testing.assert_allclose(point(item["prepared_to_source"],point(item["source_to_prepared"],original)),original,atol=1e-5)
        transformed = point(item["source_to_prepared"],points[0])
        np.testing.assert_allclose(transformed,[0,0],atol=1e-5)
        self.assertEqual(item["role"],"alternate")

    def test_overlapping_tiles_bounded_and_keep_full_table(self):
        image = Image.new("RGB",(2200,1900),"white")
        evidence = {"layout":[{"kind":"table","box":[40,40,2140,1840],"score":.95}]}
        result = self.run_image(image,evidence,tile_size=1000,overlap=120,max_tiles=12,perspective=False)
        tiles = [c for c in result["prepared_images"] if c["kind"]=="tile"]
        self.assertGreater(len(tiles),1)
        self.assertLessEqual(len(tiles),12)
        self.assertTrue(all(t["role"]=="local_evidence" and t["overlap"]==120 for t in tiles))
        self.assertTrue(any(t["role"]=="complete_region" for t in result["prepared_images"] if "role" in t))
        boxes = [t["source_polygon"] for t in tiles]
        self.assertTrue(any(boxes[0][0][0] < b[0][0] < boxes[0][1][0] for b in boxes[1:]))
        # Re-run with insufficient budget: no partial collection posing as complete.
        limited = self.run_image(image,evidence,tile_size=1000,max_tiles=1,perspective=False)
        self.assertFalse(any(c["kind"]=="tile" for c in limited["prepared_images"]))
        self.assertIn("tile_limit",[w["code"] for w in limited["warnings"]])

    def test_unreadable_and_uncertain_roi_keep_original_without_cv2(self):
        image = Image.new("RGB",(700,500),"white")
        with patch("ocr_workbench.screen_imaging.cv2",None):
            result = self.run_image(image,perspective=False)
        self.assertTrue(result["quality"]["unreadable"])
        self.assertEqual(result["primary_id"],"original")
        self.assertEqual(result["business_regions"],[])
        self.assertIn("roi_uncertain",[w["code"] for w in result["warnings"]])

    def test_region_budget_never_silently_drops_extra_table(self):
        image = Image.new("RGB",(1000,600),"white")
        evidence = {"layout":[{"kind":"table","box":[20+i*300,100,270+i*300,450],"score":.95} for i in range(3)]}
        result = self.run_image(image,evidence,max_regions=2,perspective=False)
        self.assertEqual(len(result["business_regions"]),2)
        self.assertEqual(result["primary_id"],"original")
        self.assertIn("region_limit",[w["code"] for w in result["warnings"]])


if __name__ == "__main__":
    unittest.main()
