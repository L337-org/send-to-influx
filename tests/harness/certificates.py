"""Self-signed certificates for the stub endpoints, generated per run.

A Hue bridge is reached over HTTPS with a self-signed certificate, which is why
``hue.insecure`` defaults to true. A stub bridge therefore has to speak TLS or the
collector's own handler cannot be pointed at it, and the certificate has to be generated
rather than committed: a private key in a fixture is a private key in the repository,
whatever it is for.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import datetime
import ipaddress
import os
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def write_self_signed(directory=None):
    """Generate a certificate and key for 127.0.0.1 and write them to disk.

    Args:
        directory (str or None): where to write them; a fresh temporary directory when None

    Returns:
        tuple: ``(certificate_path, key_path)``
    """
    directory = directory or tempfile.mkdtemp(prefix="harness-tls-")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = os.path.join(directory, "certificate.pem")
    key_path = os.path.join(directory, "key.pem")
    with open(certificate_path, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    os.chmod(key_path, 0o600)
    return certificate_path, key_path
