"""Inference-only context, local rereads and independent visual proposals.

GPU sessions are caller-owned so the queue can group all context tasks and reuse
PP-DocLayout/PP-OCR before switching to table transcription. Nothing here starts
a competing GPU worker, downloads a model, or consumes a reference label.
"""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

from ocr_workbench.coordinates import IDENTITY, bounds, box_polygon, polygon

POLICY_VERSION = "screen-evidence-v1"
LABELS = {"table":"table", "doc_title":"title", "paragraph_title":"title", "figure_title":"caption",
          "table_title":"caption", "footnote":"note", "footer":"note", "header":"note", "page_number":"page_number",
          "text":"text", "unit":"unit", "caption":"caption", "note":"note", "title":"title",
          "toolbar":"toolbar", "taskbar":"taskbar", "formula_bar":"formula_bar", "ruler":"ruler",
          "window_border":"window_border", "scrollbar":"scrollbar", "ui":"ui"}
UI_KINDS = {"toolbar","taskbar","formula_bar","ruler","window_border","scrollbar","ui"}


def _box(item):
    value = item.get("box",item.get("coordinate",item.get("bbox")))
    try:
        if value is None and item.get("polygon"):
            value = bounds(item["polygon"])
        if len(value) == 4 and all(math.isfinite(float(v)) for v in value) and value[0] < value[2] and value[1] < value[3]:
            return list(map(float,value))
    except (TypeError,ValueError):
        pass
    return None


def _area(box):
    return (box[2]-box[0])*(box[3]-box[1])


def _intersection(a,b):
    return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))


def _mapped(item, transform, width, height):
    box = _box(item)
    if box is None:
        return None
    mapped_polygon = polygon(transform,item.get("polygon") or box_polygon(box))
    box = bounds(mapped_polygon)
    if box[2] <= 0 or box[3] <= 0 or box[0] >= width or box[1] >= height:
        return None
    clipped = [max(0,box[0]),max(0,box[1]),min(width,box[2]),min(height,box[3])]
    return {**deepcopy(item),"box":clipped,"polygon":mapped_polygon,
            "coordinate_space":"exif_normalized_original", "partially_clipped":clipped != box}


def _sequential_rulers(layout, texts):
    """Classify only ordered ruler tokens outside every detected business table.

    A business number column inside a detected table is never classified as UI.
    A single '1', 'A', or toolbar-like word is insufficient evidence.
    """
    tables = [item["box"] for item in layout if item["kind"]=="table"]
    if not tables:
        return []
    candidates = [item for item in texts if not any(_intersection(item["box"],box)>0 for box in tables)
                  and re.fullmatch(r"\d{1,5}|[A-Z]{1,2}",str(item.get("text","")).strip())]
    groups = []
    for axis in (0,1):
        ordered = sorted(candidates,key=lambda item:((item["box"][axis]+item["box"][axis+2])/2,item["box"][1-axis]))
        bands = []
        for item in ordered:
            other = 1-axis
            center = (item["box"][other]+item["box"][other+2])/2
            band = next((b for b in bands if abs(b["center"]-center) <= max(5,(item["box"][other+2]-item["box"][other])*.55)),None)
            if band is None:
                band = {"center":center,"items":[]}
                bands.append(band)
            band["items"].append(item)
        for band in bands:
            items = sorted(band["items"],key=lambda item:item["box"][axis])
            if len(items) < 4:
                continue
            values = [str(item["text"]).strip() for item in items]
            if all(value.isdecimal() for value in values):
                numbers = [int(value) for value in values]
            elif all(re.fullmatch(r"[A-Z]{1,2}",value) for value in values):
                numbers = [sum((ord(ch)-64)*26**n for n,ch in enumerate(reversed(value))) for value in values]
            else:
                continue
            if any(b-a != 1 for a,b in zip(numbers,numbers[1:])):
                continue
            centers = [(i["box"][axis]+i["box"][axis+2])/2 for i in items]
            spacing = [b-a for a,b in zip(centers,centers[1:])]
            if min(spacing) <= 0 or max(spacing)/min(spacing) > 1.35:
                continue
            box = [min(i["box"][0] for i in items),min(i["box"][1] for i in items),max(i["box"][2] for i in items),max(i["box"][3] for i in items)]
            # Ordered tokens must border a real table, rather than an arbitrary
            # list elsewhere in the document. This is a geometric association.
            nearby = any((axis==1 and box[2] <= tb[0] and tb[0]-box[2] < max(35,(box[2]-box[0])*3)
                          and min(box[3],tb[3])-max(box[1],tb[1]) > (box[3]-box[1])*.7)
                         or (axis==0 and box[3] <= tb[1] and tb[1]-box[3] < max(35,(box[3]-box[1])*3)
                             and min(box[2],tb[2])-max(box[0],tb[0]) > (box[2]-box[0])*.7) for tb in tables)
            if nearby:
                groups.append({"kind":"ruler","box":box,"polygon":box_polygon(box),"source":"ordered_tokens_outside_table",
                               "confidence":None,"inference_basis":"sequence_alignment_spacing_and_table_adjacency"})
                for item in items:
                    item["kind"] = "ruler"
                    item["classification_basis"] = "sequence_alignment_spacing_and_table_adjacency"
    return groups


