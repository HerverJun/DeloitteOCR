"""Use verified build cache patches; retain the standalone development adapter."""
from __future__ import annotations
import ast
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys

SPECIFICATIONS={
    'paddle.dataset.common':('dataset/common.py','DATA_HOME',"os.path.join(HOME, '.cache', 'paddle', 'dataset')",'dataset'),
    'paddle.utils.download':('utils/download.py','WEIGHTS_HOME','osp.expanduser("~/.cache/paddle/hapi/weights")','hapi/weights'),
}
REVIEWED_BUILD_PATCHES={
    'paddle.dataset.common':'6ec95229e7320a6c93ce0eab45b07a63eddb00ae8b29f06a84cef4c8ef9e3500',
    'paddle.utils.download':'ee51905262572834e63096c3fc11d995c2f74e5e3138df78d78890c9a2c4e9ee',
}


def checked_f(path, *, create=False):
    from runtime_paths import require_path
    path=require_path(path, 'Paddle cache/source', role='write' if create else 'read')
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
        if not record['memory_ast_rewrite']:
            if os.environ.get('PADDLE_HOME')!=str(self.adapter.cache_root):
                raise ValueError('Managed PADDLE_HOME changed before the reviewed Paddle import')
            return compile(raw,self.path,'exec',dont_inherit=True)
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
        from packages.runtime import current_install
        config=current_install()
        self.previous_paddle_home=os.environ.get('PADDLE_HOME')
        self.managed=config is not None
        self.manifest_sha256=None
        declarations={}
        if self.managed:
            if self.previous_paddle_home:
                # Inherited user overrides cannot become a cache escape. The
                # actual selected worker cache is explicitly bound below.
                config.check(self.previous_paddle_home,'Managed PADDLE_HOME',role='write')
            manifest=checked_f(config.program/'manifests/vendor-compatibility.json')
            raw_manifest=manifest.read_bytes()
            declaration=json.loads(raw_manifest)
            if (declaration.get('format')!='clear-vendor-compatibility-v1'
                    or declaration.get('patch_set')!='paddle-managed-cache-v1'
                    or declaration.get('changes_home_or_userprofile') is not False):
                raise ValueError('Managed Paddle requires the reviewed fixed-build cache patch manifest')
            for row in declaration.get('files',[]):
                member=row.get('member')
                if member in declarations:
                    raise ValueError('Duplicate managed Paddle cache source declaration')
                declarations[member]=row
            self.manifest_sha256=hashlib.sha256(raw_manifest).hexdigest()
        self.records={}
        for fullname,(relative,variable,_,suffix) in SPECIFICATIONS.items():
            source=checked_f(package/relative)
            target=checked_f(self.cache_root/suffix,create=True)
            raw=source.read_bytes()
            source_sha=hashlib.sha256(raw).hexdigest()
            if self.managed:
                try:member=source.relative_to(config.program).as_posix()
                except ValueError:raise ValueError('Managed Paddle source is outside this offline package') from None
                row=declarations.get(member,{})
                expected=REVIEWED_BUILD_PATCHES[fullname]
                if (source_sha!=expected or row.get('patched_sha256')!=expected
                        or row.get('patched_bytes')!=len(raw) or row.get('version')!='3.3.1'
                        or row.get('resource_id')!='program_assets' or row.get('environment')!='PADDLE_HOME'):
                    raise ValueError('Managed Paddle cache source does not match the reviewed fixed-build patch: '+fullname)
                compile(raw,str(source),'exec',dont_inherit=True)
            else:
                rewrite_cache_assignment(raw,fullname,target,str(source))
            self.records[fullname]={'module':fullname,'source_path':str(source),
                'source_sha256':source_sha,'assignment':variable,
                'target':str(target),'memory_ast_rewrite':not self.managed,'imported':False}
        if self.managed:
            os.environ['PADDLE_HOME']=str(self.cache_root)

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
            'verified_build_patch':self.managed,'build_patch_manifest_sha256':self.manifest_sha256,
            'modules':[dict(self.records[name]) for name in sorted(self.records)]}

    def close(self):
        if self in sys.meta_path:sys.meta_path.remove(self)
        if self.managed:
            if self.previous_paddle_home is None:os.environ.pop('PADDLE_HOME',None)
            else:os.environ['PADDLE_HOME']=self.previous_paddle_home


def install_paddle_cache(cache_root):
    adapter=PaddleCacheAdapter(cache_root)
    sys.meta_path.insert(0,adapter)
    return adapter
