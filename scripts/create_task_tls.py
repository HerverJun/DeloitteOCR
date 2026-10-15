"""Create an F-drive evaluation certificate without touching a system store."""
import argparse
import getpass
import ipaddress
import os
import subprocess
from datetime import datetime,timedelta,timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from workbench_platform.tasks.database import f_path

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True);args=parser.parse_args()
    root=f_path(args.directory);root.mkdir(parents=True,exist_ok=True)
    certificate=root/'server.crt';private=root/'server.key'
    if certificate.exists() or private.exists():raise ValueError('Existing TLS material must not be overwritten')
    key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Clear local evaluation')]);now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
          .sign(key,hashes.SHA256()))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if os.name=='nt':subprocess.run(['icacls',str(private),'/inheritance:r','/grant:r',getpass.getuser()+':(F)'],capture_output=True,check=True)
    print('Evaluation certificate saved on F:. Production certificates must contain the actual intranet hostname.')
