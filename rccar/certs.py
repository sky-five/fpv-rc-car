"""
HTTPS certificates. Phone browsers only allow camera access on https://, so the
stream server needs a certificate — and the laptop's IP changes between networks.

Two layers:
  * A private certificate authority (CA), created once in .rccar/certs/ and never
    changed. Install ca.crt on the phone once (optional; the cockpit links to it)
    and the certificate warning is gone for good, on every network.
  * A server certificate signed by that CA, covering localhost, this machine's
    name, and every LAN IP it has had. It's re-issued automatically when a new IP
    shows up — which, with the CA installed, needs no clicks at all.

Without the CA installed it still works exactly like before: tap
"Advanced -> Proceed" once per new address.
"""

import datetime
import ipaddress
import json
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import config

CERT_DIR = os.path.join(config.STATE_DIR, "certs")
CA_CERT = os.path.join(CERT_DIR, "ca.crt")
CA_KEY = os.path.join(CERT_DIR, "ca.key")
SERVER_CERT = os.path.join(CERT_DIR, "server.crt")
SERVER_KEY = os.path.join(CERT_DIR, "server.key")
SEEN_IPS = os.path.join(CERT_DIR, "ips.json")
MAX_REMEMBERED_IPS = 24


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _write_key(path, key):
    with open(path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))


def _write_cert(path, cert):
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _load_ca():
    with open(CA_KEY, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    with open(CA_CERT, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    return key, cert


def _make_ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RC FPV local CA"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "fpv-rc-car")])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(days=1))
        .not_valid_after(_now() + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                     content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(CA_KEY, key)
    _write_cert(CA_CERT, cert)
    return key, cert


def _remembered_ips():
    try:
        with open(SEEN_IPS, encoding="utf-8") as f:
            return [ip for ip in json.load(f) if isinstance(ip, str)]
    except (OSError, ValueError):
        return []


def _server_cert_covers(names, ips):
    try:
        with open(SERVER_CERT, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have_ips = {str(v) for v in san.get_values_for_type(x509.IPAddress)}
        have_names = {v.lower() for v in san.get_values_for_type(x509.DNSName)}
        fresh = cert.not_valid_after_utc - _now() > datetime.timedelta(days=30)
        return fresh and set(ips) <= have_ips and {n.lower() for n in names} <= have_names
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False


def ensure(current_ips):
    """Make sure a CA and a server cert covering current_ips exist.

    Returns (server_cert_path, server_key_path, reissued: bool).
    """
    os.makedirs(CERT_DIR, exist_ok=True)
    if os.path.exists(CA_CERT) and os.path.exists(CA_KEY):
        ca_key, ca_cert = _load_ca()
    else:
        ca_key, ca_cert = _make_ca()

    host = socket.gethostname()
    names = ["localhost", host, f"{host}.local"]
    if _server_cert_covers(names, current_ips) and os.path.exists(SERVER_KEY):
        return SERVER_CERT, SERVER_KEY, False

    # Remember every address we've had so hopping between networks (home / RC
    # field / hotspot) settles down instead of re-issuing forever.
    ips = list(dict.fromkeys(list(current_ips) + _remembered_ips()))[:MAX_REMEMBERED_IPS]
    san = [x509.DNSName(n) for n in names]
    san.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rc-cockpit")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(days=1))
        .not_valid_after(_now() + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False,
                                     content_commitment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(SERVER_KEY, key)
    # Serve the chain (server + CA) so clients that trust the CA can build the path.
    with open(SERVER_CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    with open(SEEN_IPS, "w", encoding="utf-8") as f:
        json.dump(ips, f)
    return SERVER_CERT, SERVER_KEY, True


def ca_der():
    """The CA certificate in DER form (what Android's 'Install certificate' wants)."""
    with open(CA_CERT, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    return cert.public_bytes(serialization.Encoding.DER)
