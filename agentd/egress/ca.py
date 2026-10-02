"""A certificate authority for one sandbox session, held in memory only.

The sandbox trusts its public certificate; the egress proxy presents leaf
certificates from it for the hosts it intercepts (where it swaps placeholders
for secrets). The CA's private key is never written anywhere; it dies with
the session. Python's ``ssl`` only loads certificates from files, so each
leaf's key goes through a private temporary file that's deleted as soon as
it's loaded.
"""
from __future__ import annotations

import datetime
import os
import ssl
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class SessionCA:
    def __init__(self, name: str = "agentd sandbox egress"):
        self._key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        self._contexts: dict[tuple[str, tuple[str, ...]], ssl.SSLContext] = {}

    @property
    def pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def server_context(self, host: str, alpn: tuple[str, ...] = ("h2", "http/1.1")) -> ssl.SSLContext:
        """A TLS server context presenting a certificate for ``host``."""
        key = (host, alpn)
        if key in self._contexts:
            return self._contexts[key]
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self.cert.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if alpn:
            ctx.set_alpn_protocols(list(alpn))
        fd, path = tempfile.mkstemp(prefix="agentd-leaf-", suffix=".pem")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(leaf.public_bytes(serialization.Encoding.PEM))
                f.write(self.pem)
                f.write(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
            ctx.load_cert_chain(path)
        finally:
            Path(path).unlink(missing_ok=True)
        self._contexts[key] = ctx
        return ctx
