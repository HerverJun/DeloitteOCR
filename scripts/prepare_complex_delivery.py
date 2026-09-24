"""Prepare bounded source and release notes from the current dirty working tree."""
import json
from pathlib import Path
import shutil
import zipfile
from complex_table_run import ROOT, BUILD, AUDIT, save, sha


def main():
    target = BUILD/'delivery-input-v2'
    target.mkdir(exist_ok=True)
    info = target/'delivery-info'
    info.mkdir(exist_ok=True)
    for relative in ['regression/backend.json','regression/frontend.json','regression/build.json',
                     'regression/browser.json','final-acceptance.json','gaps.json',
                     'local/grouped-results.json','performance/direct-axis-index-v3.json']:
        dest=info/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(AUDIT/relative,dest)
    for name in ['complex-tables-guide-20260919.md','complex-tables-results-20260919.md']:
        shutil.copy2(ROOT/'docs'/name,info/name)
    shutil.copy2(BUILD/'packaged-smoke-02/receipt.json',info/'packaged-cpu-smoke.json')
    (target/'开始前请读.txt').write_text('''DeloitteOCR 复杂表格实验交付 · 2026-09-19
应用版本：0.13.0rc1；前端：0.13.0-rc.1；工作区数据库：schema 12。

1. 完整解压 ZIP，进入 OfflineOCR，双击“启动工作台.cmd”。不要在压缩软件内运行。
   建议使用较短路径；ZIP 超过 4GB，移动介质须为 NTFS/exFAT。建议保留至少 80GB 可用空间。
   包含离线 Python、模型、依赖和网页，无需安装 Python、Node.js 或 CUDA Toolkit。
   目标机须预装 Windows x64、Edge/Chrome 和兼容 NVIDIA 驱动。
   完整识别面向 16GB 显存，环境检查要求总量至少 14GiB，建议空闲约 12GB。
   无可用 GPU 可用“仅校对模式.cmd”，支持已有结果、导出、普通图像处理及原生 PDF。

2. 本地 OCR 和本地视觉审校使用包内资源。默认不会因打开页面而向外部服务发送文档。
   外部 API 功能需要配置连接并主动提交；结构仲裁还须勾选发送确认。
   发送内容包括当前页图像、已有文字片段、表结构候选及相关证据。
   断网不影响本地已有功能；外部 API 不可用时保留本地结果，不自动重发。
   外部模型只推荐已有合法候选或弃权，采用由用户操作。API 可能收费。

3. 表格结果进入“结构复核”，对照原图、受影响行列和数字格查看建议。
   接受后可使用撤销；财务一致性只列疑点，不为凑平合计修改原文字。
   “表格定位与人工绑定”可选 local-v4 实验策略；默认仍为 local-v2。
   复杂合并、重复金额、未知空值与底纹候选仍应人工核对。

4. 当前说明：docs/complex-tables-guide-20260919.md；结果与限制：
   docs/complex-tables-results-20260919.md；本次工程回执：delivery-info。
   当前完整源码快照：source-code.zip，包含打包时工作树修改。
   output、audit、maintenance 内继承的旧手册、旧快照和验收记录仅代表历史版本，
   其中旧版本号、数据库号、“不上传文档”等描述不适用于本版外部 API 功能。
   新版完整文件以根 manifest.json 和新交付校验回执为准。

5. 默认项目位于 %LOCALAPPDATA%/OfflineOCR/Workspace，升级前关闭程序并备份整个目录。
   schema 12 工作区不能交给旧程序继续写入。包内不带个人 API 凭据、业务项目或本轮原始评测材料。
   “核对文件完整性.cmd”用于解压后校验；“查看环境诊断.cmd”查看驱动／设备条件。

6. 工程回归通过不等于正式质量达标。新独立数据规模、照片／扫描标签、封存组仍不足；
   大表完整对应耗时仍可能超过 200ms。真实 API 净收益、费用、人工省时及目标设备 GPU 本轮未测。
   金额、编号、复杂表头、手写和导出定位须核对。此包为实验交付。
''','utf-8-sig')
    roots=['src','config','scripts','tests','docs','licenses','fixtures','frontend/src','frontend/public','frontend/scripts']
    allowed={'.py','.json','.md','.txt','.toml','.yaml','.yml','.ts','.tsx','.js','.mjs','.cjs',
             '.css','.html','.svg','.png','.jpg','.jpeg','.ico','.woff','.woff2','.ttf','.csv'}
    paths=[]
    for root in roots:
        folder=ROOT/root
        if folder.exists():
            paths += [p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in allowed
                      and '__pycache__' not in p.parts and 'node_modules' not in p.parts]
    paths += [p for p in (ROOT/'frontend').iterdir() if p.is_file() and p.suffix.lower() in allowed]
    paths += [ROOT/n for n in ['README.md','LICENSE','.gitignore','.gitattributes','开发总纲.md']]
    paths=sorted(set(paths))
    records=[]
    archive=target/'source-code.zip'
    if archive.exists():raise ValueError('Source archive already exists; do not overwrite a release input')
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for path in paths:
            relative=path.relative_to(ROOT).as_posix()
            if path.is_symlink() or path.stat().st_size>20*1024*1024:raise ValueError(relative)
            data=path.read_bytes()
            import hashlib
            records.append({'path':relative,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            z.writestr('OCR-source/'+relative,data)
        z.writestr('OCR-source/source-manifest.json',json.dumps({'version':'0.13.0rc1','working_tree_snapshot':True,'files':records},ensure_ascii=False,indent=2))
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert len(z.namelist())==len(records)+1
    save(info/'source-receipt.json',{'archive':'source-code.zip','sha256':sha(archive),'files':records,
        'excluded':['.git','build','audit','research','output','node_modules','frontend/dist','personal credentials','user workspaces']})
    save(AUDIT/'delivery-input.json',{'source_archive':str(archive),'sha256':sha(archive),'files':len(records),
         'readme_first_sha256':sha(target/'开始前请读.txt')})
    print(json.dumps({'input':str(target),'source_files':len(records),'source_bytes':archive.stat().st_size}))


if __name__=='__main__':main()