def normalize_screen_context(context, *, width=None, height=None, candidate=None):
    """Normalize actual context-model output, optionally mapping from a crop.

    Dimensions refer to original image coordinates when ``candidate`` supplied.
    Coordinates outside the image and missing boxes are rejected explicitly.
    Only ordered external ruler evidence is inferred as UI; other text remains.
    """
    if not isinstance(context,dict) or context.get("status") != "success":
        raise ValueError("上下文模型没有成功输出")
    image = context.get("image",{})
    width,height = width or image.get("width"),height or image.get("height")
    if type(width) not in (int,float) or type(height) not in (int,float) or min(width,height) <= 0:
        raise ValueError("缺少当前原图尺寸")
    transform = candidate.get("prepared_to_source") if candidate else IDENTITY
    if not transform:
        raise ValueError("上下文缺少可逆原图坐标映射")
    raw_layout = context.get("layout_blocks",[]) or [dict(item,kind="table") for item in context.get("regions",[])]
    raw_text = context.get("ocr_blocks",context.get("blocks",[]))
    if len(raw_layout) > 1000 or len(raw_text) > 10000:
        raise ValueError("上下文证据超过单图上限")
    layout,text,warnings = [],[],[]
    for kind,items,target in (("layout",raw_layout,layout),("text",raw_text,text)):
        for number,item in enumerate(items):
            if not isinstance(item,dict):
                warnings.append({"code":"invalid_context_item","collection":kind,"index":number})
                continue
            mapped = _mapped(item,transform,width,height)
            if not mapped:
                warnings.append({"code":"invalid_or_outside_context_box","collection":kind,"index":number})
                continue
            label = str(item.get("label",item.get("kind","text")))
            mapped["kind"] = LABELS.get(label,"text")
            mapped["original_label"] = label
            mapped["source"] = item.get("source", "PP-DocLayoutV3" if kind=="layout" else context.get("ocr_source","PP-OCRv6-auxiliary"))
            mapped["confidence"] = item.get("confidence",item.get("score"))
            if kind == "text":
                mapped["text"] = str(item.get("text",""))
            target.append(mapped)
    layout.extend(_sequential_rulers(layout,text))
    return {"policy_version":POLICY_VERSION,"layout":layout,"text":text,"warnings":warnings,
            "provenance":{"component":context.get("component"),"ocr_source":context.get("ocr_source"),
                          "image_version":context.get("image_version"),"source_candidate_id":candidate.get("id") if candidate else "original",
                          "inference_seconds":context.get("inference_seconds"),"load_seconds":context.get("load_seconds"),
                          "contains_reference_input":False}}


