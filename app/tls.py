"""Self-signed TLS helpers for the dashboard.

The browser only exposes ``navigator.mediaDevices.getUserMedia`` (the mic) in a
*secure context*: HTTPS, or a ``localhost`` origin. Reaching the UI from another
device over the LAN (``http://192.168.x.y:8080``) is therefore an INSECURE
context and the mic is silently unavailable — uplink voice can never start.

Serving the dashboard over HTTPS fixes that. We don't need a real CA: a
self-signed cert makes the origin secure once the user accepts the one-time
browser warning. This module builds/caches such a cert (covering localhost plus
the machine's detected LAN IPs) and hands back an ``ssl.SSLContext``.
"""
from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
from pathlib import Path


def _local_ips() -> list[str]:
    """Best-effort list of this machine's IPv4 addresses (for cert SANs)."""
    ips: set[str] = {"127.0.0.1"}
    host = socket.gethostname()
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # The classic "connect a UDP socket to a public IP" trick to learn the
    # primary LAN address without sending anything.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return sorted(ips)


def default_cert_dir() -> Path:
    d = Path.home() / ".tnmm_ui" / "tls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_self_signed(cert_dir: Path | None = None) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating a cached self-signed pair if
    missing. Requires the ``cryptography`` package.
    """
    cert_dir = cert_dir or default_cert_dir()
    cert_path = cert_dir / "dashboard-cert.pem"
    key_path = cert_dir / "dashboard-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "TLS requested but the 'cryptography' package is not installed. "
            "Install it (pip install cryptography) or pass --tls-cert/--tls-key."
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tnmm-ui")])

    sans: list[x509.GeneralName] = [x509.DNSName("localhost")]
    try:
        sans.append(x509.DNSName(socket.gethostname()))
    except Exception:  # pragma: no cover - defensive
        pass
    for ip in _local_ips():
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:  # pragma: no cover - defensive
            pass

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def build_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx
