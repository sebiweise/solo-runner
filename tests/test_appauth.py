import base64
import json
import subprocess

import pytest

from solo_runner.appauth import KeyFormatError, app_jwt, parse_private_key


def _b64url_decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


@pytest.fixture(scope="module", params=["pkcs1", "pkcs8"])
def key(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("keys") / "key.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(path)],
        check=True,
        capture_output=True,
    )
    if request.param == "pkcs1":
        subprocess.run(
            ["openssl", "rsa", "-in", str(path), "-out", str(path), "-traditional"], check=True, capture_output=True
        )
    pub = path.with_suffix(".pub")
    subprocess.run(["openssl", "rsa", "-in", str(path), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    return path, pub


def test_jwt_verifies_with_openssl(key, tmp_path):
    priv, pub = key
    token = app_jwt("12345", priv.read_text(), now=1_700_000_000)
    header, payload, signature = token.split(".")
    assert json.loads(_b64url_decode(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64url_decode(payload))
    assert claims == {"iat": 1_699_999_940, "exp": 1_700_000_540, "iss": "12345"}
    (tmp_path / "msg").write_bytes(f"{header}.{payload}".encode())
    (tmp_path / "sig").write_bytes(_b64url_decode(signature))
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(tmp_path / "sig"), str(tmp_path / "msg")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_garbage():
    with pytest.raises(KeyFormatError):
        parse_private_key("not a key")
    with pytest.raises(KeyFormatError):
        parse_private_key("-----BEGIN EC PRIVATE KEY-----\nAAAA\n-----END EC PRIVATE KEY-----")
