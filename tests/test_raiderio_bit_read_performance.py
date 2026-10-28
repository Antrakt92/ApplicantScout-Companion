"""Packed RaiderIO fields keep bit accuracy without per-bit Python work."""

from __future__ import annotations

import random

import pytest

from applicant_scout import raiderio_local as rio


def reference_read(record: bytes, offset: int, width: int) -> tuple[int, int]:
    if offset + width > len(record) * 8:
        raise ValueError("bit read past record")
    value = 0
    for index in range(width):
        absolute = offset + index
        if record[absolute // 8] & (1 << (absolute % 8)):
            value |= 1 << index
    return value, offset + width


def test_reads_match_reference_across_byte_boundaries():
    random_source = random.Random(7931)
    for size in (0, 1, 2, 5, 13, 64):
        record = random_source.randbytes(size)
        for offset in range(size * 8 + 1):
            for width in (0, 1, 2, 4, 5, 6, 7, 8, 10, 13, 31, 64):
                if offset + width > size * 8:
                    with pytest.raises(ValueError, match="bit read past record"):
                        rio._read_bits(record, offset, width)
                else:
                    assert rio._read_bits(record, offset, width) == reference_read(
                        record, offset, width,
                    )


@pytest.mark.parametrize("offset,width", [(-8, 5), (-1, 8), (5, -2), (0, 0)])
def test_nonstandard_internal_inputs_keep_existing_behavior(offset, width):
    record = b"\x80\x27\xff"
    assert rio._read_bits(record, offset, width) == reference_read(record, offset, width)


def test_cross_byte_field_uses_one_contiguous_read():
    accesses = []

    class ObservedRecord(bytes):
        def __getitem__(self, key):
            accesses.append(key)
            return super().__getitem__(key)

    record = ObservedRecord(b"\xa5\x73\x12")
    assert rio._read_bits(record, 7, 13) == reference_read(bytes(record), 7, 13)
    assert accesses == [slice(0, 3)]


def test_complete_profiles_and_raids_match_reference(monkeypatch):
    record = bytes((index * 37 + 11) % 256 for index in range(64))
    meta = rio._ProviderMeta(
        64, tuple(range(1, 17)), (2, 4, 7, 10, 12),
    )
    dungeons = list(rio.MPLUS_RAIDERIO_DUNGEON_ORDER)
    raids = [rio._RaidInfo("Synthetic raid", 8), rio._RaidInfo("Synthetic lair", 1)]
    profile = rio._decode_profile(record, meta, dungeons)
    progress = rio._decode_raid_progress(record * 2, (1, 2, 3, 4), raids, raids)
    monkeypatch.setattr(rio, "_read_bits", reference_read)
    assert rio._decode_profile(record, meta, dungeons) == profile
    assert rio._decode_raid_progress(record * 2, (1, 2, 3, 4), raids, raids) == progress
