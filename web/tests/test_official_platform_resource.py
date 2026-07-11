from pathlib import Path

from official_platform_resource import (
    acquire_official_platform,
    official_platform_busy,
    try_acquire_official_platform,
)


def test_official_platform_lease_is_exclusive_and_preserves_owner(tmp_path):
    path = tmp_path / "official.lock"
    first = try_acquire_official_platform(path, owner="arena:test")
    assert first is not None
    assert "owner=arena:test" in path.read_text(encoding="utf-8")
    assert official_platform_busy(path) is True
    assert try_acquire_official_platform(path, owner="official:test") is None

    first.release()
    second = acquire_official_platform(
        path,
        owner="official:test",
        timeout=0.1,
        poll_interval=0.01,
    )
    owner_record = path.read_text(encoding="utf-8")
    assert "owner=official:test" in owner_record
    second.release()
    assert official_platform_busy(path) is False
    assert path.read_text(encoding="utf-8") == owner_record
