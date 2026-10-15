"""Inspect uploaded originals and select failed files without changing bytes."""
from email.parser import BytesParser
from email import policy
import hashlib
from .store import LedgerError


def parts(raw,content_type):
    if not content_type.lower().startswith('multipart/form-data'):return []
    message=BytesParser(policy=policy.default).parsebytes(b'Content-Type: '+content_type.encode('ascii')+b'\r\nMIME-Version: 1.0\r\n\r\n'+raw)
    if not message.is_multipart():raise LedgerError('上传请求缺少有效的文件边界')
    return list(message.iter_parts())


def input_records(raw,content_type):
    result=[]
    for number,part in enumerate(parts(raw,content_type)):
        name=part.get_filename()
        if name is None:continue
        data=part.get_payload(decode=True);digest=hashlib.sha256(data).hexdigest()
        name=name.replace('\\','/').rsplit('/',1)[-1]
        identity=hashlib.sha256((str(number)+'\0'+name+'\0'+digest).encode()).hexdigest()[:32]
        result.append({'asset_id':'native-upload:'+identity,'asset_revision':1,'blob_hash':digest,'name':name,'size_bytes':len(data),'source_locator':{'upload_name':name,'upload_ordinal':number}})
    return result


def failed_files(raw,content_type,names,request_id):
    selected=parts(raw,content_type)
    file_names=[part.get_filename() for part in selected if part.get_filename() is not None]
    if not selected or len(file_names)!=len(set(file_names)) or not set(names)<=set(file_names):
        raise LedgerError('导入回执无法唯一定位失败文件，请在工具中重新选择失败项',409)
    boundary='clear-retry-'+request_id;chunks=[]
    for part in selected:
        filename=part.get_filename()
        if filename is not None and filename not in names:continue
        name=part.get_param('name',header='content-disposition') or 'files'
        for value in (name,filename or ''):
            if any(character in value for character in ('\r','\n','"')):raise LedgerError('上传文件名无法安全重放',409)
        header=f'Content-Disposition: form-data; name="{name}"'+(f'; filename="{filename}"' if filename is not None else '')
        chunks.extend([('--'+boundary+'\r\n'+header+'\r\nContent-Type: '+part.get_content_type()+'\r\n\r\n').encode('utf-8'),part.get_payload(decode=True),b'\r\n'])
    chunks.append(('--'+boundary+'--\r\n').encode())
    return b''.join(chunks),'multipart/form-data; boundary='+boundary
