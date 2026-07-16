import json

from worker_mcp.audit_log import AuditLogger, redact


def test_recursive_redaction_and_jsonl(tmp_path):
    payload = redact(
        {
            "Authorization": "Bearer abcdefghijklmnop",
            "nested": {"api_key": "secret-value"},
            "message": "token sk-test_abcdefghijklmnop",
        }
    )
    assert payload["Authorization"] == "<redacted>"
    assert payload["nested"]["api_key"] == "<redacted>"
    assert "abcdefghijklmnop" not in payload["message"]
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path, max_bytes=4096, backup_count=1)
    logger.log("test", auth_token="do-not-log", safe="ok")
    logger.close()
    row = json.loads(path.read_text())
    assert row["auth_token"] == "<redacted>" and row["safe"] == "ok"
