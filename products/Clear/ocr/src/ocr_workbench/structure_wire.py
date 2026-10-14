"""Lossless shared JSON evidence and readable, identity-bound review views."""
from collections import Counter
from copy import deepcopy
import json

from ocr_workbench.geometry_contract import fingerprint

VERSION = 'structure-evidence-v2'


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def pack(value):
    """Intern repeated strings/containers and object field schemas; omit nothing."""
    counts = Counter()
    field_counts, scalar_values = {}, {}
    def count(node):
        if isinstance(node, (dict, list, str, float)):
            key = dumps(node)
            if len(key) > (9 if isinstance(node,float) else 24): counts[key] += 1
        if isinstance(node, dict):
            shape = tuple(node)
            fields = field_counts.setdefault(shape, [Counter() for _ in node])
            for index, child in enumerate(node.values()):
                if not isinstance(child,(dict,list)):
                    key = dumps(child);fields[index][key] += 1;scalar_values[key] = child
            for child in node.values(): count(child)
        elif isinstance(node, list):
            for child in node: count(child)
    count(value)
    shared, shared_ids, schemas, schema_ids = [], {}, [], {}
    def encode(node, definition=False):
        if isinstance(node, (dict, list, str, float)):
            key = dumps(node)
            if not definition and counts[key] > 1:
                if key not in shared_ids:
                    index = len(shared);shared_ids[key] = index;shared.append(None)
                    shared[index] = encode(node, True)
                return {'r':shared_ids[key]}
        if isinstance(node, dict):
            keys = tuple(node)
            if keys not in schema_ids:
                schema_ids[keys] = len(schemas)
                defaults = {str(i):scalar_values[c.most_common(1)[0][0]] for i,c in enumerate(field_counts[keys])
                            if c and c.most_common(1)[0][1] >= 2}
                schemas.append([list(keys),defaults])
            defaults = schemas[schema_ids[keys]][1]
            present = [(i,v) for i,v in enumerate(node.values()) if str(i) not in defaults or dumps(v) != dumps(defaults[str(i)])]
            return {'o':[schema_ids[keys],sum(1<<i for i,_ in present),*[encode(v) for _,v in present]]}
        if isinstance(node, list):
            if (len(node)==4 and all(isinstance(p,list) and len(p)==2 for p in node)
                    and all(type(v) in (int,float) for p in node for v in p)):
                a,b=node[0];c,d=node[2]
                if dumps(node)==dumps([[a,b],[c,b],[c,d],[a,d]]):
                    return {'b':[encode(v) for v in (a,b,c,d)]}
            return [encode(v) for v in node]
        if isinstance(node,str) and len(node)>64 and ':token:' in node:
            prefix,suffix=node.rsplit(':token:',1)
            if counts[dumps(prefix)]>1:return {'p':[encode(prefix),':token:'+suffix]}
        return node
    data = encode(value)
    return {'schemas':schemas, 'shared':shared, 'data':data, 'sha256':fingerprint(value)}


def unpack(archive):
    expanded = 0
    def decode(node, active=frozenset(), depth=0):
        nonlocal expanded
        expanded += 1
        if expanded > 500000 or depth > 80: raise ValueError('结构证据展开超限')
        if isinstance(node, dict):
            if set(node)=={'b'}:
                if not isinstance(node['b'],list) or len(node['b'])!=4:raise ValueError('矩形证据无效')
                a,b,c,d=[decode(v,active,depth+1) for v in node['b']]
                return [[a,b],[c,b],[c,d],[a,d]]
            if set(node)=={'p'}:
                parts=node['p']
                if not isinstance(parts,list) or len(parts)!=2:raise ValueError('来源前缀无效')
                prefix,suffix=[decode(v,active,depth+1) for v in parts]
                if not isinstance(prefix,str) or not isinstance(suffix,str):raise ValueError('来源前缀类型无效')
                return prefix+suffix
            if set(node) == {'r'}:
                index = node['r']
                if type(index) is not int or not 0 <= index < len(archive['shared']) or index in active:
                    raise ValueError('共享证据引用无效')
                return decode(archive['shared'][index], active | {index}, depth+1)
            if set(node) != {'o'}: raise ValueError('共享证据编码无效')
            values = node['o']
            if not isinstance(values,list) or not values or type(values[0]) is not int or not 0 <= values[0] < len(archive['schemas']):
                raise ValueError('共享证据字段无效')
            keys, defaults = archive['schemas'][values[0]]
            if len(values)<2 or type(values[1]) is not int or not 0<=values[1]<(1<<len(keys)):
                raise ValueError('共享证据字段掩码无效')
            mask=values[1]
            if mask.bit_count()!=len(values)-2 or len(keys)!=len(set(keys)): raise ValueError('共享证据字段重复或缺失')
            fields=iter(values[2:]);result={}
            for i,key in enumerate(keys):
                if mask & (1<<i):result[key]=decode(next(fields),active,depth+1)
                elif str(i) in defaults:result[key]=deepcopy(defaults[str(i)])
                else:raise ValueError('共享证据缺少字段')
            return result
        if isinstance(node,list): return [decode(v,active,depth+1) for v in node]
        return node
    value = decode(archive['data'])
    if fingerprint(value) != archive['sha256']: raise ValueError('共享证据哈希不一致')
    return value


