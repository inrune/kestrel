"""Cryptographic primitives: hashing, base58check, keys, signatures."""

import unittest

from kestrel import params
from kestrel.crypto_utils import (
    sha256d, hash160, b58encode, b58decode, b58check_encode, b58check_decode,
    generate_private_key, private_to_public, pubkey_to_address,
    address_to_hash, is_valid_address, private_to_wif, wif_to_private,
    sign, verify,
)
from ecdsa import SECP256k1
from ecdsa.util import sigdecode_der


class TestHashing(unittest.TestCase):
    def test_sha256d_known_vector(self):
        # sha256d("") — the standard Bitcoin double-SHA of empty input
        self.assertEqual(
            sha256d(b"").hex(),
            "5df6e0e2761359d30a8275058e299fcc0381534545f55cf43e41983f5d4c9456",
        )

    def test_hash160_is_20_bytes(self):
        self.assertEqual(len(hash160(b"kestrel")), 20)


class TestBase58(unittest.TestCase):
    def test_roundtrip_random(self):
        for _ in range(200):
            raw = generate_private_key()
            self.assertEqual(b58decode(b58encode(raw)), raw)

    def test_leading_zero_bytes_preserved(self):
        data = b"\x00\x00\x05kestrel"
        self.assertEqual(b58decode(b58encode(data)), data)

    def test_check_roundtrip(self):
        payload = b"\x11" * 20
        s = b58check_encode(params.ADDRESS_VERSION, payload)
        ver, back = b58check_decode(s)
        self.assertEqual(ver, params.ADDRESS_VERSION)
        self.assertEqual(back, payload)

    def test_bad_checksum_rejected(self):
        s = b58check_encode(params.ADDRESS_VERSION, b"\x11" * 20)
        bad = s[:-1] + ("A" if s[-1] != "A" else "B")
        with self.assertRaises(ValueError):
            b58check_decode(bad)


class TestKeysAndAddresses(unittest.TestCase):
    def test_private_key_in_range(self):
        for _ in range(50):
            k = int.from_bytes(generate_private_key(), "big")
            self.assertTrue(0 < k < SECP256k1.order)

    def test_public_key_compressed(self):
        pub = private_to_public(generate_private_key())
        self.assertEqual(len(pub), 33)
        self.assertIn(pub[0], (2, 3))

    def test_address_starts_with_K(self):
        addr = pubkey_to_address(private_to_public(generate_private_key()))
        self.assertTrue(addr.startswith("K"))
        self.assertTrue(is_valid_address(addr))

    def test_address_roundtrip(self):
        pub = private_to_public(generate_private_key())
        addr = pubkey_to_address(pub)
        self.assertEqual(address_to_hash(addr), hash160(pub))

    def test_genesis_placeholder_is_not_valid(self):
        # build_genesis pays "K"*34 on purpose so the reward is unspendable
        self.assertFalse(is_valid_address("K" * 34))

    def test_garbage_and_tampered_rejected(self):
        self.assertFalse(is_valid_address("hello"))
        self.assertFalse(is_valid_address(""))
        addr = pubkey_to_address(private_to_public(generate_private_key()))
        tampered = addr[:-1] + ("A" if addr[-1] != "A" else "B")
        self.assertFalse(is_valid_address(tampered))

    def test_wif_roundtrip(self):
        priv = generate_private_key()
        self.assertEqual(wif_to_private(private_to_wif(priv)), priv)

    def test_wif_wrong_version_rejected(self):
        # an address string is not a WIF key
        addr = pubkey_to_address(private_to_public(generate_private_key()))
        with self.assertRaises(ValueError):
            wif_to_private(addr)


class TestSignatures(unittest.TestCase):
    def test_sign_verify_roundtrip(self):
        priv = generate_private_key()
        pub = private_to_public(priv)
        digest = sha256d(b"spend 3.5 KSL")
        sig = sign(priv, digest)
        self.assertTrue(verify(pub, sig, digest))

    def test_wrong_message_fails(self):
        priv = generate_private_key()
        pub = private_to_public(priv)
        sig = sign(priv, sha256d(b"a"))
        self.assertFalse(verify(pub, sig, sha256d(b"b")))

    def test_wrong_key_fails(self):
        priv = generate_private_key()
        other = private_to_public(generate_private_key())
        digest = sha256d(b"x")
        self.assertFalse(verify(other, sign(priv, digest), digest))

    def test_signatures_are_deterministic(self):
        # RFC 6979: same key + message -> byte-identical signature
        priv = generate_private_key()
        digest = sha256d(b"determinism")
        self.assertEqual(sign(priv, digest), sign(priv, digest))

    def test_high_s_signature_rejected(self):
        # malleability guard: flipping s to n-s must fail verification even
        # though the (r, n-s) pair is otherwise a valid ECDSA signature
        from ecdsa.util import sigencode_der
        priv = generate_private_key()
        pub = private_to_public(priv)
        digest = sha256d(b"malleable")
        sig = sign(priv, digest)
        r, s = sigdecode_der(sig, SECP256k1.order)
        high = sigencode_der(r, SECP256k1.order - s, SECP256k1.order)
        self.assertFalse(verify(pub, high, digest))


if __name__ == "__main__":
    unittest.main()
