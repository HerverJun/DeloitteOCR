"""Observe real loader boundaries using CPU modules, preserving model options."""
import builtins
from pathlib import Path
import sys
from types import ModuleType,SimpleNamespace
from unittest.mock import Mock

import pytest

from ocr_workbench import glm,engine_host


def test_loading_trace_keeps_local_model_parameters_and_original_clock(monkeypatch):
    modules={}
    def module(name,**values):
        result=ModuleType(name)
        result.__dict__.update(values)
        modules[name]=result
        return result
    torch=module('torch',bfloat16='existing-bf16',cuda=SimpleNamespace(
        is_available=lambda:True,is_bf16_supported=lambda:True))
    model=Mock();model.to.return_value=model;model.eval.return_value=model
    detector=Mock();processor=object();loader=object();formatter=object()
    model_factory=Mock(return_value=model);processor_factory=Mock(return_value=processor)
    module('transformers',AutoProcessor=SimpleNamespace(from_pretrained=processor_factory),
        AutoModelForImageTextToText=SimpleNamespace(from_pretrained=model_factory))
    config=SimpleNamespace(pipeline=SimpleNamespace(maas=SimpleNamespace(enabled=True),max_workers=8,
        layout=SimpleNamespace(model_dir='old',device='gpu'),page_loader=object(),result_formatter=object()))
    module('glmocr')
    module('glmocr.config',load_config=lambda:config)
    module('glmocr.layout',PPDocLayoutDetector=Mock(return_value=detector))
    module('glmocr.dataloader',PageLoader=Mock(return_value=loader))
    module('glmocr.postprocess',ResultFormatter=Mock(return_value=formatter))
    module('glmocr.utils')
    module('glmocr.utils.image_utils',crop_image_region=Mock())
    for name,value in modules.items():monkeypatch.setitem(sys.modules,name,value)
    events=[];clocks=iter([10.,17.5])
    monkeypatch.setattr(glm.time,'perf_counter',lambda:next(clocks))
    session=glm.load_session(Path('F:/models'),stage=lambda name,detail:events.append((name,detail)))
    assert session==(detector,model,processor,loader,formatter,7.5,config)
    model_factory.assert_called_once_with('F:\\models\\GLM-OCR',dtype=torch.bfloat16,
        local_files_only=True,attn_implementation='sdpa')
    model.to.assert_called_once_with('cuda');model.eval.assert_called_once_with()
    processor_factory.assert_called_once_with('F:\\models\\GLM-OCR',local_files_only=True)
    assert config.pipeline.layout.device=='cpu' and config.pipeline.max_workers==1
    assert config.pipeline.maas.enabled is False
    assert events[0][0]=='glm_torch_import'
    names=[name for name,_ in events]
    assert names.index('glm_weights_loading')<names.index('glm_cuda_transfer')<names.index('glm_processor_loading')
    assert names[-1]=='glm_formatter_initialization'


def test_failed_torch_import_stays_in_its_observed_loading_stage(monkeypatch,tmp_path):
    original_import=builtins.__import__
    failure=RuntimeError('Controlled runtime import failure')
    def importing(name,*args,**kwargs):
        if name=='torch':raise failure
        return original_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',importing)
    progress=engine_host.LoadProgress(tmp_path,'glm',interval=.005)
    try:
        with pytest.raises(RuntimeError) as caught:
            engine_host.Resident(tmp_path,'glm',tmp_path,progress=progress)
        assert caught.value is failure
        assert progress.stage=='glm_torch_import'
    finally:progress.finish('failed',failure)