def evidence(structure):
    return {k:structure[k] for k in ('version','table_index','current_table','candidates','image_sha256','image_version')}


def token_ids(structure):
    ids = list(dict.fromkeys(t for c in structure['candidates'] for t in c['token_ids']))
    for cell in structure['current_table']['cells']:
        for ident in cell.get('structure_source',{}).get('token_ids',[]):
            if ident not in ids: ids.append(ident)
    return ids


def delta(before, after, path=()):
    if dumps(before)==dumps(after):return []
    if isinstance(before,dict) and isinstance(after,dict) and list(before)==list(after):
        return [op for key in before for op in delta(before[key],after[key],path+(key,))]
    if isinstance(before,list) and isinstance(after,list) and len(before)==len(after):
        return [op for i,(a,b) in enumerate(zip(before,after)) for op in delta(a,b,path+(i,))]
    return [[list(path),deepcopy(after)]]


def restore(archive):
    saved=unpack(archive)
    original=deepcopy(saved['evidence'])
    for index,changes in saved['table_deltas']:
        if type(index) is not int or not 0<index<len(original['candidates']):raise ValueError('候选差分索引无效')
        table=deepcopy(original['candidates'][0]['table'])
        for path,value in changes:
            if not path:table=deepcopy(value);continue
            node=table
            for key in path[:-1]:node=node[key]
            node[path[-1]]=deepcopy(value)
        original['candidates'][index]['table']=table
    if fingerprint(original)!=saved['original_sha256']:raise ValueError('候选差分还原失败')
    return original


def encode(structure):
    original = evidence(structure)
    saved=deepcopy(original);deltas=[]
    for i,candidate in enumerate(saved['candidates'][1:],1):
        changes=delta(saved['candidates'][0]['table'],candidate['table'])
        if len(dumps(changes))<len(dumps(candidate['table'])):
            deltas.append([i,changes]);candidate['table']=None
    archive = pack({'evidence':saved,'table_deltas':deltas,'original_sha256':fingerprint(original)})
    if restore(archive) != original: raise ValueError('共享证据无法无损还原')
    # One namespace for all candidates. Aliases are never reused for other IDs.
    ids = token_ids(structure)
    aliases = {ident:f't{i}' for i,ident in enumerate(ids)}
    def cells(table):
        return [[c['row'],c['column'],c['row_span'],c['column_span'],c['text'],bool(c.get('is_header')),
                 c.get('header_role'),[aliases[t] for t in c.get('structure_source',{}).get('token_ids',[])]]
                for c in sorted(table['cells'],key=lambda c:(c['row'],c['column']))]
    base = cells(structure['current_table'])
    candidates = []
    for c in structure['candidates']:
        rows = cells(c['table'])
        compact = {'id':c['id'], 'rows':c['table']['rows'], 'columns':c['table']['columns'],
                   'token_ids':[aliases[t] for t in c['token_ids']]}
        if len(rows) == len(base) and all(a[:4] == b[:4] for a,b in zip(base,rows)):
            compact['base'] = 'current'
            compact['changes'] = [[i,row] for i,row in enumerate(rows) if row != base[i]]
        else: compact['cells'] = rows
        candidates.append(compact)
    strings = {v:i for i,v in enumerate(archive['shared']) if isinstance(v,str)}
    for i,value in enumerate(archive['shared']):
        if isinstance(value,dict) and set(value)=={'p'}:
            prefix,suffix=value['p']
            if isinstance(prefix,dict) and set(prefix)=={'r'}:prefix=archive['shared'][prefix['r']]
            if isinstance(prefix,str):strings[prefix+suffix]=i
    return {'version':VERSION, 'cell_fields':['row','column','row_span','column_span','text','is_header','header_role','token_ids'],
        'current':{'rows':structure['current_table']['rows'],'columns':structure['current_table']['columns'],'cells':base},
        'candidates':candidates, 'token_aliases':[{'r':strings[t]} if t in strings else t for t in ids], 'archive':archive}


def decode_response(response, structure):
    from ocr_workbench.structure_arbitration import validate_response, VERSION as INTERNAL_VERSION
    if not isinstance(response,dict) or response.get('version') != VERSION:
        raise ValueError('结构传输响应版本无效')
    wire = encode(structure)
    if fingerprint(wire) != structure.get('wire_sha256'): raise ValueError('结构传输快照已变化')
    result = deepcopy(response)
    refs = result.get('token_ids')
    if not isinstance(refs,list): raise ValueError('文字引用无效')
    mapping = {f't{i}':ident for i,ident in enumerate(token_ids(structure))}
    if any(not isinstance(ref,str) or ref not in mapping for ref in refs): raise ValueError('未知文字引用')
    result['version'] = INTERNAL_VERSION
    result['token_ids'] = [mapping[ref] for ref in refs]
    return validate_response(result,structure)
