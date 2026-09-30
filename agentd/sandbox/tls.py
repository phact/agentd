"""agentd's sandbox CA: TLS for model API hostnames *inside* sandboxes only.

Some harnesses (e.g. Codex with a ChatGPT login) insist on talking to the
real ``https://`` hostname. Inside a sandbox those hostnames resolve to a
loopback endpoint that tunnels the raw TLS stream to a host proxy, which
terminates TLS with a certificate from this CA, adds the real credential,
and forwards to the real service.

The CA is created once per install under ``~/.agentd/ca`` (key readable only
by the user) and is name-constrained to :data:`ALLOWED_HOSTS`, so even a
leaked key cannot mint certificates for anything else. Only sandboxes trust
it (their overlay CA bundle); it is never installed on the host.
"""
from __future__ import annotations

import datetime
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from agentd.sandbox.base import DEFAULT_HOME

CA_DIR = Path(os.environ.get("AGENTD_CA_DIR", DEFAULT_HOME / "ca"))
ALLOWED_HOSTS = ("chatgpt.com", "api.openai.com", "api.anthropic.com")


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def ensure_ca() -> tuple[Path, Path]:
    """(ca cert path, ca key path), creating the CA on first use."""
    cert_path, key_path = CA_DIR / "ca.pem", CA_DIR / "ca-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agentd sandbox CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                          encipher_only=False, decipher_only=False),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(permitted_subtrees=[x509.DNSName(h) for h in ALLOWED_HOSTS], excluded_subtrees=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    _write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def server_context(hostname: str) -> ssl.SSLContext:
    """A TLS server context presenting a CA-signed cert for ``hostname``."""
    if hostname not in ALLOWED_HOSTS:
        raise ValueError(f"{hostname} is not a sandbox TLS host ({ALLOWED_HOSTS})")
    cert_path, key_path = ensure_ca()
    leaf_cert, leaf_key = CA_DIR / f"{hostname}.pem", CA_DIR / f"{hostname}-key.pem"
    if not (leaf_cert.exists() and leaf_key.exists()) or _expiring(leaf_cert):
        ca_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        ca_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write_private(leaf_key, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        leaf_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + cert_path.read_bytes())
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(leaf_cert, leaf_key)
    return ctx


def _expiring(cert_path: Path) -> bool:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) < datetime.timedelta(days=7)
