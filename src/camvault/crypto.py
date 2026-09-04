from __future__ import annotations

import hashlib
import secrets
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ARCHIVE_ENCRYPTION_ALGORITHM = "AES-256-GCM"
ARCHIVE_ENCRYPTION_MAGIC = b"CVAEAD01"
ARCHIVE_ENCRYPTION_TAG_BYTES = 16
_HEADER = struct.Struct(">8sIQ12s")
ARCHIVE_ENCRYPTION_HEADER_BYTES = _HEADER.size
_AAD_DOMAIN = b"CamVault archive encryption v1\0"
_MAX_CHUNKS = 2**32


class ArchiveEncryptionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveEncryptionHeader:
    chunk_size: int
    plaintext_size: int
    nonce_base: bytes

    @property
    def chunk_count(self) -> int:
        if self.plaintext_size == 0:
            return 0
        return (self.plaintext_size + self.chunk_size - 1) // self.chunk_size

    def pack(self) -> bytes:
        return _HEADER.pack(
            ARCHIVE_ENCRYPTION_MAGIC,
            self.chunk_size,
            self.plaintext_size,
            self.nonce_base,
        )


def new_encryption_header(
    plaintext_size: int,
    chunk_size: int,
    *,
    nonce_base: bytes | None = None,
) -> ArchiveEncryptionHeader:
    if plaintext_size < 0:
        raise ArchiveEncryptionError("plaintext size cannot be negative")
    if chunk_size < 64 * 1024 or chunk_size > 8 * 1024 * 1024:
        raise ArchiveEncryptionError("encryption chunk size must be between 64 KiB and 8 MiB")
    chunk_count = (plaintext_size + chunk_size - 1) // chunk_size
    if chunk_count >= _MAX_CHUNKS:
        raise ArchiveEncryptionError("encrypted archive contains too many chunks")
    nonce = secrets.token_bytes(12) if nonce_base is None else nonce_base
    if len(nonce) != 12:
        raise ArchiveEncryptionError("AES-GCM nonce base must be 12 bytes")
    return ArchiveEncryptionHeader(
        chunk_size=chunk_size,
        plaintext_size=plaintext_size,
        nonce_base=nonce,
    )


def parse_encryption_header(payload: bytes) -> ArchiveEncryptionHeader:
    if len(payload) != ARCHIVE_ENCRYPTION_HEADER_BYTES:
        raise ArchiveEncryptionError("encrypted archive header has an invalid size")
    magic, chunk_size, plaintext_size, nonce_base = _HEADER.unpack(payload)
    if magic != ARCHIVE_ENCRYPTION_MAGIC:
        raise ArchiveEncryptionError("encrypted archive header has an invalid signature")
    return new_encryption_header(plaintext_size, chunk_size, nonce_base=nonce_base)


def encrypted_size(plaintext_size: int, chunk_size: int) -> int:
    header = new_encryption_header(plaintext_size, chunk_size, nonce_base=b"\0" * 12)
    return (
        ARCHIVE_ENCRYPTION_HEADER_BYTES
        + plaintext_size
        + header.chunk_count * ARCHIVE_ENCRYPTION_TAG_BYTES
    )


def plaintext_chunk_size(header: ArchiveEncryptionHeader, index: int) -> int:
    if index < 0 or index >= header.chunk_count:
        raise ArchiveEncryptionError("encrypted archive chunk index is out of range")
    return min(header.chunk_size, header.plaintext_size - index * header.chunk_size)


def encrypted_chunk_offset(header: ArchiveEncryptionHeader, index: int) -> int:
    if index < 0 or index >= header.chunk_count:
        raise ArchiveEncryptionError("encrypted archive chunk index is out of range")
    return ARCHIVE_ENCRYPTION_HEADER_BYTES + index * (
        header.chunk_size + ARCHIVE_ENCRYPTION_TAG_BYTES
    )


def encrypted_span_for_plaintext_range(
    header: ArchiveEncryptionHeader,
    start: int,
    end: int,
) -> tuple[int, int, int, int]:
    if start < 0 or end < start or end >= header.plaintext_size:
        raise ArchiveEncryptionError("plaintext byte range is invalid")
    first_chunk = start // header.chunk_size
    last_chunk = end // header.chunk_size
    stored_start = encrypted_chunk_offset(header, first_chunk)
    stored_end = (
        encrypted_chunk_offset(header, last_chunk)
        + plaintext_chunk_size(header, last_chunk)
        + ARCHIVE_ENCRYPTION_TAG_BYTES
        - 1
    )
    return first_chunk, last_chunk, stored_start, stored_end


def _chunk_nonce(header: ArchiveEncryptionHeader, index: int) -> bytes:
    value = (int.from_bytes(header.nonce_base, "big") + index) % (1 << 96)
    return value.to_bytes(12, "big")


def _chunk_aad(
    header: ArchiveEncryptionHeader,
    context: str,
    index: int,
    chunk_size: int,
) -> bytes:
    context_digest = hashlib.sha256(context.encode("utf-8")).digest()
    return _AAD_DOMAIN + header.pack() + context_digest + struct.pack(">II", index, chunk_size)


def encrypt_archive_chunks(
    chunks: Iterable[bytes],
    *,
    plaintext_size: int,
    chunk_size: int,
    key: bytes,
    context: str,
    nonce_base: bytes | None = None,
) -> Iterator[bytes]:
    header = new_encryption_header(plaintext_size, chunk_size, nonce_base=nonce_base)
    cipher = AESGCM(key)
    yield header.pack()

    pending = bytearray()
    consumed = 0
    index = 0
    for source in chunks:
        if not source:
            continue
        consumed += len(source)
        if consumed > plaintext_size:
            raise ArchiveEncryptionError("archive encryption input exceeded its declared size")
        pending.extend(source)
        while len(pending) >= chunk_size:
            plaintext = bytes(pending[:chunk_size])
            del pending[:chunk_size]
            yield cipher.encrypt(
                _chunk_nonce(header, index),
                plaintext,
                _chunk_aad(header, context, index, len(plaintext)),
            )
            index += 1

    if pending:
        plaintext = bytes(pending)
        yield cipher.encrypt(
            _chunk_nonce(header, index),
            plaintext,
            _chunk_aad(header, context, index, len(plaintext)),
        )
        index += 1
    if consumed != plaintext_size:
        raise ArchiveEncryptionError(
            f"archive encryption received {consumed} bytes, expected {plaintext_size}"
        )
    if index != header.chunk_count:
        raise ArchiveEncryptionError("archive encryption produced an invalid chunk count")


def decrypt_archive_chunk(
    payload: bytes,
    *,
    header: ArchiveEncryptionHeader,
    index: int,
    key: bytes,
    context: str,
) -> bytes:
    expected_plaintext_size = plaintext_chunk_size(header, index)
    if len(payload) != expected_plaintext_size + ARCHIVE_ENCRYPTION_TAG_BYTES:
        raise ArchiveEncryptionError("encrypted archive chunk has an invalid size")
    try:
        return AESGCM(key).decrypt(
            _chunk_nonce(header, index),
            payload,
            _chunk_aad(header, context, index, expected_plaintext_size),
        )
    except InvalidTag as exc:
        raise ArchiveEncryptionError(
            "encrypted archive authentication failed; data, key, or path is incorrect"
        ) from exc
