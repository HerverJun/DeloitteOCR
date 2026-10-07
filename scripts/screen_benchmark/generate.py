"""Deterministic screen-photo regression data. Truth is separate from model inputs."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path
import random
import statistics
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

SEED = 20261007
ROOT = Path(__file__).resolve().parents[2]
FAMILIES = ['detail', 'wide', 'dense', 'multi_header', 'row_group', 'income_negative',
            'identifiers', 'percent_wan', 'blank_repeat', 'multiple_tables', 'outside_notes', 'wrapped_text']
DEVELOPMENT_FAMILIES = FAMILIES[:8]
TEST_FAMILIES = FAMILIES[8:]

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if path.exists():
        if path.read_text('utf-8') != data:
            raise ValueError(f'Frozen file differs; choose a new dataset root: {path}')
        return
    path.write_text(data, 'utf-8')

def save_image(path, image, **options):
    path=Path(path); buffer=io.BytesIO(); image.save(buffer,format='PNG' if path.suffix.lower()=='.png' else 'JPEG',**options)
    payload=buffer.getvalue()
    if path.exists():
        if path.read_bytes()!=payload:raise ValueError(f'Frozen image differs: {path}')
    else:path.write_bytes(payload)

def require_f(path):
    result = Path(path).resolve()
    if result.drive.upper() != 'F:':
        raise ValueError(f'Project writes must remain on F: {result}')
    for ancestor in [result, *result.parents]:
        if ancestor.exists() and ancestor.is_symlink():
            if ancestor.resolve().drive.upper() != 'F:':
                raise ValueError('Off-drive link')
    return result

def font(size, bold=False):
    return ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc', size)

def table(headers, rows, caption, widths=None, merges=(), size=21, notes=()):
    """Render literal cells including empty cells and explicit merged spans."""
    widths = widths or [160] * len(headers)
    row_h = 55 if size >= 20 else 38
    values = [headers, *rows]
    merge_map = {(r,c):(rs,cs,text) for r,c,rs,cs,text in merges}
    covered = {(r+i,c+j) for r,c,rs,cs,_ in merges for i in range(rs) for j in range(cs) if i or j}
    top = 62
    width = sum(widths) + 48
    height = top + row_h * len(values) + 24 + len(notes)*32
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((24, 14), caption, font=font(25, True), fill='#222222')
    xlist = [24]
    for w in widths: xlist.append(xlist[-1]+w)
    cells = []
    for r, row in enumerate(values):
        for c in range(len(widths)):
            if (r,c) in covered: continue
            rs,cs,text = merge_map.get((r,c), (1,1,str(row[c])))
            box = [xlist[c], top+r*row_h, xlist[c+cs], top+(r+rs)*row_h]
            draw.rectangle(box, fill='#dce8e0' if r == 0 or (r,c) in merge_map else 'white', outline='#75857c', width=1)
            # Long-content family uses real line breaks, not unlabelled clipped text.
            lines = str(text).split('\n')
            line_size = size if len(lines)==1 else 17
            for i,line in enumerate(lines):
                draw.text((box[0]+8, box[1]+6+i*(line_size+2)), line, font=font(line_size, r == 0), fill='#161d19')
            cells.append({'row':r,'column':c,'row_span':rs,'column_span':cs,'text':str(text),
                          'box':box,'tags':['critical'] if any(ch.isdigit() for ch in str(text)) else []})
    context = [{'text':caption, 'box':[24,10,width-20,48], 'kind':'caption'}]
    for i,note in enumerate(notes):
        y = top+row_h*len(values)+12+i*32
        draw.text((24,y), note, font=font(18), fill='#20251f')
        context.append({'text':note,'box':[24,y,width-20,y+28],'kind':'note'})
    return image, {'rows':len(values),'columns':len(widths),'cells':cells,'caption':caption}, context

def chinese(family, index, split):
    semantics = ROOT/'build/ocr-agent-20260920-p0/fixtures-final'/('holdout' if split=='test' else 'development')/'FX03.json'
    cases = json.loads(semantics.read_text('utf-8'))['cases']
    case = cases[index % len(cases)]
    amounts = case['terms']; total=case['displayed_total']; unit=case['unit']
    key=f'{index+1:06d}'; long=f'20261007{index+1:016d}'
    rows=[[f'业务{index+1}-{j+1}', amounts[j%len(amounts)], key] for j in range(4)]
    unit_label=f' 单位：{unit}' if unit not in {'unknown','missing','',None} else ''
    caption=f'资金明细 {index+1:02d}{unit_label}'
    headers=['项目','金额','业务编号']; widths=[240,180,240]; merges=(); size=21; notes=()
    if family=='wide':
        headers=['部门','日期','业务编号','摘要','收入','支出','余额','审核人']
        widths=[130,180,210,220,140,140,150,130]
        rows=[[f'部门{j+1}','2026-10-07',key,f'业务支付{j+1}',amounts[0],amounts[-1],total,'王敏'] for j in range(7)]
    elif family=='dense':
        headers=['序号','科目','金额','预算','执行率','批次']; widths=[75,155,130,130,115,150];size=16
        rows=[[str(j+1),f'费用科目{j%5}',amounts[j%len(amounts)],'1,234.56',f'{20+j}%',key] for j in range(24)]
    elif family=='multi_header':
        headers=['部门','本期收支','','年初预算'];widths=[170,180,180,180]
        rows=[['','收入','支出',''],['财务部',amounts[0],amounts[-1],total],['技术部','0.00','-20.00','1,234.56'],['合计',total,'(20.00)','1,500.00']]
        merges=[(0,0,2,1,'部门'),(0,1,1,2,'本期收支'),(0,3,2,1,'年初预算')]
    elif family=='row_group':
        headers=['分类','子项','金额','说明'];widths=[150,190,180,210]
        rows=[['行政','办公用品',amounts[0],'按月结算'],['','差旅费用',amounts[-1],'凭单支付'],['','合计',total,'实际值'],['项目','设备采购','1,234.56','验收入库'],['','服务支出','(20.00)','冲减'],['','小计','1,214.56','无需改原值']]
        merges=[(1,0,3,1,'行政'),(4,0,3,1,'项目')]
    elif family=='income_negative':
        headers=['日期','收支摘要','收入','支出','结余'];widths=[180,240,180,180,180]
        rows=[['2026-10-01','收到拨款','1,200.00','','1,200.00'],['2026-10-02','退款冲回','','-20.00','1,220.00'],['2026-10-03','退回调整','(20.00)','','1,200.00'],['2026-10-07','期末合计',total,'0.00',total]]
    elif family=='identifiers':
        headers=['业务编号','合同流水号','金额','户名'];widths=[210,460,160,180]
        rows=[[f'{index+j+1:08d}',f'20261007{index*20+j:016d}',amounts[j%len(amounts)],f'单位{j+1}'] for j in range(8)]
    elif family=='percent_wan':
        caption=f'资金执行表 {index+1:02d} 单位：万元'
        headers=['项目','预算（万元）','实际（万元）','执行比例']; widths=[230,230,230,220]
        rows=[['培训费','1.20','0.24','20%'],['会议费','2.30','0.69','30%'],['管理费','0.33','0.33','100%'],['合计','3.86','1.26','32.64%']]
    elif family=='blank_repeat':
        headers=['项目','数额','备注','凭证号'];widths=[240,160,240,230]
        amount=f'{50+index}.00';duplicate_key=f'{index*2+1:06d}';next_key=f'{index*2+2:06d}'
        rows=[['重复支付',amount,'',duplicate_key],['重复支付',amount,'',duplicate_key],['未填写','','待补',next_key],['真正空白','','',''],['合计',f'{(50+index)*2}.00','保留重复','']]
    elif family=='outside_notes':
        headers=['序号','事项','原值','核对值'];widths=[100,300,190,190]
        rows=[['1','本期发生',amounts[0],amounts[0]],['2','上期调整',amounts[-1],amounts[-1]],['3','账面合计',total,total]]
        caption=f'往来核对清单 {index+1:02d}'
        notes=([f'金额单位：{unit}'] if unit_label else [])+['说明：括号表示负数；账面合计按屏幕原值保留。']
    elif family=='wrapped_text':
        headers=['业务事项','说明','编号','金额'];widths=[250,380,210,160]
        rows=[['设备采购','第一批验收入库\n附合同及付款凭证',key,amounts[0]],['服务结算','按月核对原始记录\n金额不作自动修正',key,amounts[-1]],['合计','保留全部可见内容\n长内容原文换行',key,total]]
    elif family=='multiple_tables':
        one,t1,ctx1=table(['部门','收入','编号'],[['行政',amounts[0],key],['技术',amounts[-1],key]],f'收入台账 {index+1:02d}{unit_label}',[220,200,240])
        two,t2,ctx2=table(['事项','支出','凭证'],[['差旅','-20.00','000001'],['采购','1,234.56','000002']],f'支出台账 {index+1:02d} 单位：元',[220,200,240])
        gap=55; image=Image.new('RGB',(max(one.width,two.width),one.height+two.height+gap),'white');image.paste(one,(0,0));image.paste(two,(0,one.height+gap))
        for cell in t2['cells']:cell['box']=[cell['box'][0],cell['box'][1]+one.height+gap,cell['box'][2],cell['box'][3]+one.height+gap]
        for item in ctx2:item['box']=[item['box'][0],item['box'][1]+one.height+gap,item['box'][2],item['box'][3]+one.height+gap]
        return image,[t1,t2],ctx1+ctx2,semantics,case['label']
    else:
        rows.append(['合计',total,''])
    image,t,context=table(headers,rows,caption,widths,merges,size,notes)
    return image,[t],context,semantics,case['label']

def homography(source, target):
    a=[];b=[]
    for (x,y),(u,v) in zip(source,target):
        a.extend([[x,y,1,0,0,0,-u*x,-u*y],[0,0,0,x,y,1,-v*x,-v*y]]);b.extend([u,v])
    return np.append(np.linalg.solve(np.array(a,float),np.array(b,float)),1).reshape(3,3)

def points(matrix, polygon):
    out=[]
    for x,y in polygon:
        q=np.asarray(matrix)@np.array([x,y,1.]);out.append([round(float(q[0]/q[2]),4),round(float(q[1]/q[2]),4)])
    return out

def corners(box):
    x,y,x1,y1=box;return [[x,y],[x1,y],[x1,y1],[x,y1]]

def render_screen(image, truth_tables, context, level, rng, challenge=None):
    typical=statistics.median([max(8,(c['box'][3]-c['box'][1])*.43) for t in truth_tables for c in t['cells']]) if truth_tables else 20
    desired={'clean':24,'normal':22,'hard':15}.get(level,15)
    if challenge=='tiny':desired=8
    scale=max(.35,min(2.5,desired/typical))
    tw,th=round(image.width*scale),round(image.height*scale)
    screen_w=max(1420,tw+300);screen_h=max(1020,th+320)
    screen=Image.new('RGB',(screen_w,screen_h),'#eef1f3');draw=ImageDraw.Draw(screen)
    draw.rectangle([0,0,screen_w,36],fill='#1e7145');draw.text((18,5),'财务台账.xlsx - Excel',font=font(19),fill='white')
    draw.rectangle([0,36,screen_w,78],fill='#fafafa');draw.text((25,47),'文件   开始   插入   页面布局   公式   数据   审阅   视图',font=font(18),fill='#242424')
    draw.rectangle([0,79,screen_w,156],fill='#f7f7f7');draw.text((30,100),'粘贴    剪切    复制      微软雅黑    11     B  I  U      Σ 自动求和',font=font(18),fill='#353535')
    draw.text((30,131),'常规   合并后居中   ¥  0.00  %     排序和筛选   100.00',font=font(15),fill='#444444')
    draw.rectangle([16,170,108,202],fill='white',outline='#aaaaaa');draw.text((26,174),'C12',font=font(17),fill='#333333')
    draw.rectangle([129,170,screen_w-25,202],fill='white',outline='#aaaaaa');draw.text((145,175),'fx   =SUM(C2:C11)   300.00  000001',font=font(17),fill='#292929')
    x,y=74,244
    draw.rectangle([0,217,screen_w,screen_h-66],fill='white')
    draw.rectangle([0,217,48,screen_h-66],fill='#efefef');draw.rectangle([48,217,screen_w,239],fill='#efefef')
    for j in range(20):draw.text((75+j*75,220),chr(65+j),font=font(13),fill='#505050')
    for j in range(max(1,(screen_h-300)//35)):draw.text((13,247+j*35),str(j+1),font=font(13),fill='#555555')
    if challenge!='no_table':screen.paste(image.resize((tw,th),Image.Resampling.LANCZOS),(x,y))
    else:draw.text((240,350),'没有可识别的业务表格。请打开文件。',font=font(26),fill='#555555')
    draw.rectangle([screen_w-22,239,screen_w-4,screen_h-66],fill='#eeeeee');draw.rectangle([screen_w-20,270,screen_w-6,410],fill='#c3c3c3')
    draw.rectangle([0,screen_h-66,screen_w,screen_h-38],fill='#eeeeee');draw.text((70,screen_h-62),'＋    资金明细    数据源       状态：就绪   合计 300.00',font=font(16),fill='#414141')
    draw.rectangle([0,screen_h-38,screen_w,screen_h],fill='#20252b');draw.text((20,screen_h-31),'开始   搜索     Excel        100.00         10:07  2026/10/07',font=font(16),fill='white')
    ui=[{'kind':'ribbon_formula','polygon':corners([0,0,screen_w,217]),'text':'Excel 文件 开始 SUM 300.00 000001'},
        {'kind':'rulers','polygon':corners([0,217,48,screen_h-66]),'text':'1 2 3 4'},
        {'kind':'sheet_taskbar','polygon':corners([0,screen_h-66,screen_w,screen_h]),'text':'合计 300.00 100.00'}]
    occlusion=Image.new('L',screen.size,0)
    if challenge=='occlusion':
        box=[x+tw*.24,y+th*.24,x+tw*.82,y+th*.65]
        ImageDraw.Draw(occlusion).rectangle(box,fill=255)
        draw.rectangle(box,fill='#f7f7f7',outline='#747474',width=2)
        draw.text((box[0]+12,box[1]+12),'保存更改？',font=font(25),fill='#202020')
        draw.text((box[0]+12,box[1]+60),'合计 300.00    确定   取消',font=font(18),fill='#202020')
        ui.append({'kind':'occluding_dialog','polygon':corners(box),'text':'保存更改 合计 300.00'})
    # Display effects apply to both the table and all surrounding UI before the camera transform.
    parameters={'screen_size':[screen_w,screen_h],'table_scale':scale,'estimated_character_pixels':desired,'effect_order':['compose_ui','screen_sampling','brightness_moire','whole_screen_perspective','camera_blur_jpeg']}
    if level!='clean':
        sampling=.97 if level=='normal' else .89
        screen=screen.resize((round(screen_w*sampling),round(screen_h*sampling)),Image.Resampling.BICUBIC).resize((screen_w,screen_h),Image.Resampling.BICUBIC)
        arr=np.asarray(screen,dtype=np.float32); yy,xx=np.indices(arr.shape[:2]); amp=1.5 if level=='normal' else 4.0
        frequency=rng.uniform(.24,.48);angle=rng.uniform(-.9,.9)
        moire=np.sin((xx*np.cos(angle)+yy*np.sin(angle))*frequency)*amp
        gradient=.99-(yy/max(1,screen_h-1))*(.04 if level=='normal' else .1)
        arr=arr*gradient[:,:,None]+moire[:,:,None]*np.array([.7,1.,.6])
        if level=='hard':arr+=np.exp(-((xx-screen_w*.8)**2/(screen_w*.25)**2+(yy-screen_h*.15)**2/(screen_h*.5)**2))[:,:,None]*9
        screen=Image.fromarray(np.clip(arr,0,255).astype('uint8'))
        parameters.update(screen_sampling_ratio=sampling,moire_amplitude=amp,moire_frequency=frequency,moire_angle=angle,brightness_gradient=float(gradient[-1,0]))
    camera_w,camera_h=screen_w+100,screen_h+100
    src=[[0,0],[screen_w,0],[screen_w,screen_h],[0,screen_h]]
    if level=='clean':dest=[[50,50],[screen_w+50,50],[screen_w+50,screen_h+50],[50,screen_h+50]]
    else:
        d=.018 if level=='normal' else .055
        dest=[[50+rng.uniform(0,d)*screen_w,50+rng.uniform(0,d)*screen_h],
              [camera_w-50-rng.uniform(0,d)*screen_w,50+rng.uniform(0,d)*screen_h],
              [camera_w-50-rng.uniform(0,d)*screen_w,camera_h-50-rng.uniform(0,d)*screen_h],
              [50+rng.uniform(0,d)*screen_w,camera_h-50-rng.uniform(0,d)*screen_h]]
    h=homography(src,dest); inv=np.linalg.inv(h); inv=inv/inv[2,2]
    final=screen.transform((camera_w,camera_h),Image.Transform.PERSPECTIVE,tuple(inv.flat)[:8],Image.Resampling.BICUBIC,fillcolor='#2b2b30')
    occ=occlusion.transform((camera_w,camera_h),Image.Transform.PERSPECTIVE,tuple(inv.flat)[:8],Image.Resampling.NEAREST,fillcolor=0)
    visibility=Image.new('L',final.size,0); vd=ImageDraw.Draw(visibility);vd.polygon([tuple(p) for p in dest],fill=255)
    if level!='clean':
        blur=.15 if level=='normal' else .45
        final=final.filter(ImageFilter.GaussianBlur(blur));buf=io.BytesIO();quality=92 if level=='normal' else 72
        final.save(buf,format='JPEG',quality=quality,subsampling=0);buf.seek(0);final=Image.open(buf).convert('RGB')
        parameters.update(blur_radius=blur,jpeg_quality=quality)
    clipping=[0,0,0,0]
    if challenge=='clipping':
        clipping=[round((x+tw*.2)+50),0,0,0]; final=final.crop((clipping[0],0,camera_w,camera_h));occ=occ.crop((clipping[0],0,camera_w,camera_h));visibility=visibility.crop((clipping[0],0,camera_w,camera_h))
        h=np.array([[1,0,-clipping[0]],[0,1,0],[0,0,1.]])@h
    source_to_screen=np.array([[scale,0,x],[0,scale,y],[0,0,1.]])
    combined=h@source_to_screen
    output=[]
    for t in truth_tables if challenge!='no_table' else []:
        mapped={k:v for k,v in t.items() if k!='cells'};mapped['cells']=[]
        for cell in t['cells']:
            polygon=points(combined,corners(cell['box']));minx=min(p[0] for p in polygon);maxx=max(p[0] for p in polygon)
            status='visible'
            if minx<0 or maxx>final.width:status='clipped'
            if challenge=='tiny':status='readability_uncertain'
            # Conservative challenge occlusion: any target bbox intersection gets flagged.
            if challenge=='occlusion':
                b=cell['box']; local=[x+b[0]*scale,y+b[1]*scale,x+b[2]*scale,y+b[3]*scale]
                if local[0]<box[2] and local[2]>box[0] and local[1]<box[3] and local[3]>box[1]:status='occluded'
            mapped['cells'].append({**cell,'polygon':polygon,'visibility':status})
        output.append(mapped)
    mapped_context=[{**c,'polygon':points(combined,corners(c['box']))} for c in context] if challenge!='no_table' else []
    for region in ui:region['polygon']=points(h,region['polygon'])
    parameters['clipping']=clipping
    return final,output,mapped_context,ui,occ,visibility,parameters,combined.tolist(),h.tolist()

def public_sources(split):
    base=ROOT/'build'/('tableformer-next-20260915' if split=='test' else 'table-matching-v2')/'dataset'
    manifest=base/'inputs'/f'{split}.json';annotation=base/('sealed' if split=='test' else 'annotations')/f'{split}.annotations.json'
    data=json.loads(manifest.read_text('utf-8'));labels={s['id']:s for s in json.loads(annotation.read_text('utf-8'))['samples']}
    sources=[]
    for item in data['samples']:
        path=(manifest.parent/item['image']).resolve();ann=labels[item['id']]
        if digest(path)!=item['sha256'] or ann['sha256']!=item['sha256']:raise ValueError('Source hash mismatch')
        cells=ann['targets']; rows=max(c['row']+c['row_span'] for c in cells);columns=max(c['column']+c['column_span'] for c in cells)
        sources.append({'id':item['id'],'group_id':item['group_id'],'cohort':'public','split':split,'path':path,
                        'source_manifest':str(manifest),'source_manifest_sha256':digest(manifest),'annotation_sha256':digest(annotation),
                        'tables':[{'rows':rows,'columns':columns,'cells':cells,'caption':''}],'context':[]})
    return sources

def generate(output, only_pilot=False):
    output=require_f(output);output.mkdir(parents=True,exist_ok=True)
    sources=public_sources('development'); test=public_sources('test'); validation=public_sources('validation')
    chinese_sources=[]
    for split,families,count in [('development',DEVELOPMENT_FAMILIES,2),('test',TEST_FAMILIES,15)]:
        for family in families:
            for index in range(count):
                im,tables,context,semantic,semantic_case=chinese(family,index,split)
                sid=f'zh-{split}-{family}-{index:02d}';path=output/'sources'/f'{sid}.png';path.parent.mkdir(exist_ok=True)
                save_image(path,im)
                chinese_sources.append({'id':sid,'group_id':f'zh-template-{family}','cohort':'chinese','split':split,'family':family,'path':path,'tables':tables,'context':context,'semantic_source':str(semantic),'semantic_sha256':digest(semantic),'semantic_case':semantic_case})
    public_groups={s['group_id'] for s in sources};test_groups={s['group_id'] for s in test};validation_groups={s['group_id'] for s in validation}
    if public_groups & test_groups or public_groups & validation_groups or test_groups & validation_groups:raise ValueError('Public document group leakage')
    dev_zh=[s for s in chinese_sources if s['split']=='development']
    pilot_public=[];seen=set()
    for s in sources:
        if s['group_id'] not in seen:pilot_public.append(s);seen.add(s['group_id'])
        if len(pilot_public)==12:break
    pilot_zh=[next(s for s in dev_zh if s['family']==f) for f in DEVELOPMENT_FAMILIES]
    pilot_zh += [next(s for s in dev_zh if s['family']==f and s['id'].endswith('01')) for f in DEVELOPMENT_FAMILIES[:4]]
    plans={'pilot':[(s,l,None) for s in pilot_public+pilot_zh for l in ['clean','normal','hard']]}
    if not only_pilot:
        plans['validation']=[(s,'normal' if i%2==0 else 'hard',None) for i,s in enumerate(validation)]
        unused_dev=[s for s in dev_zh if s['id'] not in {p['id'] for p in pilot_zh}]
        plans['chinese-validation']=[(s,l,None) for s in unused_dev for l in ['normal','hard']]
        plans['final']=[(s,'normal' if i%2==0 else 'hard',None) for i,s in enumerate(test)]+[(s,l,None) for s in chinese_sources if s['split']=='test' for l in ['normal','hard']]
        challenge_sources=[next(s for s in chinese_sources if s.get('family')==f and s['id'].endswith('00')) for f in TEST_FAMILIES]
        challenge_sources += [next(s for s in chinese_sources if s.get('family')==f and s['id'].endswith('01')) for f in TEST_FAMILIES[:2]]
        plans['challenge']=[(s,'hard',kind) for s in challenge_sources for kind in ['occlusion','clipping','tiny','no_table']]
    source_records=[]
    for s in sources+validation+test+chinese_sources:
        source_records.append({k:str(v) if isinstance(v,Path) else v for k,v in s.items() if k not in {'tables','context'}} | {'source_sha256':digest(s['path'])})
    write_json(output/'source-registry.json',{'seed':SEED,'families':{'development':DEVELOPMENT_FAMILIES,'test':TEST_FAMILIES},'sources':source_records,'split_policy':'group original public document and Chinese template before generating variants; prior development exposure acknowledged'})
    for name,plan in plans.items():
        existing=output/name/'freeze.json'
        if existing.exists():
            freeze=json.loads(existing.read_text('utf-8'))
            if freeze['count']!=len(plan) or digest(output/name/'inputs.json')!=freeze['inputs_sha256'] or digest(output/name/'truth.json')!=freeze['truth_sha256']:raise ValueError('Frozen manifest altered')
            print(json.dumps({'manifest':str(output/name/'inputs.json'),'count':len(plan),'already_frozen':True}),flush=True)
            continue
        inputs=[];truth=[]
        for ordinal,(source,level,challenge) in enumerate(plan):
            sid=f'{name}-{ordinal:03d}-{source["id"]}-{challenge or level}'
            sample_seed=SEED+int(hashlib.sha256(sid.encode()).hexdigest()[:8],16)
            with Image.open(source['path']) as original:base=original.convert('RGB')
            im,tables,context,ui,occ,visibility,params,matrix,screen_matrix=render_screen(base,source['tables'],source['context'],level,random.Random(sample_seed),challenge)
            folder=output/name;folder.mkdir(exist_ok=True);(folder/'images').mkdir(exist_ok=True);(folder/'masks').mkdir(exist_ok=True)
            image_path=folder/'images'/f'{sid}.png';save_image(image_path,im)
            occ_path=folder/'masks'/f'{sid}-occlusion.png';save_image(occ_path,occ)
            vis_path=folder/'masks'/f'{sid}-visibility.png';save_image(vis_path,visibility)
            inputs.append({'id':sid,'image':str(image_path),'image_sha256':digest(image_path),'input_kind':'full-screen-photo-simulation','width':im.width,'height':im.height})
            truth.append({'id':sid,'cohort':source['cohort'],'split':source['split'],'group_id':source['group_id'],'source_id':source['id'],'family':source.get('family'),'source_sha256':digest(source['path']),'level':level,'challenge':challenge,'seed':sample_seed,'tables':tables,'context_targets':context,'ui_regions':ui,'parameters':params,'source_to_image':matrix,'screen_to_image':screen_matrix,'occlusion_mask':str(occ_path),'visibility_mask':str(vis_path),'occlusion_mask_sha256':digest(occ_path),'visibility_mask_sha256':digest(vis_path),'expected_risk':challenge is not None and challenge!='no_table','expect_no_table':challenge=='no_table'})
        write_json(folder/'inputs.json',{'protocol_version':1,'name':name,'seed':SEED,'samples':inputs,'truth_available_to_inference':False,'scope':'frozen simulated regression; not real smartphone accuracy or first-blind holdout'})
        write_json(folder/'truth.json',{'protocol_version':1,'name':name,'samples':truth})
        write_json(folder/'freeze.json',{'count':len(inputs),'inputs_sha256':digest(folder/'inputs.json'),'truth_sha256':digest(folder/'truth.json'),'source_count':len({t['source_id'] for t in truth}),'group_count':len({t['group_id'] for t in truth}),'cohorts':{cohort:sum(t['cohort']==cohort for t in truth) for cohort in ['public','chinese']},'levels':{level:sum(t['level']==level for t in truth) for level in ['clean','normal','hard']},'generator_sha256':digest(__file__)})
        gallery(folder,inputs,truth)
        print(json.dumps({'manifest':str(folder/'inputs.json'),'count':len(inputs),'sha256':digest(folder/'inputs.json')},ensure_ascii=False),flush=True)
    if not only_pilot:
        engineer=output/'engineering';engineer.mkdir(exist_ok=True)
        payload=Path(json.loads((output/'pilot/inputs.json').read_text('utf-8'))['samples'][0]['image'])
        entries=[]
        for dirname,filename,data in [('one','重复.png',payload.read_bytes()),('two','重复.png',payload.read_bytes()),('three','重复.png',Path(json.loads((output/'pilot/inputs.json').read_text('utf-8'))['samples'][1]['image']).read_bytes()),('bad','损坏.png',b'not an image')]:
            path=engineer/dirname/filename;path.parent.mkdir(exist_ok=True)
            if path.exists() and path.read_bytes()!=data:raise ValueError('Frozen engineering input differs')
            if not path.exists():path.write_bytes(data)
            entries.append({'image':str(path),'sha256':digest(path),'original_name':filename})
        write_json(engineer/'inputs.json',{'samples':entries,'purpose':'exact duplicate, same-name differing content, corrupt file; not OCR accuracy denominator'})

def gallery(folder, samples, truth):
    chosen=[]
    for cohort in ['chinese','public']:
        candidates=[(s,t) for s,t in zip(samples,truth) if t['cohort']==cohort]
        if cohort=='chinese':
            groups=[];seen=set()
            for s,t in candidates:
                key=(t.get('family'),t.get('challenge') or t['level'])
                if key not in seen:groups.append((s,t));seen.add(key)
            chosen.extend(groups[:16])
        else:chosen.extend(candidates[:12])
    if not chosen:chosen=list(zip(samples,truth))[:12]
    w,h=500,380;canvas=Image.new('RGB',(w*3,h*((len(chosen)+2)//3)),'#dddddd');draw=ImageDraw.Draw(canvas)
    for i,(s,t) in enumerate(chosen):
        im=Image.open(s['image']);im.thumbnail((w-12,h-52));x=(i%3)*w;y=(i//3)*h;canvas.paste(im,(x+6,y+40));draw.text((x+8,y+8),f'{i+1} {t["source_id"][-28:]} {t["challenge"] or t["level"]}',font=font(13),fill='#222222')
    save_image(folder/'gallery.jpg',canvas,quality=94)

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--pilot-only',action='store_true');a=p.parse_args();generate(a.output,a.pilot_only)

if __name__=='__main__':main()
