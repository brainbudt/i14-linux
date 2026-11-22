"""Pure offline V3 structure helpers for the hash-bound I14 zero partition.

No I/O, transport, crypto, or write action. The caller must verify ciphertext
with the bound LK hardware algorithm before assembling a usable candidate.
"""
import hashlib
import struct

PARTITION_SIZE = 524288
RECORD_SIZE = 0x1860
CIPHER_OFFSET = 0x2C
CIPHER_SIZE = 0x1830
ORIGINAL_SHA256 = "07854d2fef297a06ba81685e660c332de36d5d18d546927d30daad6d7fda1541"


def plaintext_body() -> bytes:
    """Return the exact body implied by bound init_seccfg + final unlock attr."""
    return (bytes(20 * 0x68)
            + struct.pack("<III", 0, 0x43434343, 0x44444444)
            + bytes(0x1004))


def assemble_partition(original: bytes, ciphertext: bytes) -> bytes:
    """Check custody/lengths and replace only the V3 record; no crypto claim."""
    if len(original) != PARTITION_SIZE:
        raise ValueError("Unexpected original SECCFG partition size")
    if hashlib.sha256(original).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("Original SECCFG partition does not match frozen evidence")
    if len(ciphertext) != CIPHER_SIZE:
        raise ValueError("Ciphertext must cover exactly the 0x1830-byte V3 body")
    header = (b"AND_SECCFG_v" + bytes(4)
              + struct.pack("<5I", 0x4D4D4D4D, 3, RECORD_SIZE, 0, 0x07F20000)
              + bytes(8))
    record = header + ciphertext + struct.pack("<I", 0x45454545)
    return record + original[RECORD_SIZE:]
