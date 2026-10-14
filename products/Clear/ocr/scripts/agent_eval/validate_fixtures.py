"""Validate frozen dataset hashes/structure without printing held-out content.

Does not run OCR, model calls, fault injection or product acceptance scenarios.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def read(path): return json.loads(path.read_text("utf-8"))


def validate(root):
    manifest=read(root/"fixtures-manifest.json")
    for name,record in manifest["files"].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()): raise ValueError("Manifest path escapes dataset")
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=record["sha256"] or len(data)!=record["bytes"]:
            raise ValueError("Frozen file hash/size mismatch: "+name)
    tasks={s:read(root/s/"tasks.json") for s in ("development","holdout")}
    known_fixtures={f["id"] for f in manifest["fixtures"]}
    seen=set()
    for split,items in tasks.items():
        expected=6 if split=="development" else 4
        if Counter(t["scenario"] for t in items)!={f"S{i:02d}":expected for i in range(1,7)}: raise ValueError("Invalid scenario balance")
        for task in items:
            if task["id"] in seen: raise ValueError("Duplicate task")
            seen.add(task["id"])
            if task["split"]!=split or not set(task["fixtures"])<=known_fixtures: raise ValueError("Invalid fixture/split")
            if task["execution_status"]!="not_tested" or task["real_model_runs"]!=0: raise ValueError("Definition must not claim execution")
            if not task["assertions"] or not task["intent"] or not task["terminal_condition"]: raise ValueError("Incomplete task")
            label=task["initial_state"]["selection_label"].split("/",1)[1]
            if label not in read(root/split/"id-map.json")["labels"]: raise ValueError("Unresolved selection label")
            if task["scenario"]=="S02":
                cases=read(root/split/"FX03.json")["cases"]
                match=[c for c in cases if c["label"]==task["expected"]["template"]]
                if len(match)!=1 or match[0]["reportable_difference"]!=task["expected"]["difference"]: raise ValueError("Financial truth mismatch")
    for key in ("template_group","document_group"):
        if {t[key] for t in tasks["development"]} & {t[key] for t in tasks["holdout"]}: raise ValueError("Split leakage: "+key)
    split=read(root/"evaluation-split.json")
    if {r["id"] for r in split["tasks"]}!=seen: raise ValueError("Split manifest mismatch")
    for name in tasks:
        if hashlib.sha256((root/name/"tasks.json").read_bytes()).hexdigest()!=split["files"][name]["sha256"]: raise ValueError("Task hash mismatch")
    from pypdf import PdfReader
    for name in tasks:
        folder=root/name/"documents"
        native=PdfReader(folder/"native-20.pdf");scan=PdfReader(folder/"scan-8.pdf")
        if len(native.pages)!=20 or len(scan.pages)!=8: raise ValueError("PDF page counts")
        truth=read(folder/"native-truth.json")
        if not all(p["keyword"] in page.extract_text() for p,page in zip(truth["pages"],native.pages)): raise ValueError("Chinese text not searchable")
        if any(p.extract_text().strip() for p in scan.pages): raise ValueError("Scan contains native text")
        if len(read(root/name/"FX08.json")["results"])!=1000: raise ValueError("Stress pages")
    return {"status":"pass","scope":"fixture structural/hash validation only","families":8,"fixture_instances":16,"tasks":60,"development":36,"holdout":24,"pdf_pages":56,"files_verified":len(manifest["files"]),"model_execution":"not_tested","fault_injection":"not_tested","content_quality":"not_tested"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True,help='Frozen fixture directory')
    parser.add_argument('--receipt',type=Path,required=True,help='Write structural validation receipt')
    args=parser.parse_args();result=validate(args.root)
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