def collect_screen_context(input_path, output_dir, adapter, *, image_version=None, prepare=True):
    """Run one image using an already loaded, caller-owned context adapter.

    The existing GPU lease/timeout/cleanup remains with EngineAdapter. Reuse the
    same adapter for a whole group; unload before another GPU model is started.
    """
    from PIL import Image
    from ocr_workbench.image_utils import normalized_rgb
    from ocr_workbench.screen_imaging import prepare_screen_image
    input_path,output_dir = Path(input_path).resolve(),Path(output_dir).resolve()
    if output_dir.drive.upper() != "F:":
        raise ValueError("屏拍上下文产物必须保存于 F 盘")
    if getattr(adapter,"engine",None) != "geometry" or getattr(adapter,"geometry_provider",None) != "context":
        raise ValueError("请提供已配置 context 的几何模型适配器")
    output_dir.mkdir(parents=True,exist_ok=True)
    normalized_path = output_dir/"context-input.png"
    with Image.open(input_path) as image:
        normalized_rgb(image).save(normalized_path,format="PNG")
    context = adapter.recognize(normalized_path,output_dir,geometry_request={"include_all_layout":True,"image_version":image_version})
    evidence = normalize_screen_context(context)
    (output_dir/"screen-context-evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    preparation = prepare_screen_image(input_path,output_dir/"prepared",evidence) if prepare else None
    return {"context":context,"evidence":evidence,"preparation":preparation}


def merge_local_text_evidence(tile_results, *, width, height, max_tokens=2000):
    """Map overlapping tile text back; never concatenate tile rows into tables.

    Identical text in substantially overlapping boxes is the same observation.
    Similar/repeated text in distinct boxes remains distinct, and conflicting
    overlapping readings are preserved for review rather than majority voted.
    """
    if not isinstance(tile_results,list) or len(tile_results) > 16 or not 1 <= max_tokens <= 10000:
        raise ValueError("局部证据预算无效")
    blocks,conflicts,warnings = [],[],[]
    for result in tile_results:
        candidate = result.get("candidate")
        if not candidate or candidate.get("kind") != "tile":
            raise ValueError("局部证据必须带实际分块及坐标映射")
        normalized = normalize_screen_context(result["context"],width=width,height=height,candidate=candidate)
        warnings.extend(normalized["warnings"])
        for block in normalized["text"]:
            overlaps = [old for old in blocks if _intersection(old["box"],block["box"])/min(_area(old["box"]),_area(block["box"])) > .8]
            identical = next((old for old in overlaps if old["text"] == block["text"]),None)
            if identical:
                identical.setdefault("tile_sources",[]).append(candidate["id"])
                continue
            if len(blocks) >= max_tokens:
                warnings.append({"code":"local_token_budget","message":"局部文字证据达到上限；保留整表结果，不声明分块已覆盖全表。"})
                return {"blocks":blocks,"conflicts":conflicts,"warnings":warnings,"can_replace_whole_table":False,"complete":False}
            block["tile_sources"] = [candidate["id"]]
            for old in overlaps:
                if old["text"] != block["text"]:
                    conflicts.append({"code":"overlap_text_conflict","before":old["text"],"alternative":block["text"],
                                      "box":block["box"],"tile_ids":old.get("tile_sources",[])+[candidate["id"]],"resolution":"pending_review"})
            blocks.append(block)
    return {"blocks":blocks,"conflicts":conflicts,"warnings":warnings,"can_replace_whole_table":False,"complete":not warnings}


def build_screen_visual_snapshot(raw, image_info, issues, input_path, *, max_targets=16):
    """Build bounded independent-reading targets with reliable local geometry.

    The model sees an empty ``before`` field and original local pixels, so an OCR
    candidate cannot steer its transcription. Actual current values stay in the
    source_targets audit map, which ReviewPipeline does not send to the model.
    """
    if not 1 <= max_targets <= 32:
        raise ValueError("视觉核对预算无效")
    path = Path(input_path)
    width,height = image_info["width"],image_info["height"]
    targets,source_targets,skipped = [],[],[]
    used = set()
    for issue in issues:
        key = (issue.get("table"),issue.get("row"),issue.get("column"))
        if key in used or any(type(v) is not int for v in key):
            continue
        used.add(key)
        ti,row,column = key
        tables = raw.get("tables",[])
        if not 0 <= ti < len(tables):
            continue
        cell = next((c for c in tables[ti].get("cells",[]) if c.get("row")==row and c.get("column")==column),None)
        box = _box(cell) if cell else None
        if not cell or not box or cell.get("geometry_origin") not in {"direct_detection","verified_cell","native_cell"} or box[0] < 0 or box[1] < 0 or box[2] > width or box[3] > height:
            skipped.append({"target":key,"reason":"no_reliable_local_cell_geometry"})
            continue
        if len(targets) == max_targets:
            skipped.append({"target":key,"reason":"visual_target_budget"})
            continue
        ident = f"screen:{ti}:{row}:{column}"
        targets.append({"id":ident,"before":"","context":"逐字读取此单元格实际可见内容，保留负号、小数点、前导零；模糊或遮挡时返回未知，不推测。",
                        "evidence":{"level":"cell","polygon":cell.get("polygon") or box_polygon(box),"range_semantics":"full_cell"}})
        source_targets.append({"id":ident,"table":ti,"row":row,"column":column,"before":cell.get("text",""),"box":box,"issue_code":issue.get("code")})
    return {"image_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"targets":targets,"source_targets":source_targets,
            "skipped":skipped,"policy_version":POLICY_VERSION,"human_review":"not_performed"}


def review_screen_visual_proposals(session, input_path, snapshot):
    """Use an existing caller-owned Qwen review session; return suggestions only."""
    if not snapshot.get("targets"):
        return {"proposals":[],"skipped":snapshot.get("skipped",[]),"human_review":"not_performed"}
    response = session.review(input_path,snapshot)
    originals = {target["id"]:target for target in snapshot["source_targets"]}
    proposals = []
    for item in response.get("items",[]):
        original = originals[item["target_id"]]
        reading = None if item["decision"] == "uncertain" else item["after"]
        decision = "uncertain" if reading is None else "keep" if reading == original["before"] else "replace"
        proposals.append({**original,"reading":reading,"decision":decision,"reason":item.get("reason"),
                          "automatic_apply":False,"human_review":"not_performed","source":"local_visual_independent_reading"})
    return {"proposals":proposals,"skipped":snapshot.get("skipped",[]),"response":response,"human_review":"not_performed"}
