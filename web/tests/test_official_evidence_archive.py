import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from official_evidence_archive import build_evidence_archive, validate_evidence_archive


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("POK_OFFICIAL_EVIDENCE_STORE", str(tmp_path / "store"))


def _suite(path: Path) -> Path:
    (path / "self_play_01" / "thp").mkdir(parents=True)
    (path / "summary.json").write_text('{"passed":true}\n', encoding="utf-8")
    (path / "self_play_01" / "wire_events.jsonl").write_text('{"conn":"A"}\n', encoding="utf-8")
    (path / "self_play_01" / "thp" / "match.txt").write_bytes(b"STATE:1:test;\n")
    return path


def test_archive_is_deterministic_across_checkout_paths(tmp_path):
    first = build_evidence_archive(_suite(tmp_path / "checkout-a" / "suite"))
    second = build_evidence_archive(_suite(tmp_path / "checkout-b" / "suite"))

    assert first["archive_sha256"] == second["archive_sha256"]
    assert first["manifest_digest"] == second["manifest_digest"]
    assert validate_evidence_archive(first)["valid"] is True
    assert validate_evidence_archive(second)["valid"] is True


def test_archive_tampering_fails_closed(tmp_path):
    receipt = build_evidence_archive(_suite(tmp_path / "suite"))
    archive = Path(receipt["path"])
    archive.write_bytes(archive.read_bytes() + b"tamper")

    result = validate_evidence_archive(receipt)

    assert result["valid"] is False
    assert "evidence_archive_sha256_mismatch" in result["issues"]


def test_archive_builder_rejects_symlink(tmp_path):
    suite = _suite(tmp_path / "suite")
    (suite / "link.log").symlink_to(suite / "summary.json")

    with pytest.raises(ValueError, match="symlink"):
        build_evidence_archive(suite)


def test_archive_validator_rejects_path_traversal_member(tmp_path):
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    payload = gzip.compress(tar_buffer.getvalue(), mtime=0)
    digest = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "store" / f"{digest}.tar.gz"
    path.parent.mkdir()
    path.write_bytes(payload)
    receipt = {
        "schema_version": 1,
        "storage": "content-addressed-local",
        "path": str(path),
        "archive_sha256": digest,
        "archive_size_bytes": len(payload),
        "manifest_digest": "0" * 64,
        "file_count": 0,
        "total_bytes": 0,
    }

    result = validate_evidence_archive(receipt)

    assert result["valid"] is False
    assert "evidence_archive_unsafe_member" in result["issues"]


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("archive_size_bytes", "not-an-int", "evidence_archive_receipt_size_invalid"),
        ("file_count", [], "evidence_archive_receipt_file_count_invalid"),
        ("total_bytes", -1, "evidence_archive_receipt_total_bytes_invalid"),
    ],
)
def test_archive_validator_fails_closed_on_malformed_numeric_receipt(
    tmp_path, field, value, issue
):
    receipt = build_evidence_archive(_suite(tmp_path / "suite"))
    receipt[field] = value

    result = validate_evidence_archive(receipt)

    assert result["valid"] is False
    assert issue in result["issues"]


def test_archive_validator_uses_digest_store_not_untrusted_receipt_path(tmp_path):
    receipt = build_evidence_archive(_suite(tmp_path / "suite"))
    receipt["path"] = "/etc/passwd"

    result = validate_evidence_archive(receipt)

    assert result["valid"] is True
    assert result["path"] != "/etc/passwd"


def test_archive_validator_fails_closed_on_hash_io_error(tmp_path, monkeypatch):
    receipt = build_evidence_archive(_suite(tmp_path / "suite"))

    def fail_hash(_path):
        raise OSError("simulated unreadable archive")

    monkeypatch.setattr("official_evidence_archive._sha256_file", fail_hash)

    result = validate_evidence_archive(receipt)

    assert result["valid"] is False
    assert any(issue.startswith("evidence_archive_read_error:OSError") for issue in result["issues"])


def test_archive_binds_official_evidence_artifact_manifest(tmp_path):
    suite = _suite(tmp_path / "suite")
    external = tmp_path / "external.log"
    external.write_text("not archived", encoding="utf-8")
    evidence = {
        "schema_version": 1,
        "rounds": [{
            "artifacts": {
                "platform_log": {
                    "path": str(external),
                    "exists": True,
                    "size_bytes": external.stat().st_size,
                    "sha256": hashlib.sha256(external.read_bytes()).hexdigest(),
                    "archive_path": "external.log",
                }
            }
        }],
    }
    evidence_path = suite / "official_evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    receipt = build_evidence_archive(suite)

    result = validate_evidence_archive(
        receipt,
        expected_evidence_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    )

    assert result["valid"] is False
    assert "evidence_archive_artifact_mismatch:external.log" in result["issues"]
