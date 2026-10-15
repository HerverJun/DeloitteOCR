from workbench_platform.tasks.multipart import input_records,failed_files
import hashlib


def test_failed_file_retry_preserves_binary_bytes_and_reuses_successes():
    def part(name,data):return b'--original\r\nContent-Disposition: form-data; name="files"; filename="'+name.encode()+b'"\r\nContent-Type: application/octet-stream\r\n\r\n'+data+b'\r\n'
    first=b'original-success\x00\n\r\n\xff';second=b'failed-original\x00\n\r\n\x80'
    raw=part('success.bin',first)+part('failed.bin',second)+b'--original--\r\n'
    content_type='multipart/form-data; boundary=original'
    records=input_records(raw,content_type)
    assert [row['blob_hash'] for row in records]==[hashlib.sha256(first).hexdigest(),hashlib.sha256(second).hexdigest()]
    selected,new_type=failed_files(raw,content_type,['failed.bin'],'retry-test')
    new=input_records(selected,new_type)
    assert len(new)==1 and new[0]['blob_hash']==records[1]['blob_hash'] and new[0]['size_bytes']==len(second)
