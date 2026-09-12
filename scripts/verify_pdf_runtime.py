"""Validate the delivered CPU runtime without requiring shipping pip."""
import argparse
import hashlib
import importlib.metadata as md
import json
import sys
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
p=argparse.ArgumentParser();p.add_argument('--lock',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
if not sys.flags.isolated:
    raise SystemExit('Run this checker with python.exe -I, matching production workers and excluding user site packages')
lock=json.loads(a.lock.read_text('utf-8'))
installed={canonicalize_name(d.metadata['Name']):d.version for d in md.distributions()}
errors=[]
for row in lock['wheels']:
    if installed.get(canonicalize_name(row['name']))!=row['version']:errors.append({'kind':'locked-version','expected':row,'actual':installed.get(canonicalize_name(row['name']))})
for dist in md.distributions():
    for raw in dist.requires or []:
        requirement=Requirement(raw)
        if requirement.marker and not requirement.marker.evaluate({'extra':''}):continue
        actual=installed.get(canonicalize_name(requirement.name))
        if actual is None or actual not in requirement.specifier:errors.append({'kind':'dependency','package':dist.metadata['Name'],'requires':raw,'installed':actual})
report={'runtime':str(Path(__import__('sys').executable).resolve()),'lock_sha256':hashlib.sha256(a.lock.read_bytes()).hexdigest(),'installed':installed,'errors':errors,'passed':not errors}
a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print('PDF runtime',len(installed),'distributions; errors:',len(errors));raise SystemExit(bool(errors))
