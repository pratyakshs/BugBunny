from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from bugbunny.util import atomic_write_text, load_json


def test_atomic_write_syncs_the_parent_directory(tmp_path: Path) -> None:
    # os.replace gives atomic visibility, but the rename is crash-durable
    # only once the containing directory itself is synced; a checkpoint or
    # manifest commit must not silently revert after a power loss.
    target = tmp_path / "checkpoint.json"
    synced_inodes: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        synced_inodes.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    with patch("bugbunny.util.os.fsync", new=recording_fsync):
        atomic_write_text(target, '{"value": 1}\n')

    directory_inode = os.stat(tmp_path).st_ino
    file_inode = os.stat(target).st_ino
    assert directory_inode in synced_inodes
    assert file_inode in synced_inodes
    assert load_json(target) == {"value": 1}
