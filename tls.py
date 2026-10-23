"""Generate a local self-signed certificate for encrypted LAN transport."""

from __future__ import annotations

import datetime
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from twonote.vault import atomic_write


def ensure_certificate(cert_path: Path, key_path: Path, password: bytes) -> None:
    if cert_path.is_file() and key_path.is_file():
        raw = key_path.read_bytes()
        if b"ENCRYPTED PRIVATE KEY" not in raw:
            existing = serialization.load_pem_private_key(raw, password=None)
            atomic_write(key_path, existing.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password)))
        return
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names: list[x509.GeneralName] = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            names.append(x509.IPAddress(ipaddress.ip_address(address)))
    except (socket.gaierror, ValueError):
        pass
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TwoNote LAN")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True, data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    atomic_write(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password)))
    atomic_write(cert_path, certificate.public_bytes(serialization.Encoding.PEM))
