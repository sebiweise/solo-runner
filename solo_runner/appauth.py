"""GitHub App authentication without third-party packages.

A GitHub App signs a short JWT with RS256 (RSASSA-PKCS1-v1_5 with SHA-256)
and trades it for an installation token. The standard library has no RSA, but
signing is one modular exponentiation once the key is parsed, so this module
reads the PEM key (PKCS#1 as GitHub hands it out, or PKCS#8) itself.

Signing happens a few times per hour on a host that already trusts the key,
so the lack of constant-time arithmetic is not a practical concern here.
"""

import base64
import hashlib
import json
import time

# DigestInfo prefix for SHA-256 (RFC 8017, section 9.2, note 1).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_RSA_OID = bytes.fromhex("2a864886f70d010101")


class KeyFormatError(ValueError):
    pass


def _read_tlv(data: bytes, pos: int):
    """One DER element at pos: (tag, value, next position)."""
    if pos + 2 > len(data):
        raise KeyFormatError("truncated key")
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or pos + count > len(data):
            raise KeyFormatError("bad length in key")
        length = int.from_bytes(data[pos : pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise KeyFormatError("truncated key")
    return tag, data[pos : pos + length], pos + length


def _sequence(data: bytes) -> list:
    tag, body, _ = _read_tlv(data, 0)
    if tag != 0x30:
        raise KeyFormatError("expected a DER SEQUENCE")
    items, pos = [], 0
    while pos < len(body):
        tag, value, pos = _read_tlv(body, pos)
        items.append((tag, value))
    return items


def _pkcs1(der: bytes):
    items = _sequence(der)
    if len(items) < 4 or any(tag != 0x02 for tag, _ in items[:4]):
        raise KeyFormatError("not an RSA private key")
    n = int.from_bytes(items[1][1], "big")
    d = int.from_bytes(items[3][1], "big")
    return n, d


def parse_private_key(pem: str):
    """(n, d) from a PEM RSA key in PKCS#1 or unencrypted PKCS#8 form."""
    lines = [line.strip() for line in pem.strip().splitlines()]
    if not lines or not lines[0].startswith("-----BEGIN"):
        raise KeyFormatError("not a PEM key")
    kind = lines[0]
    body = base64.b64decode("".join(line for line in lines if line and not line.startswith("-----")))
    if "RSA PRIVATE KEY" in kind:
        return _pkcs1(body)
    if "ENCRYPTED" in kind:
        raise KeyFormatError("encrypted keys are not supported; use the key GitHub generated")
    if "PRIVATE KEY" in kind:
        items = _sequence(body)
        if len(items) < 3 or items[1][0] != 0x30 or items[2][0] != 0x04:
            raise KeyFormatError("not a PKCS#8 key")
        oid_tag, oid, _ = _read_tlv(items[1][1], 0)
        if oid_tag != 0x06 or oid != _RSA_OID:
            raise KeyFormatError("not an RSA key")
        return _pkcs1(items[2][1])
    raise KeyFormatError(f"unsupported key type: {kind}")


def sign_rs256(message: bytes, n: int, d: int) -> bytes:
    size = (n.bit_length() + 7) // 8
    digest_info = _SHA256_PREFIX + hashlib.sha256(message).digest()
    padding = size - len(digest_info) - 3
    if padding < 8:
        raise KeyFormatError("key too small")
    encoded = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return pow(int.from_bytes(encoded, "big"), d, n).to_bytes(size, "big")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, pem: str, now=None) -> str:
    """A JWT for the GitHub App API, valid for 9 minutes (GitHub allows 10)."""
    now = int(time.time() if now is None else now)
    n, d = parse_private_key(pem)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    # iat 60 s in the past absorbs clock drift, as GitHub recommends.
    claims = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    return f"{header}.{payload}.{_b64url(sign_rs256(signing_input, n, d))}"
