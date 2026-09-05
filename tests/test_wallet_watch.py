from __future__ import annotations

import pytest

from coinfinder.chains import BASE
from coinfinder.ingest.wallet_watch import blocks_for_minutes, chunk, next_window


def test_chunk_splits_evenly_and_keeps_remainder():
    assert chunk(list("abcde"), 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert chunk([], 3) == []


@pytest.mark.parametrize(
    ("last_done", "head", "expected"),
    [
        (100, 200, (101, 188)),  # confirmations withhold the last 12 blocks
        (188, 200, None),  # caught up to the safe head
        (190, 200, None),  # already past the safe head
        (0, 5, None),  # head still inside the confirmation buffer
    ],
)
def test_next_window_respects_confirmations(last_done, head, expected):
    assert next_window(last_done=last_done, head=head, confirmations=12, max_span=500) == expected


def test_next_window_caps_span():
    assert next_window(last_done=0, head=10_000, confirmations=12, max_span=100) == (1, 100)


def test_blocks_for_minutes_uses_chain_block_time():
    # Base produces a block every 2s, so 3 hours is 5400 blocks.
    assert blocks_for_minutes(BASE, 180) == 5400
    assert blocks_for_minutes(BASE, 0) == 1
