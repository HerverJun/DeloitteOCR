"""Targeted in-memory Paddle cache overrides; never alter HOME or vendor files."""
from __future__ import annotations
import ast
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
from pathlib import Path
import sys

SPECIFICATIONS={
    'paddle.dataset.common':('dataset/common.py','DATA_HOME',"os.path.join(HOME, '.cache', 'paddle', 'dataset')",'dataset'),
    'paddle.utils.download':('utils/download.py','WEIGHTS_HOME','osp.expanduser("~/.cache/paddle/hapi/weights")','hapi/weights'),
}


def checked_f(path, *, create=False):
    path=Path(path).absolute()
    if path.drive.upper()!='F:' or path.resolve().drive.upper()!='F:':
        raise ValueError('Paddle cache/source path must resolve to local F: '+str(path))
    if any(parent.is_symlink() or parent.is_junction() for parent in (path,*path.parents)):
        raise ValueError('Paddle cache/source path cannot use a redirected directory: '+str(path))
    if create:path.mkdir(parents=True,exist_ok=True)
    return path.resolve()


def rewrite_cache_assignment(source,module,target,filename):
    """Require exactly the known top-level assignment, then replace only its RHS."""
    _,variable,expression,_=SPECIFICATIONS[module]
    tree=ast.parse(importlib.util.decode_source(source),filename=filename)
    writes=[]
    for node in ast.walk(tree):
        targets=node.targets if isinstance(node,ast.Assign) else [node.target] if isinstance(node,(ast.AnnAssign,ast.AugAssign)) else []
        if any(isinstance(value,ast.Name) and value.id==variable for value in targets):writes.append(node)
    if (len(writes)!=1 or writes[0] not in tree.body or not isinstance(writes[0],ast.Assign)
            or len(writes[0].targets)!=1
            or ast.dump(writes[0].value,include_attributes=False)!=ast.dump(ast.parse(expression,mode='eval').body,include_attributes=False)):
        raise ValueError('Unexpected Paddle cache assignment; refusing unreviewed override: '+module)
    node=writes[0]
    node.value=ast.copy_location(ast.Constant(str(target)),node.value)
    return compile(ast.fix_missing_locations(tree),filename,'exec',dont_inherit=True)


class _CacheLoader(importlib.machinery.SourceFileLoader):
    def __init__(self,fullname,path,adapter):
        super().__init__(fullname,path);self.adapter=adapter

    def get_code(self,fullname):
        record=self.adapter.records[fullname]
        raw=self.get_data(self.path)
        if hashlib.sha256(raw).hexdigest()!=record['source_sha256']:
            raise ValueError('Paddle source changed after cache-adapter validation')
        return rewrite_cache_assignment(raw,fullname,record['target'],self.path)

    def exec_module(self,module):
        super().exec_module(module)
        self.adapter.records[module.__name__]['imported']=True


class PaddleCacheAdapter(importlib.abc.MetaPathFinder):
    def __init__(self,cache_root):
        self.cache_root=checked_f(cache_root,create=True)
        if any(name in sys.modules for name in SPECIFICATIONS):
            raise ValueError('Install Paddle cache adapter before importing Paddle cache modules')
        spec=importlib.machinery.PathFinder.find_spec('paddle')
        if spec is None or not spec.origin:raise ImportError('Bundled Paddle package is unavailable')
        package=checked_f(Path(spec.origin).parent)
        self.records={}
        for fullname,(relative,variable,_,suffix) in SPECIFICATIONS.items():
            source=checked_f(package/relative)
            target=checked_f(self.cache_root/suffix,create=True)
            raw=source.read_bytes()
            rewrite_cache_assignment(raw,fullname,target,str(source))
            self.records[fullname]={'module':fullname,'source_path':str(source),
                'source_sha256':hashlib.sha256(raw).hexdigest(),'assignment':variable,
                'target':str(target),'memory_ast_rewrite':True,'imported':False}

    def find_spec(self,fullname,path=None,target=None):
        if fullname not in self.records:return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec is None or not isinstance(spec.loader,importlib.machinery.SourceFileLoader):
            raise ImportError('Paddle cache module must use the reviewed source loader: '+fullname)
        if checked_f(spec.origin)!=Path(self.records[fullname]['source_path']):
            raise ImportError('Paddle cache module resolved to an unexpected source: '+fullname)
        spec.loader=_CacheLoader(fullname,spec.origin,self)
        return spec

    def identity(self):
        return {'format':'clear-paddle-f-cache-adapter-v1','cache_root':str(self.cache_root),
            'vendor_files_modified':False,'home_environment_modified':False,
            'modules':[dict(self.records[name]) for name in sorted(self.records)]}

    def close(self):
        if self in sys.meta_path:sys.meta_path.remove(self)


def install_paddle_cache(cache_root):
    adapter=PaddleCacheAdapter(cache_root)
    sys.meta_path.insert(0,adapter)
    return adapter
