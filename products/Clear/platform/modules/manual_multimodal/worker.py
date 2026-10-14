"""One request per process. All resources are explicit local paths; no model downloads."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

# Embedded Windows Python uses ._pth isolation and does not add the script's
# directory to sys.path. Keep the small sibling modules explicit in packaged runs.
sys.path.insert(0, str(Path(__file__).resolve().parent))

def initialize_temporary_directory(output):
    import tempfile
    configured=os.environ.get('CLEAR_PADDLE_TEMP') or os.environ.get('TEMP','')
    if not configured or not str(configured).isascii():
        configured='F:/OCR/temp/manual/'+hashlib.sha256(str(output).encode('utf-8')).hexdigest()[:20]
    temporary=Path(configured).absolute()
    if temporary.drive.upper()!='F:' or temporary.resolve().drive.upper()!='F:' or not str(temporary).isascii():
        raise ValueError('Native libraries require an ASCII temporary directory on F:')
    if any(p.is_symlink() or p.is_junction() for p in (temporary,*temporary.parents)):
        raise ValueError('Temporary directory must be a real F: directory')
    temporary.mkdir(parents=True,exist_ok=True)
    for key in ('TEMP','TMP','TMPDIR','CLEAR_PADDLE_TEMP'):os.environ[key]=str(temporary)
    tempfile.tempdir=str(temporary)

def deny_network():
    """Workers consume local resources exclusively, including cold-cache runs."""
    import socket
    def blocked(*args,**kwargs):raise OSError('离线推理 worker 禁止网络访问；请使用随包资源')
    socket.getaddrinfo=blocked
    socket.socket.connect=blocked
    socket.socket.connect_ex=blocked

def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def save(name,value):
    temporary=OUT/(name+'.new')
    temporary.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    # On Windows a brief reader or virus scan may hold the destination without
    # delete sharing. Retain atomic publication and retry the bounded conflict.
    for attempt in range(20):
        try:
            os.replace(temporary,OUT/name);break
        except PermissionError:
            if attempt==19:raise
            time.sleep(.05*(attempt+1))

def progress(stage, **fields):
    save('progress.json',{'stage':stage,'worker_epoch':getattr(globals().get('CONTROL'), 'epoch', None),**fields})

def image(path):
    import cv2
    import numpy as np
    value=cv2.imdecode(np.fromfile(path,dtype=np.uint8),cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if value is None:raise ValueError('无法解码图片')
    return value

def png(name,value):
    import cv2
    ok,content=cv2.imencode('.png',value)
    if not ok:raise ValueError('图片编码失败')
    content.tofile(OUT/name)

def face(request):
    import cv2
    import numpy as np
    folder=Path(request['models'])/'face'
    # The native filename overload cannot open Unicode Windows paths.
    empty=np.empty(0,dtype=np.uint8)
    detector=cv2.FaceDetectorYN.create('onnx',np.fromfile(folder/'yunet.onnx',dtype=np.uint8),empty,(320,320),.85,.3,5000)
    recognizer=cv2.FaceRecognizerSF.create('onnx',np.fromfile(folder/'sface.onnx',dtype=np.uint8),empty)
    hashes={'yunet':digest(folder/'yunet.onnx'),'sface':digest(folder/'sface.onnx')}
    images=[]
    for index,source in enumerate(request['inputs']):
        progress(f'检测第 {index+1} 张图片')
        frame=image(source['path']);height,width=frame.shape[:2]
        if width*height>40_000_000:raise ValueError('图片超过首版 4000 万像素上限')
        detector.setInputSize((width,height))
        _,detected=detector.detect(frame);faces=[]
        preview=frame.copy()
        for number,row in enumerate([] if detected is None else detected[:100]):
            x,y,w,h=row[:4];x1=max(0,int(x));y1=max(0,int(y));x2=min(width,int(x+w));y2=min(height,int(y+h))
            if x2<=x1 or y2<=y1:continue
            aligned=recognizer.alignCrop(frame,row)
            feature=recognizer.feature(aligned).flatten();feature=feature/max(float(np.linalg.norm(feature)),1e-12)
            sharpness=float(cv2.Laplacian(cv2.cvtColor(aligned,cv2.COLOR_BGR2GRAY),cv2.CV_64F).var())
            usable=min(w,h)>=40 and sharpness>=20
            faces.append({'face_index':number,'box':[x1,y1,x2,y2],'detection_score':float(row[-1]),
                          'quality':{'usable':bool(usable),'sharpness':sharpness,'min_pixels':float(min(w,h)),
                          'note':'' if usable else '人脸过小或模糊，当前结果仅供查看'},'feature':feature.tolist(),
                          'crop':f'face-{index}-{number}.png'})
            png(f'face-{index}-{number}.png',aligned)
            cv2.rectangle(preview,(x1,y1),(x2,y2),(20,160,70),2)
            cv2.putText(preview,str(number+1),(x1,y1+20),cv2.FONT_HERSHEY_SIMPLEX,.7,(20,160,70),2)
        png(f'page-{index+1}.png',preview)
        if index==0:png('preview.png',preview)
        images.append({'width':width,'height':height,'faces':faces,'asset_id':source['asset_id'],
                       'preview':f'page-{index+1}.png','transform':{'orientation':'stored_pixels','scale':[1,1],'rotation_degrees':0,'offset':[0,0]}})
    result={'images':images,'model_hashes':hashes,'algorithm':'YuNet-2023mar + SFace-2021dec',
            'opencv':cv2.__version__,'device':'cpu','outcome':'detected' if any(x['faces'] for x in images) else 'no_face',
            'threshold_version':'sface-reference-uncalibrated','threshold':request['options']['threshold'],
            'limitations':['余弦相似度不是身份概率；参考阈值尚需业务样本校准','静态照片不提供活体或反换脸结论']}
    indices=request['options'].get('face_indices',[])
    def selected(index):
        number=indices[index] if len(indices)>index else 0
        values=images[index]['faces']
        return next((value for value in values if value['face_index']==number),None)
    action=request['options']['action']
    if action=='compare':
        left,right=selected(0),selected(1)
        if left is None or right is None:result.update(outcome='no_face',comparison=None)
        elif not left['quality']['usable'] or not right['quality']['usable']:result.update(outcome='quality_insufficient',comparison=None)
        else:
            similarity=float(np.dot(left['feature'],right['feature']))
            result.update(outcome='compared',comparison={'cosine_similarity':similarity,
                'meets_reference_threshold':similarity>=request['options']['threshold'],'face_indices':[left['face_index'],right['face_index']]})
    elif action=='search':
        query=selected(0);candidates=[]
        if query is None:result['outcome']='no_face'
        elif not query['quality']['usable']:result['outcome']='quality_insufficient'
        else:
            for reference in request['references']:
                if reference['model_hash']!=hashes['sface']:continue
                score=float(np.dot(query['feature'],reference['feature']))
                candidates.append({'reference_id':reference['reference_id'],'label':reference['label'],'score':score,
                    'meets_reference_threshold':score>=request['options']['threshold'],'source':reference['source']})
            candidates.sort(key=lambda value:value['score'],reverse=True)
            result['outcome']='candidates' if candidates and candidates[0]['meets_reference_threshold'] else 'no_trusted_candidate'
        result['candidates']=candidates[:request['options']['top_k']]
    return result

def audio(request):
    from audio_processing import run_audio
    return run_audio(request, OUT, CONTROL, progress)

def seal(request):
    sys.path.insert(0,str(Path(request['program'])/'bundles/ocr/src'))
    from ocr_workbench.windows_paths import ascii_model_directory
    with ascii_model_directory(Path(request['models'])/'seal') as root:
        return seal_at_root(request,root)

def seal_at_root(request,root):
    progress('加载印章定位、曲线文字检测与识别模型')
    from paddleocr import SealRecognition
    import paddle
    from paddle import inference
    import re
    device='cpu' if request['options']['device']=='cpu' else 'gpu:0'
    paddle.set_device(device)
    predictors=[]
    original_create=inference.create_predictor
    def verified_predictor(config):
        actual={'use_gpu':config.use_gpu(),'mkldnn':config.mkldnn_enabled(),
                'cpu_threads':config.cpu_math_library_num_threads()}
        if actual['use_gpu']!=(device!='cpu'):raise RuntimeError('印章推理设备与请求不一致')
        predictors.append(actual)
        return original_create(config)
    inference.create_predictor=verified_predictor
    try:
        # The bundled Paddle PIR/oneDNN path rejects DoubleAttribute arrays.
        # Use the validated native CPU kernels for all three predictors.
        pipeline=SealRecognition(layout_detection_model_name='PP-DocLayout-L',layout_detection_model_dir=str(root/'PP-DocLayout-L'),
            seal_text_detection_model_name='PP-OCRv4_server_seal_det',seal_text_detection_model_dir=str(root/'PP-OCRv4_server_seal_det'),
            text_recognition_model_name='PP-OCRv4_server_rec',text_recognition_model_dir=str(root/'PP-OCRv4_server_rec'),
            use_doc_orientation_classify=False,use_doc_unwarping=False,use_layout_detection=True,
            device=device,enable_mkldnn=False,cpu_threads=8)
    finally:
        inference.create_predictor=original_create
    if len(predictors)!=3:raise RuntimeError('印章流水线没有完成三项模型设备验证')
    progress('多页与多章识别')
    seals=[];pages=[];page_images=[]
    for page in pipeline.predict(request['inputs'][0]['path']):
        data=page.json.get('res',page.json)
        page_number=(data.get('page_index') or 0)+1
        frame=page['doc_preprocessor_res']['output_img']
        height,width=frame.shape[:2]
        preview_name=f'page-{page_number}.png';png(preview_name,frame)
        page_images.append({'page_number':page_number,'width':width,'height':height,'preview':preview_name,
            'transform':{'orientation':'stored_pixels','rotation_degrees':0,'offset':[0,0],
                'coordinate_space':'rendered_page_pixels','normalized_to_pixels':[width,height]}})
        boxes=[x for x in data.get('layout_det_res',{}).get('boxes',[]) if x.get('label','').lower()=='seal']
        for index,row in enumerate(data.get('seal_res_list',[])):
            text=' '.join(row.get('rec_texts',[]));numbers=re.findall(r'\d{8,}',text)
            subject=re.sub(r'\d+|[\s·•]','',text)
            expected=request['options'].get('expected_subject','')
            box=boxes[index].get('coordinate') if index<len(boxes) else None
            if box:box=[max(0,float(box[0])),max(0,float(box[1])),min(width,float(box[2])),min(height,float(box[3]))]
            seals.append({'page_number':page_number,'seal_index':index,'box':box,
                'text':text,'subject':subject,'number':numbers[0] if numbers else '', 'scores':row.get('rec_scores',[]),
                'expected_subject':expected,'subject_check':'not_requested' if not expected else 'contains' if expected in subject else 'needs_review',
                'outcome':'recognized' if text else 'unreadable'})
        pages.append(data)
    (OUT/'raw-paddle-result.json').write_text(json.dumps(pages,ensure_ascii=False),encoding='utf-8')
    if Path(request['inputs'][0]['path']).suffix.lower()!='.pdf':png('preview.png',image(request['inputs'][0]['path']))
    hashes={path.relative_to(root).as_posix():digest(path) for path in root.rglob('*') if path.is_file() and path.suffix in {'.json','.pdiparams','.yml','.pdmodel'}}
    return {'algorithm':'PaddleOCR SealRecognition','device':request['options']['device'],'paddle':paddle.__version__,
        'model_hashes':hashes,'seals':seals,'pages':page_images,'outcome':'recognized' if seals else 'no_seal',
        'execution':{'paddle_device':paddle.get_device(),'predictors':predictors},
        'limitations':['识别文字及主体核对不等于印章真伪鉴定','章下遮挡内容可能无法辨认','主体提取为文字规则结果，需要人工校正']}

def catnet(request):
    import cv2
    import numpy as np
    import torch
    import torch.nn.functional as functional
    import jpeglib
    from PIL import Image
    from yacs.config import CfgNode
    root=Path(request['models'])/'catnet'
    sys.path.insert(0,str(root/'source'))
    from lib.models.network_CAT import CAT_Net
    import yaml
    config=CfgNode(yaml.safe_load((root/'source/experiments/CAT_full.yaml').read_text('utf-8')))
    config.MODEL.PRETRAINED_RGB='';config.MODEL.PRETRAINED_DCT=''
    progress('加载 CAT-Net v2 权重')
    model=CAT_Net(config)
    checkpoint=torch.load(root/'CAT_full_v2.pth.tar',map_location='cpu',weights_only=False)
    state=checkpoint['state_dict']
    model.load_state_dict(state,strict=True)
    device=request['options']['device'];model=model.eval().to(device)
    source=request['inputs'][0]['path']
    import shutil,tempfile
    # Preserve the original JPEG bytes while crossing libjpeg's ASCII API.
    with tempfile.TemporaryDirectory(prefix='clear-catnet-') as directory:
        compatible=Path(directory)/'input.jpg';shutil.copyfile(source,compatible)
        if digest(compatible)!=digest(source):raise ValueError('JPEG temporary copy changed')
        jpeg=jpeglib.read_dct(str(compatible))
        y=jpeg.Y
        qindex=int(jpeg.quant_tbl_no[0])
        quantization=jpeg.qt[qindex].copy()
    rgb=np.asarray(Image.open(source).convert('RGB'))
    height,width=rgb.shape[:2]
    if height*width>16_000_000:raise ValueError('首版深度取证支持最多 1600 万像素 JPEG；原件未修改')
    # jpeglib exposes the original quantized Y coefficients, not a DCT of
    # decoded/recompressed pixels. This matches CAT-Net's one-channel stream.
    coefficients=y.transpose(0,2,1,3).reshape(y.shape[0]*8,y.shape[1]*8)
    qtable=torch.tensor(quantization,dtype=torch.float32)[None,None].to(device)
    hp,wp=coefficients.shape
    padded=np.full((hp,wp,3),127.5,dtype=np.float32);padded[:height,:width]=rgb
    probabilities=np.zeros((height,width),dtype=np.float32)
    tile_size=int((request.get('runner') or {}).get('catnet_tile_size',1024))
    if tile_size not in (512,768,1024):raise ValueError('CAT-Net tile size must be 512, 768 or 1024')
    total=((height+tile_size-1)//tile_size)*((width+tile_size-1)//tile_size);done=0
    with torch.inference_mode():
        for top in range(0,height,tile_size):
            for left in range(0,width,tile_size):
                CONTROL.check()
                bottom=min(top+tile_size,hp);right=min(left+tile_size,wp)
                pixels=padded[top:bottom,left:right]
                block=np.abs(coefficients[top:bottom,left:right])
                volume=np.stack([block==i for i in range(20)]+[block>=20]).astype(np.float32)
                tensor=torch.cat([torch.from_numpy(pixels.transpose(2,0,1)).float().sub(127.5).div(127.5),torch.from_numpy(volume)])[None].to(device)
                done+=1;progress(f'JPEG DCT 区域分析 {done}/{total}')
                logits=model(tensor,qtable)
                prediction=functional.interpolate(logits,size=pixels.shape[:2],mode='bilinear',align_corners=True).softmax(dim=1)[0,1].cpu().numpy()
                bh=min(bottom,height)-top;bw=min(right,width)-left
                probabilities[top:top+bh,left:left+bw]=prediction[:bh,:bw]
    np.save(OUT/'heatmap-probabilities.npy',probabilities,allow_pickle=False)
    colored=cv2.applyColorMap((np.clip(probabilities,0,1)*255).astype('uint8'),cv2.COLORMAP_JET)
    png('heatmap.png',colored);png('preview.png',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
    return {'algorithm':'CAT-Net v2','device':device,'torch':torch.__version__,'outcome':'analyzed','width':width,'height':height,
        'model_hashes':{'CAT_full_v2':digest(root/'CAT_full_v2.pth.tar'),'network':digest(root/'source/lib/models/network_CAT.py')},
        'metrics':{'map_min':float(probabilities.min()),'map_max':float(probabilities.max()),'map_mean':float(probabilities.mean())},
        'processing':{'jpeg_recompressed':False,'orientation':'stored_pixels','tile_size':tile_size,'tile_grid':[height,width]},
        'limitations':['热图是模型的区域响应，不是整图真实性概率','大图按1024像素网格推理，块边界可能影响定位','训练域与手机屏拍、重存和文档改字不同，需要分组评测']}

if __name__=='__main__':
    deny_network()
    input_path=Path(sys.argv[1]).resolve();OUT=input_path.parent
    if input_path.drive.upper()!='F:' or OUT.resolve().drive.upper()!='F:':raise ValueError('worker output must be on F:')
    initialize_temporary_directory(OUT)
    request=json.loads(input_path.read_text('utf-8'))
    import faulthandler
    faulthandler.enable()
    from worker_protocol import WorkerControl, WorkerStopped
    CONTROL=WorkerControl(request, save)
    CONTROL.event('ready')
    try:
        CONTROL.wait('start')
        progress('门控已通过')
        CONTROL.check()
    except WorkerStopped:
        CONTROL.event('yielded')
        sys.exit(75)
    started=time.monotonic()
    try:
        result={'face':face,'audio':audio,'seal':seal,'catnet':catnet}[request['kind']](request)
        CONTROL.check()
    except WorkerStopped:
        CONTROL.event('yielded')
        sys.exit(75)
    result['elapsed_seconds']=round(time.monotonic()-started,3)
    result['input_hashes']=[source['blob_hash'] for source in request['inputs']]
    import platform
    result['environment']={'python':platform.python_version(),'system':platform.platform(),'machine':platform.machine(),
        'adapter_sha256':digest(__file__),'runtime_executable_sha256':digest(sys.executable)}
    result['parameters']=request['options']
    result['network_policy']='all_outbound_connections_denied'
    save('result.json',result);progress('completed')
