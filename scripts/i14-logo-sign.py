#!/usr/bin/env python3
"""Sign a logo image for the I14 LK logo check (RSA-2048-PSS, SHA-256, MGF1-SHA-256, 32-byte salt).

  i14-logo-sign.py IN.img KEY.json LK.img OUT.img

IN is an image from `i14-logo.py replace` (signature slot zero or stale). KEY.json holds the private key
(artifacts/splash-key/i14-logo-privkey.json, never in git; see docs/research/SPLASH-KEY-2026-11-03.md).
The key must belong to LK's logo modulus (checked against lk.img). OUT is IN with a fresh signature in
the slot, and is verified with LK's public key before it is written. Offline only; touches no phone.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("i14_logo", ROOT / "scripts/i14-logo.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def pss_sign(message, n, d, salt=None):
    """EMSA-PSS encode (SHA-256, MGF1-SHA-256, salt 32 B) and the RSA private operation."""
    k, mod_bits, h_len, salt_len = (n.bit_length() + 7) // 8, n.bit_length(), 32, 32
    em_len = (mod_bits - 1 + 7) // 8
    salt = secrets.token_bytes(salt_len) if salt is None else salt
    h = hashlib.sha256(b"\0" * 8 + hashlib.sha256(message).digest() + salt).digest()
    db = b"\0" * (em_len - salt_len - h_len - 2) + b"\x01" + salt
    masked = bytearray(a ^ b for a, b in zip(db, tool._mgf1(h, len(db))))
    unused = 8 * em_len - (mod_bits - 1)
    if unused:
        masked[0] &= 0xFF >> unused
    em = bytes(masked) + h + b"\xbc"
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def sign_image(data, key, lk_data):
    modulus = tool.lk_modulus(lk_data)  # LogoError unless lk.img holds the known logo key
    n, d = int(key["modulus_n_hex"], 16), int(key["private_exponent_d_hex"], 16)
    if n.to_bytes(256, "big") != modulus or key.get("public_exponent_e") != tool.RSA_E:
        raise tool.LogoError("key does not belong to LK's logo modulus")
    logo = tool.Logo(bytes(data))
    sig = pss_sign(logo.signed_range(), n, d)
    out = bytearray(data)
    out[logo.sig_offset:logo.sig_offset + 256] = sig
    if tool.Logo(bytes(out)).signature_state(modulus) != "valid":
        raise tool.LogoError("fresh signature does not verify")
    return bytes(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    src, key, lk, out = map(Path, argv)
    signed = sign_image(src.read_bytes(), json.loads(key.read_text()), lk.read_bytes())
    try:
        tool.write_new(out, signed)  # exclusive create: never overwrites
    except FileExistsError:
        raise tool.LogoError(f"{out} exists; refusing to overwrite") from None
    print(json.dumps({"out": str(out), "sha256": hashlib.sha256(signed).hexdigest(), "signature": "valid"}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except tool.LogoError as exc:
        print(f"i14-logo-sign: {exc}", file=sys.stderr)
        sys.exit(1)
