"""Ephemeral local certificate material only; never deployment certificates."""
from datetime import datetime,timedelta,timezone
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID,NameOID


def certificates(directory, *, broker_uri="urn:foodtech:service:combi-read-broker", server_dns=None):
    now=datetime.now(timezone.utc)
    def ca(name):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name)])
        cert=x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(
            x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1)).add_extension(
            x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256())
        path=directory/(name+'.pem');path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return key,cert,str(path)
    root=ca('root'); other=ca('other-root')
    def issue(name,issuer,usage,uri=None,server=False,reuse_key=None,common_name=None):
        key=serialization.load_pem_private_key(Path(reuse_key).read_bytes(),password=None) if reuse_key else rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,common_name or name)])
        builder=x509.CertificateBuilder().subject_name(subject).issuer_name(issuer[1].subject).public_key(key.public_key()).serial_number(
            x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1)).add_extension(
            x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.ExtendedKeyUsage([usage]),critical=False)
        if server:
            san = x509.DNSName(server_dns) if server_dns else x509.IPAddress(ipaddress.ip_address('127.0.0.1'))
            builder = builder.add_extension(x509.SubjectAlternativeName([san]), critical=False)
        elif uri: builder=builder.add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]),critical=False)
        cert=builder.sign(issuer[0],hashes.SHA256())
        cp,kp=directory/(name+'.pem'),directory/(name+'.key')
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));kp.chmod(0o600)
        return str(cp),str(kp)
    uri=broker_uri
    server=issue('server',root,ExtendedKeyUsageOID.SERVER_AUTH,server=True)
    return dict(ca=root[2],server=server,
        other_server=issue('other-server',root,ExtendedKeyUsageOID.SERVER_AUTH,server=True),
        other_server_same_key=issue('other-server-same-key',root,ExtendedKeyUsageOID.SERVER_AUTH,server=True,reuse_key=server[1],common_name='server'),
        broker=issue('broker',root,ExtendedKeyUsageOID.CLIENT_AUTH,uri),
        other_broker=issue('other-broker',root,ExtendedKeyUsageOID.CLIENT_AUTH,uri),
        business=issue('business',root,ExtendedKeyUsageOID.CLIENT_AUTH,'urn:foodtech:service:business-api'),
        other_business=issue('other-business',root,ExtendedKeyUsageOID.CLIENT_AUTH,'urn:foodtech:service:business-api'),
        wrong_ca=issue('wrong-ca-client',other,ExtendedKeyUsageOID.CLIENT_AUTH,uri))
