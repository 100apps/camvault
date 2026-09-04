from __future__ import annotations

import pytest

from camvault.crypto import (
    ARCHIVE_ENCRYPTION_HEADER_BYTES,
    ARCHIVE_ENCRYPTION_TAG_BYTES,
    ArchiveEncryptionError,
    decrypt_archive_chunk,
    encrypt_archive_chunks,
    encrypted_size,
    encrypted_span_for_plaintext_range,
    parse_encryption_header,
    plaintext_chunk_size,
)

KEY = bytes(range(32))
CONTEXT = "front/2026/09/04/12/archive.ts.enc"


def _encrypt(payload: bytes, *, chunk_size: int = 64 * 1024) -> bytes:
    source = (payload[:17], payload[17:70001], payload[70001:])
    return b"".join(
        encrypt_archive_chunks(
            source,
            plaintext_size=len(payload),
            chunk_size=chunk_size,
            key=KEY,
            context=CONTEXT,
            nonce_base=b"\x11" * 12,
        )
    )


def _decrypt(payload: bytes) -> bytes:
    header = parse_encryption_header(payload[:ARCHIVE_ENCRYPTION_HEADER_BYTES])
    plaintext = bytearray()
    offset = ARCHIVE_ENCRYPTION_HEADER_BYTES
    for index in range(header.chunk_count):
        size = plaintext_chunk_size(header, index) + ARCHIVE_ENCRYPTION_TAG_BYTES
        plaintext.extend(
            decrypt_archive_chunk(
                payload[offset : offset + size],
                header=header,
                index=index,
                key=KEY,
                context=CONTEXT,
            )
        )
        offset += size
    assert offset == len(payload)
    return bytes(plaintext)


def test_chunked_aes_gcm_round_trip_has_bounded_overhead() -> None:
    plaintext = bytes(range(251)) * 700
    ciphertext = _encrypt(plaintext)

    assert _decrypt(ciphertext) == plaintext
    assert ciphertext[ARCHIVE_ENCRYPTION_HEADER_BYTES:] != plaintext
    assert len(ciphertext) == encrypted_size(len(plaintext), 64 * 1024)


def test_each_chunk_authenticates_content_and_object_path() -> None:
    plaintext = b"private camera frame" * 5000
    ciphertext = bytearray(_encrypt(plaintext))
    ciphertext[ARCHIVE_ENCRYPTION_HEADER_BYTES + 100] ^= 1

    with pytest.raises(ArchiveEncryptionError, match="authentication failed"):
        _decrypt(bytes(ciphertext))

    valid = _encrypt(plaintext)
    header = parse_encryption_header(valid[:ARCHIVE_ENCRYPTION_HEADER_BYTES])
    first_size = plaintext_chunk_size(header, 0) + ARCHIVE_ENCRYPTION_TAG_BYTES
    with pytest.raises(ArchiveEncryptionError, match="authentication failed"):
        decrypt_archive_chunk(
            valid[ARCHIVE_ENCRYPTION_HEADER_BYTES : ARCHIVE_ENCRYPTION_HEADER_BYTES + first_size],
            header=header,
            index=0,
            key=KEY,
            context="another/path.ts.enc",
        )


def test_plaintext_range_maps_to_only_required_encrypted_chunks() -> None:
    plaintext = b"x" * 150_000
    ciphertext = _encrypt(plaintext)
    header = parse_encryption_header(ciphertext[:ARCHIVE_ENCRYPTION_HEADER_BYTES])

    first, last, stored_start, stored_end = encrypted_span_for_plaintext_range(
        header, 65_000, 130_000
    )

    assert (first, last) == (0, 1)
    assert stored_start == ARCHIVE_ENCRYPTION_HEADER_BYTES
    assert stored_end + 1 == (
        ARCHIVE_ENCRYPTION_HEADER_BYTES + 2 * (header.chunk_size + ARCHIVE_ENCRYPTION_TAG_BYTES)
    )
