"""
Kestrel cryptographic primitives.

- sha256d:      double SHA-256, used for block ids, txids and merkle trees
- scrypt_hash:  scrypt(1024,1,1), used only as the proof-of-work function
- hash160:      double SHA-256 truncated to 20 bytes (address hash).
                Kestrel deliberately uses truncated SHA-256d instead of
                RIPEMD-160(SHA-256) so nodes have zero dependency on the
                RIPEMD-160 availability of the local OpenSSL build.
- base58check:  Bitcoin-style address encoding with a 4-byte checksum
- ECDSA:        secp256k1 keys, compressed public keys, DER signatures
"""

import hashlib
import os

from ecdsa import SigningKey, VerifyingKey, SECP256k1, BadSignatureError
from ecdsa.util import sigencode_der_canonize, sigdecode_der

from . import params

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# --------------------------------------------------------------- hashing

def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sha256d(data: bytes) -> bytes:
    """Double SHA-256 (Bitcoin-style)."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def scrypt_hash(data: bytes) -> bytes:
    """Proof-of-work hash. Litecoin-style scrypt with the data as its own salt."""
    return hashlib.scrypt(
        data, salt=data,
        n=params.SCRYPT_N, r=params.SCRYPT_R, p=params.SCRYPT_P,
        dklen=32,
    )


def hash160(data: bytes) -> bytes:
    """20-byte address hash: SHA-256d truncated to 160 bits."""
    return sha256d(data)[:20]


# ----------------------------------------------------------- base58check

def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(B58_ALPHABET[rem])
    # preserve leading zero bytes as '1'
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(out))


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58_ALPHABET.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def b58check_encode(version: int, payload: bytes) -> str:
    data = bytes([version]) + payload
    checksum = sha256d(data)[:4]
    return b58encode(data + checksum)


def b58check_decode(s: str) -> tuple[int, bytes]:
    raw = b58decode(s)
    if len(raw) < 5:
        raise ValueError("base58check string too short")
    data, checksum = raw[:-4], raw[-4:]
    if sha256d(data)[:4] != checksum:
        raise ValueError("bad base58check checksum")
    return data[0], data[1:]


# ------------------------------------------------------------------ keys

def generate_private_key() -> bytes:
    """32 random bytes, valid for secp256k1 with overwhelming probability."""
    while True:
        key = os.urandom(32)
        if 0 < int.from_bytes(key, "big") < SECP256k1.order:
            return key


def private_to_public(private_key: bytes) -> bytes:
    """Compressed 33-byte secp256k1 public key."""
    sk = SigningKey.from_string(private_key, curve=SECP256k1)
    return sk.get_verifying_key().to_string("compressed")


def pubkey_to_address(pubkey: bytes) -> str:
    """Kestrel address: base58check(0x2D || hash160(pubkey)). Starts with 'K'."""
    return b58check_encode(params.ADDRESS_VERSION, hash160(pubkey))


def address_to_hash(address: str) -> bytes:
    version, payload = b58check_decode(address)
    if version != params.ADDRESS_VERSION:
        raise ValueError(f"not a Kestrel address (version byte {version:#x})")
    if len(payload) != 20:
        raise ValueError("bad address payload length")
    return payload


def is_valid_address(address: str) -> bool:
    try:
        address_to_hash(address)
        return True
    except Exception:
        return False


def private_to_wif(private_key: bytes) -> str:
    """Wallet-import-format export (compressed-key flag appended)."""
    return b58check_encode(params.WIF_VERSION, private_key + b"\x01")


def wif_to_private(wif: str) -> bytes:
    version, payload = b58check_decode(wif)
    if version != params.WIF_VERSION:
        raise ValueError("not a Kestrel WIF key")
    if len(payload) == 33 and payload[-1] == 0x01:
        payload = payload[:-1]
    if len(payload) != 32:
        raise ValueError("bad WIF payload length")
    return payload


# ------------------------------------------------------------ signatures

def sign(private_key: bytes, digest: bytes) -> bytes:
    """Deterministic (RFC 6979) ECDSA signature over a 32-byte digest,
    DER-encoded in canonical low-S form (s <= n/2)."""
    sk = SigningKey.from_string(private_key, curve=SECP256k1)
    return sk.sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=sigencode_der_canonize
    )


def verify(pubkey: bytes, signature: bytes, digest: bytes) -> bool:
    """Strict verification: DER must parse and s must be low (canonical).

    Rejecting high-S signatures closes the classic ECDSA malleability
    hole — a third party can no longer flip s to n-s and change a
    transaction's txid while keeping the signature valid.
    """
    try:
        order = SECP256k1.order
        _r, s = sigdecode_der(signature, order)
        if not (0 < s <= order // 2):
            return False
        vk = VerifyingKey.from_string(pubkey, curve=SECP256k1)
        return vk.verify_digest(signature, digest, sigdecode=sigdecode_der)
    except Exception:
        return False
