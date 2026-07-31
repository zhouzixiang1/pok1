import json
import subprocess

import pytest

from bot_namespace import (
    EVOLUTION_BRANCH,
    bot_name,
    bot_tag,
    high_water_tag,
)
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(path):
    path.mkdir()
    _git(path, "init", "-b", EVOLUTION_BRANCH)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "baseline")


def _intent(
    repo,
    baseline,
    *,
    strategy="transaction test",
    remote_required=True,
    remote_enabled=True,
    prepublication_strict_bots=(),
    version=None,
    source_v=None,
):
    from bot_artifact import hash_path
    from publication_transaction import (
        build_publication_intent,
        file_sha256,
    )

    if version is None:
        version = STRICT_TARGET_V + 1
    if source_v is None:
        source_v = STRICT_TARGET_V
    candidate = repo / "bots" / bot_name(version)
    certificate = (
        repo / "official_certificates" / f"{bot_name(version)}.json"
    )
    checkpoint = {
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "workflow_run_id": f"generation:{version}:test",
        "checkpoint_revision": 9,
        "stage": "verified",
    }
    payload = json.loads(certificate.read_text(encoding="utf-8"))
    return build_publication_intent(
        checkpoint=checkpoint,
        candidate_artifact_hash=hash_path(candidate),
        certificate_digest=payload["certificate_digest"],
        certificate_policy_id="official-full-v5",
        official_status={"status": "certified", "certificate_digest": "b" * 64},
        certificate_relative_path=f"official_certificates/{bot_name(version)}.json",
        certificate_file_sha256=file_sha256(certificate),
        certificate_attestation_digest=payload["attestation_digest"],
        final_gate_ledger_digest="d" * 64,
        strategy_tag=strategy,
        rating_info="",
        baseline_head=baseline,
        baseline_remote_main=baseline,
        baseline_remote_completion_refs={},
        prepublication_strict_bots=prepublication_strict_bots,
        remote_publication_required=remote_required,
        remote_publication_enabled=remote_enabled,
    )


def _write_candidate_and_certificate(repo, *, version=None):
    if version is None:
        version = STRICT_TARGET_V + 1
    candidate = repo / "bots" / bot_name(version)
    candidate.mkdir(parents=True)
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    certificate = (
        repo / "official_certificates" / f"{bot_name(version)}.json"
    )
    certificate.parent.mkdir()
    certificate.write_text(
        json.dumps({
            "certificate_digest": "b" * 64,
            "attestation_digest": "c" * 64,
        }, sort_keys=True),
        encoding="utf-8",
    )
    return candidate, certificate


def _add_bare_origin(repo, bare):
    subprocess.run(
        ["git", "init", "--bare", f"--initial-branch={EVOLUTION_BRANCH}", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", EVOLUTION_BRANCH)


def _prepare_competing_strict_checkout(path, bare):
    competing_v = STRICT_TARGET_V + 2
    subprocess.run(
        ["git", "clone", str(bare), str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(path, "config", "user.email", "competitor@example.com")
    _git(path, "config", "user.name", "Competitor")
    bot = path / "bots" / bot_name(competing_v)
    bot.mkdir(parents=True)
    (bot / "national_bot.py").write_text("# competing strict\n", encoding="utf-8")
    certificate = (
        path / "official_certificates" / f"{bot_name(competing_v)}.json"
    )
    certificate.parent.mkdir(exist_ok=True)
    certificate.write_text("{}\n", encoding="utf-8")
    _git(
        path,
        "add",
        f"bots/{bot_name(competing_v)}",
        f"official_certificates/{bot_name(competing_v)}.json",
    )
    _git(path, "commit", "-m", f"publish competing strict v{competing_v}")
    commit_oid = _git(path, "rev-parse", "HEAD")
    _git(
        path,
        "tag",
        "-a",
        bot_tag(competing_v),
        commit_oid,
        "-m",
        f"National bot v{competing_v} competing strict",
    )
    _git(
        path,
        "tag",
        "-a",
        high_water_tag(competing_v),
        commit_oid,
        "-m",
        f"high water {competing_v}",
    )
    return commit_oid


def _push_competing_strict(path):
    competing_v = STRICT_TARGET_V + 2
    _git(
        path,
        "push",
        "--atomic",
        "origin",
        EVOLUTION_BRANCH,
        bot_tag(competing_v),
        high_water_tag(competing_v),
    )


def _patch_real_publication_runtime(monkeypatch, evolution_infra, repo, candidate):
    import bot_artifact

    results = repo / ".runtime"
    results.mkdir()
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", repo)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: candidate)
    monkeypatch.setattr(
        evolution_infra, "_require_national_epoch_registry_for_commit", lambda: None
    )
    monkeypatch.setattr(evolution_infra, "_git_ensure_main_branch", lambda: None)
    monkeypatch.setattr(
        evolution_infra, "publish_runtime_expected_head", lambda *_a, **_k: ""
    )
    monkeypatch.setattr(bot_artifact, "ROOT", repo)

    def advance_high_water(version):
        tag = high_water_tag(int(version))
        if not _git(repo, "tag", "-l", tag):
            _git(repo, "tag", "-a", tag, "HEAD", "-m", f"high water {version}")
        return tag

    def push_refs(*refs):
        _git(repo, "push", "origin", *refs)
        return True

    monkeypatch.setattr(
        evolution_infra, "_advance_national_epoch_high_water", advance_high_water
    )
    monkeypatch.setattr(evolution_infra, "git_push_refs", push_refs)
    return advance_high_water, push_refs


def test_commit_without_tag_is_recovered_from_exact_frozen_commit(
    tmp_path, monkeypatch
):
    import evolution_infra

    repo = tmp_path / "repo"
    _init_repo(repo)
    baseline = _git(repo, "rev-parse", "HEAD")
    version = STRICT_TARGET_V + 1
    candidate = repo / "bots" / bot_name(version)
    candidate.mkdir(parents=True)
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    certificate = repo / "official_certificates" / f"{bot_name(version)}.json"
    certificate.parent.mkdir()
    certificate.write_text(
        json.dumps({
            "certificate_digest": "b" * 64,
            "attestation_digest": "c" * 64,
        }, sort_keys=True),
        encoding="utf-8",
    )
    intent = _intent(repo, baseline)
    _git(
        repo,
        "add",
        f"bots/{bot_name(version)}",
        f"official_certificates/{bot_name(version)}.json",
    )
    _git(repo, "commit", "-m", intent["commit_message"])
    committed = _git(repo, "rev-parse", "HEAD")

    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", repo)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: candidate)

    assert evolution_infra._resolve_existing_publication_commit(intent) == committed
    assert _git(repo, "tag", "-l", bot_tag(version)) == ""
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"


def test_recovery_rejects_a_second_commit_touching_frozen_paths(
    tmp_path, monkeypatch
):
    import evolution_infra

    repo = tmp_path / "repo"
    _init_repo(repo)
    baseline = _git(repo, "rev-parse", "HEAD")
    version = STRICT_TARGET_V + 1
    candidate = repo / "bots" / bot_name(version)
    candidate.mkdir(parents=True)
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    certificate = repo / "official_certificates" / f"{bot_name(version)}.json"
    certificate.parent.mkdir()
    certificate.write_text(
        json.dumps({
            "certificate_digest": "b" * 64,
            "attestation_digest": "c" * 64,
        }, sort_keys=True),
        encoding="utf-8",
    )
    intent = _intent(repo, baseline)
    _git(
        repo,
        "add",
        f"bots/{bot_name(version)}",
        f"official_certificates/{bot_name(version)}.json",
    )
    _git(repo, "commit", "-m", intent["commit_message"])
    (candidate / "national_bot.py").write_text("# native\n# drift\n", encoding="utf-8")
    _git(repo, "add", f"bots/{bot_name(version)}")
    _git(repo, "commit", "-m", "second mutation")

    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", repo)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: candidate)

    with pytest.raises(RuntimeError, match="multiple commits"):
        evolution_infra._resolve_existing_publication_commit(intent)


def test_remote_publication_requires_exact_objects_peeled_commits_and_main(
    monkeypatch,
):
    import evolution_infra
    from publication_transaction import build_publication_intent

    version = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    completion = bot_tag(version)
    high_water = high_water_tag(version)
    commit = "1" * 40
    tag_object = "2" * 40
    water_object = "3" * 40
    remote_main = "4" * 40
    checkpoint = {
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "workflow_run_id": f"generation:{version}:test",
        "checkpoint_revision": 1,
        "stage": "verified",
    }
    intent = build_publication_intent(
        checkpoint=checkpoint,
        candidate_artifact_hash="a" * 64,
        certificate_digest="b" * 64,
        certificate_policy_id="official-full-v5",
        official_status={"status": "certified"},
        certificate_relative_path=f"official_certificates/{bot_name(version)}.json",
        certificate_file_sha256="c" * 64,
        certificate_attestation_digest="d" * 64,
        final_gate_ledger_digest="e" * 64,
        strategy_tag="test",
        rating_info="",
        baseline_head="5" * 40,
        baseline_remote_main=remote_main,
        baseline_remote_completion_refs={},
        prepublication_strict_bots=[],
        remote_publication_required=True,
        remote_publication_enabled=True,
    )
    local_state = {
        "commit_oid": commit,
        "local_refs": {
            completion: {
                "object_oid": tag_object,
                "peeled_commit_oid": commit,
            },
            high_water: {
                "object_oid": water_object,
                "peeled_commit_oid": commit,
            },
        },
    }
    refs = {
        f"refs/heads/{EVOLUTION_BRANCH}": remote_main,
        f"refs/tags/{completion}": tag_object,
        f"refs/tags/{completion}^{{}}": commit,
        f"refs/tags/{high_water}": water_object,
        f"refs/tags/{high_water}^{{}}": commit,
    }

    def fake_git(*args, **_kwargs):
        if args[:2] == ("ls-remote", "origin"):
            return "\n".join(f"{oid}\t{ref}" for ref, oid in refs.items())
        if args[:3] == ("fetch", "--no-tags", "origin"):
            return ""
        if args == ("rev-parse", f"refs/remotes/origin/{EVOLUTION_BRANCH}"):
            return remote_main
        return ""

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(evolution_infra, "_git_command_succeeds", lambda *_a: True)

    assert evolution_infra.verify_remote_bot_publication(
        intent, local_state=local_state
    )["valid"] is True
    refs.pop(f"refs/tags/{completion}^{{}}")
    invalid = evolution_infra.verify_remote_bot_publication(
        intent, local_state=local_state
    )
    assert invalid["valid"] is False
    assert f"remote_tag_peeled_mismatch:{completion}" in invalid["issues"]


def test_remote_publication_is_proven_against_a_real_bare_origin(
    tmp_path, monkeypatch
):
    import evolution_infra

    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    _init_repo(repo)
    _add_bare_origin(repo, bare)
    baseline = _git(repo, "rev-parse", "HEAD")
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(repo, baseline)
    version = STRICT_TARGET_V + 1
    _git(
        repo,
        "add",
        f"bots/{bot_name(version)}",
        f"official_certificates/{bot_name(version)}.json",
    )
    _git(repo, "commit", "-m", intent["commit_message"])
    commit_oid = _git(repo, "rev-parse", "HEAD")
    _git(
        repo,
        "tag",
        "-a",
        intent["completion_tag"],
        commit_oid,
        "-m",
        intent["tag_message"],
    )
    _git(
        repo,
        "tag",
        "-a",
        intent["high_water_tag"],
        commit_oid,
        "-m",
        "high water",
    )
    _git(
        repo,
        "push",
        "origin",
        EVOLUTION_BRANCH,
        intent["completion_tag"],
        intent["high_water_tag"],
    )
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", repo)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: candidate)
    local_state = {
        "commit_oid": commit_oid,
        "local_refs": {
            name: {
                "object_oid": _git(repo, "rev-parse", f"refs/tags/{name}"),
                "peeled_commit_oid": _git(
                    repo, "rev-parse", f"refs/tags/{name}^{{commit}}"
                ),
            }
            for name in (intent["completion_tag"], intent["high_water_tag"])
        },
    }

    proof = evolution_infra.verify_remote_bot_publication(
        intent, local_state=local_state
    )

    assert proof["valid"] is True
    assert proof["remote_main_oid"] == commit_oid
    assert (
        proof["remote_refs"][f"refs/tags/{intent['completion_tag']}^{{}}"]
        == commit_oid
    )
    assert (
        proof["remote_refs"][f"refs/tags/{intent['high_water_tag']}^{{}}"]
        == commit_oid
    )


def test_existing_completion_tag_at_wrong_commit_is_never_rewritten(
    tmp_path, monkeypatch
):
    import evolution_infra

    repo = tmp_path / "repo"
    _init_repo(repo)
    baseline = _git(repo, "rev-parse", "HEAD")
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(
        repo,
        baseline,
        remote_required=False,
        remote_enabled=False,
    )
    version = STRICT_TARGET_V + 1
    _git(
        repo,
        "add",
        f"bots/{bot_name(version)}",
        f"official_certificates/{bot_name(version)}.json",
    )
    _git(repo, "commit", "-m", intent["commit_message"])
    _git(
        repo,
        "tag",
        "-a",
        intent["completion_tag"],
        baseline,
        "-m",
        intent["tag_message"],
    )
    wrong_object = _git(repo, "rev-parse", f"refs/tags/{intent['completion_tag']}")
    _patch_real_publication_runtime(monkeypatch, evolution_infra, repo, candidate)

    with pytest.raises(RuntimeError, match="different commit"):
        evolution_infra.ensure_bot_git_publication(
            intent,
            official_certificate={
                "certificate_digest": intent["official_certificate_digest"],
                "candidate_hash": intent["candidate_artifact_hash"],
                "policy_id": intent["official_policy_id"],
            },
        )

    assert _git(repo, "rev-parse", f"refs/tags/{intent['completion_tag']}") == wrong_object
    assert (
        _git(repo, "rev-parse", f"refs/tags/{intent['completion_tag']}^{{commit}}")
        == baseline
    )


@pytest.mark.parametrize("crash_phase", ["commit", "high_water", "tag", "push"])
def test_publication_recovery_converges_from_each_git_effect_boundary(
    tmp_path, monkeypatch, crash_phase
):
    import evolution_infra

    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    _init_repo(repo)
    _add_bare_origin(repo, bare)
    baseline = _git(repo, "rev-parse", "HEAD")
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(repo, baseline)
    advance_high_water, push_refs = _patch_real_publication_runtime(
        monkeypatch, evolution_infra, repo, candidate
    )
    original_create = evolution_infra._create_publication_commit
    original_validate_refs = evolution_infra._validate_local_publication_refs
    original_first_strict_push = evolution_infra._push_first_strict_publication

    def crash_after_commit(value):
        commit_oid = original_create(value)
        raise RuntimeError(f"simulated crash after commit {commit_oid}")

    def crash_after_high_water(version):
        advance_high_water(version)
        raise RuntimeError("simulated crash after high water")

    def crash_after_tag(value, commit_oid):
        original_validate_refs(value, commit_oid)
        raise RuntimeError("simulated crash after completion tag")

    def crash_after_push(*args, **kwargs):
        original_first_strict_push(*args, **kwargs)
        raise RuntimeError("simulated crash after push")

    failpoint = {
        "commit": ("_create_publication_commit", crash_after_commit),
        "high_water": ("_advance_national_epoch_high_water", crash_after_high_water),
        "tag": ("_validate_local_publication_refs", crash_after_tag),
        "push": ("_push_first_strict_publication", crash_after_push),
    }[crash_phase]
    monkeypatch.setattr(evolution_infra, *failpoint)
    certificate = {
        "certificate_digest": intent["official_certificate_digest"],
        "candidate_hash": intent["candidate_artifact_hash"],
        "policy_id": intent["official_policy_id"],
    }

    with pytest.raises(RuntimeError, match="simulated crash"):
        evolution_infra.ensure_bot_git_publication(
            intent,
            official_certificate=certificate,
            pre_push_authority=lambda: None,
        )

    # Restore every effect owner, then replay the same immutable intent.
    monkeypatch.setattr(
        evolution_infra, "_create_publication_commit", original_create
    )
    monkeypatch.setattr(
        evolution_infra, "_advance_national_epoch_high_water", advance_high_water
    )
    monkeypatch.setattr(
        evolution_infra, "_validate_local_publication_refs", original_validate_refs
    )
    monkeypatch.setattr(
        evolution_infra, "_push_first_strict_publication", original_first_strict_push
    )
    monkeypatch.setattr(evolution_infra, "git_push_refs", push_refs)
    recovered = evolution_infra.ensure_bot_git_publication(
        intent,
        official_certificate=certificate,
        pre_push_authority=lambda: None,
    )
    remote = evolution_infra.verify_remote_bot_publication(
        intent, local_state=recovered
    )

    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert recovered["push_ok"] is True
    assert remote["valid"] is True
    assert (
        _git(repo, "rev-parse", f"refs/tags/{intent['completion_tag']}^{{commit}}")
        == recovered["commit_oid"]
    )
    assert (
        _git(repo, "rev-parse", f"refs/tags/{intent['high_water_tag']}^{{commit}}")
        == recovered["commit_oid"]
    )


@pytest.mark.parametrize("race_timing", ["before_preflight", "after_authority"])
def test_competing_strict_before_first_strict_push_has_no_candidate_remote_effects(
    tmp_path, monkeypatch, race_timing
):
    import evolution_infra

    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    competitor = tmp_path / "competitor"
    _init_repo(repo)
    _add_bare_origin(repo, bare)
    baseline = _git(repo, "rev-parse", "HEAD")
    competing_commit = _prepare_competing_strict_checkout(competitor, bare)
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(repo, baseline)
    _patch_real_publication_runtime(monkeypatch, evolution_infra, repo, candidate)
    certificate = {
        "certificate_digest": intent["official_certificate_digest"],
        "candidate_hash": intent["candidate_artifact_hash"],
        "policy_id": intent["official_policy_id"],
    }

    # Materialize the candidate commit/high-water/completion refs, then stop at
    # the exact boundary immediately before any remote mutation.
    original_validate_refs = evolution_infra._validate_local_publication_refs

    def stop_after_local_refs(value, commit_oid):
        original_validate_refs(value, commit_oid)
        raise RuntimeError("stop after local refs")

    monkeypatch.setattr(
        evolution_infra, "_validate_local_publication_refs", stop_after_local_refs
    )
    with pytest.raises(RuntimeError, match="stop after local refs"):
        evolution_infra.ensure_bot_git_publication(
            intent,
            official_certificate=certificate,
            pre_push_authority=lambda: None,
        )
    monkeypatch.setattr(
        evolution_infra, "_validate_local_publication_refs", original_validate_refs
    )
    candidate_commit = _git(repo, "rev-parse", "HEAD")
    authority_calls = []

    if race_timing == "before_preflight":
        _push_competing_strict(competitor)

        def authority():
            authority_calls.append(True)

        error = "origin/main changed after intent baseline"
    else:

        def authority():
            authority_calls.append(True)
            _push_competing_strict(competitor)

        error = "atomic lease failed"

    with pytest.raises(RuntimeError, match=error):
        evolution_infra.ensure_bot_git_publication(
            intent,
            official_certificate=certificate,
            pre_push_authority=authority,
        )

    version = STRICT_TARGET_V + 1
    competing_v = STRICT_TARGET_V + 2
    remote = _git(
        repo,
        "ls-remote",
        "origin",
        f"refs/heads/{EVOLUTION_BRANCH}",
        f"refs/tags/{bot_tag(version)}",
        f"refs/tags/{high_water_tag(version)}",
        f"refs/tags/{bot_tag(competing_v)}",
    )
    remote_refs = {
        ref: oid
        for line in remote.splitlines()
        for oid, ref in [line.split("\t", 1)]
    }
    assert remote_refs[f"refs/heads/{EVOLUTION_BRANCH}"] == competing_commit
    assert remote_refs[f"refs/tags/{bot_tag(competing_v)}"]
    assert f"refs/tags/{bot_tag(version)}" not in remote_refs
    assert f"refs/tags/{high_water_tag(version)}" not in remote_refs
    assert _git(repo, "rev-parse", "HEAD") == candidate_commit
    assert len(authority_calls) == (0 if race_timing == "before_preflight" else 1)


def test_first_strict_push_rejects_remote_completion_namespace_drift(
    tmp_path, monkeypatch
):
    import evolution_infra

    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    observer = tmp_path / "observer"
    _init_repo(repo)
    _add_bare_origin(repo, bare)
    baseline = _git(repo, "rev-parse", "HEAD")
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(repo, baseline)
    _patch_real_publication_runtime(monkeypatch, evolution_infra, repo, candidate)
    certificate = {
        "certificate_digest": intent["official_certificate_digest"],
        "candidate_hash": intent["candidate_artifact_hash"],
        "policy_id": intent["official_policy_id"],
    }
    original_validate_refs = evolution_infra._validate_local_publication_refs

    def stop_after_local_refs(value, commit_oid):
        original_validate_refs(value, commit_oid)
        raise RuntimeError("stop after local refs")

    monkeypatch.setattr(
        evolution_infra, "_validate_local_publication_refs", stop_after_local_refs
    )
    with pytest.raises(RuntimeError, match="stop after local refs"):
        evolution_infra.ensure_bot_git_publication(
            intent,
            official_certificate=certificate,
            pre_push_authority=lambda: None,
        )
    monkeypatch.setattr(
        evolution_infra, "_validate_local_publication_refs", original_validate_refs
    )

    subprocess.run(
        ["git", "clone", str(bare), str(observer)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(observer, "config", "user.email", "observer@example.com")
    _git(observer, "config", "user.name", "Observer")
    competing_completion = bot_tag(STRICT_TARGET_V + 8)
    _git(
        observer,
        "tag",
        "-a",
        competing_completion,
        baseline,
        "-m",
        "concurrent completion publication",
    )
    _git(observer, "push", "origin", competing_completion)

    with pytest.raises(RuntimeError, match="remote strict completion refs changed"):
        evolution_infra.ensure_bot_git_publication(
            intent,
            official_certificate=certificate,
            pre_push_authority=lambda: None,
        )

    version = STRICT_TARGET_V + 1
    remote = _git(
        repo,
        "ls-remote",
        "origin",
        f"refs/heads/{EVOLUTION_BRANCH}",
        f"refs/tags/{bot_tag(version)}",
        f"refs/tags/{high_water_tag(version)}",
    )
    assert f"{baseline}\trefs/heads/{EVOLUTION_BRANCH}" in remote
    assert bot_tag(version) not in remote
    assert high_water_tag(version) not in remote


def test_first_strict_atomic_push_leases_main_and_never_forces_tags(monkeypatch):
    import evolution_infra
    from bot_namespace import bot_tag_glob, high_water_tag_glob

    version = STRICT_TARGET_V + 1
    completion = bot_tag(version)
    high_water = high_water_tag(version)
    local_pub_ref = f"refs/heads/{EVOLUTION_BRANCH}"
    baseline = "1" * 40
    commit_oid = "2" * 40
    completion_object = "3" * 40
    high_water_object = "4" * 40
    push_calls = []
    authority_calls = []

    def fake_git(*args, **_kwargs):
        if args == ("rev-parse", local_pub_ref):
            return commit_oid
        if args == ("fetch", "origin", "--prune", "--tags"):
            return ""
        if args == (
            "ls-remote",
            "origin",
            local_pub_ref,
            f"refs/tags/{bot_tag_glob()}",
            f"refs/tags/{high_water}",
        ):
            return f"{baseline}\t{local_pub_ref}"
        if args == ("rev-parse", f"refs/tags/{completion}"):
            return completion_object
        if args == ("rev-parse", f"refs/tags/{high_water}"):
            return high_water_object
        if args[:2] == ("push", "--atomic"):
            push_calls.append(args)
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(
        evolution_infra, "_git_command_succeeds", lambda *_args: True
    )
    assert evolution_infra._push_first_strict_publication(
        {
            "baseline_remote_main": baseline,
            "baseline_remote_completion_refs": {},
            "completion_tag": completion,
            "high_water_tag": high_water,
            "prepublication_strict_bots": [],
        },
        commit_oid,
        {
            completion: {"object_oid": completion_object},
            high_water: {"object_oid": high_water_object},
        },
        pre_push_authority=lambda: authority_calls.append(True),
    ) is True

    assert authority_calls == [True]
    assert len(push_calls) == 1
    push = push_calls[0]
    assert f"--force-with-lease={local_pub_ref}:{baseline}" in push
    assert not any(
        item.startswith("--force-with-lease=refs/tags/") for item in push
    )
    assert (
        f"refs/tags/{completion}:refs/tags/{completion}" in push
    )
    assert (
        f"refs/tags/{high_water}:refs/tags/{high_water}"
        in push
    )
    assert not any(item.startswith("+") for item in push)


def test_already_linearized_publication_survives_later_strict_remote_commit(
    tmp_path, monkeypatch
):
    import evolution_infra

    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    competitor = tmp_path / "competitor"
    _init_repo(repo)
    _add_bare_origin(repo, bare)
    baseline = _git(repo, "rev-parse", "HEAD")
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(repo, baseline)
    _patch_real_publication_runtime(monkeypatch, evolution_infra, repo, candidate)
    certificate = {
        "certificate_digest": intent["official_certificate_digest"],
        "candidate_hash": intent["candidate_artifact_hash"],
        "policy_id": intent["official_policy_id"],
    }
    original_push = evolution_infra._push_first_strict_publication

    def crash_after_remote_linearization(*args, **kwargs):
        original_push(*args, **kwargs)
        raise RuntimeError("crash after remote linearization")

    monkeypatch.setattr(
        evolution_infra,
        "_push_first_strict_publication",
        crash_after_remote_linearization,
    )
    with pytest.raises(RuntimeError, match="crash after remote linearization"):
        evolution_infra.ensure_bot_git_publication(
            intent,
            official_certificate=certificate,
            pre_push_authority=lambda: None,
        )
    monkeypatch.setattr(
        evolution_infra, "_push_first_strict_publication", original_push
    )

    competing_commit = _prepare_competing_strict_checkout(competitor, bare)
    _push_competing_strict(competitor)
    recovered = evolution_infra.ensure_bot_git_publication(
        intent,
        official_certificate=certificate,
        pre_push_authority=lambda: (_ for _ in ()).throw(
            AssertionError("linearized publication must not reopen authority")
        ),
    )
    remote = evolution_infra.verify_remote_bot_publication(
        intent, local_state=recovered
    )

    assert recovered["already_remote"] is True
    assert recovered["push_ok"] is True
    assert remote["valid"] is True
    assert remote["remote_main_oid"] == competing_commit


def test_non_first_publication_keeps_existing_reconcile_push_strategy(
    tmp_path, monkeypatch
):
    import evolution_infra

    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    _init_repo(repo)
    _add_bare_origin(repo, bare)
    baseline = _git(repo, "rev-parse", "HEAD")
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(
        repo,
        baseline,
        prepublication_strict_bots=(bot_name(STRICT_TARGET_V),),
    )
    _patch_real_publication_runtime(monkeypatch, evolution_infra, repo, candidate)
    push_calls = []
    authority_calls = []

    def existing_push(*refs):
        push_calls.append(refs)
        _git(repo, "push", "--atomic", "origin", *refs)
        return True

    monkeypatch.setattr(evolution_infra, "git_push_refs", existing_push)
    result = evolution_infra.ensure_bot_git_publication(
        intent,
        official_certificate={
            "certificate_digest": intent["official_certificate_digest"],
            "candidate_hash": intent["candidate_artifact_hash"],
            "policy_id": intent["official_policy_id"],
        },
        pre_push_authority=lambda: authority_calls.append(True),
    )

    version = STRICT_TARGET_V + 1
    assert result["push_ok"] is True
    assert authority_calls == [True]
    assert push_calls == [
        (EVOLUTION_BRANCH, bot_tag(version), high_water_tag(version))
    ]


def test_local_only_inflight_tag_does_not_activate_or_repair_sentinel(
    tmp_path, monkeypatch
):
    import evolution_infra

    repo = tmp_path / "repo"
    _init_repo(repo)
    baseline = _git(repo, "rev-parse", "HEAD")
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(
        repo,
        baseline,
        remote_required=False,
        remote_enabled=False,
    )
    version = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    checkpoint = {
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "workflow_run_id": f"generation:{version}:test",
        "checkpoint_revision": 10,
        "stage": "publishing",
        "publication_intent": intent,
    }
    monkeypatch.setattr(evolution_infra, "BOTS_DIR", repo / "bots")
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(evolution_infra, "_tagged_bot_versions", lambda: {version})
    monkeypatch.setattr(evolution_infra, "evolution_git_push_required", lambda: False)
    monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())
    monkeypatch.setattr(
        evolution_infra, "active_native_contract_filter_enabled", lambda: False
    )
    monkeypatch.setattr(
        evolution_infra, "is_active_bot_protocol_eligible", lambda _v: True
    )
    monkeypatch.setattr(evolution_infra, "_official_parent_eligible", lambda _p: True)

    assert evolution_infra.get_active_bots() == []
    assert not (candidate / ".completed").exists()

    (candidate / ".completed").write_text(
        f"publication_id={intent['publication_id']}\n", encoding="utf-8"
    )
    assert evolution_infra.get_active_bots() == [bot_name(version)]


@pytest.mark.parametrize("first_failure", ["after_sentinel", "cas_rejected"])
def test_publication_recovery_retries_after_sentinel_before_checkpoint_cas(
    tmp_path, monkeypatch, first_failure
):
    import national_runtime_authority
    import official_certification
    import post_publication_handoff
    import publication_transaction
    import tool_commit

    repo = tmp_path / "repo"
    _init_repo(repo)
    baseline = _git(repo, "rev-parse", "HEAD")
    version = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    candidate, _certificate = _write_candidate_and_certificate(
        repo,
        version=version,
    )
    intent = _intent(
        repo,
        baseline,
        remote_required=False,
        remote_enabled=False,
        version=version,
        source_v=source_v,
    )
    official_status = {
        "status": "certified",
        "policy_id": "official-full-v5",
        "certificate_digest": "b" * 64,
        "certification_identity": {
            "candidate_hash": intent["candidate_artifact_hash"],
        },
    }
    checkpoint = {
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "workflow_run_id": f"generation:{version}:test",
        "checkpoint_revision": 10,
        "stage": "publishing",
        "publication_intent": intent,
        "gate_results": {"official_full": {"status": official_status}},
    }
    clear_calls = []
    ensure_calls = []
    sentinel_calls = []
    handoff_calls = []
    monkeypatch.setenv(
        "POK_ALLOW_LOCAL_ONLY_POST_PUBLICATION_HANDOFF_FOR_TESTS",
        "1",
    )
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _v: candidate)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(
        tool_commit,
        "_existing_local_bot_tag_matches_certificate",
        lambda *_a, **_k: (True, ""),
    )
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_a, **_k: {"missing_gates": [], "failed_gates": []},
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_gate_ledger_digest",
        lambda _ledger: intent["final_gate_ledger_digest"],
    )
    monkeypatch.setattr(
        publication_transaction, "publication_intent_live_errors", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_checkpoint_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda **_k: (bot_name(version),),
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "build_pending_local_publication_proof",
        lambda _path: {
            "bot": bot_name(version),
            "proof_digest": "proof",
        },
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda *_a, **_k: True,
    )

    def ensure(*_a, **_k):
        ensure_calls.append(True)
        return {"commit_oid": "1" * 40, "push_ok": False, "local_refs": {}}

    def clear(**kwargs):
        clear_calls.append(kwargs)
        return first_failure != "cas_rejected" or len(clear_calls) > 1

    original_write_sentinel = tool_commit._write_completed_sentinel_durable

    def write_then_crash(*args, **kwargs):
        sentinel_calls.append(True)
        result = original_write_sentinel(*args, **kwargs)
        if len(sentinel_calls) == 1:
            raise RuntimeError("simulated crash after durable sentinel")
        return result

    monkeypatch.setattr(tool_commit, "ensure_bot_git_publication", ensure)
    monkeypatch.setattr(tool_commit, "evolution_git_push_required", lambda: False)
    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tool_commit, "clear_pipeline_checkpoint", clear)
    def ensure_handoff(**kwargs):
        # Keep the journal itself hermetic here while preserving its security
        # boundary: local-only completion is explicit test mode, bound to the
        # exact publishing checkpoint and proven publication result.
        assert kwargs["version"] == version
        assert kwargs["source_v"] == source_v
        assert kwargs["publishing_checkpoint"] is checkpoint
        assert kwargs["allow_local_only"] is True
        publication = kwargs["publication_result"]
        assert publication["committed"] is True
        assert publication["publication_id"] == intent["publication_id"]
        assert publication["completed_sentinel_written"] is True
        handoff_calls.append(kwargs)
        return {"identity_digest": "d" * 64, "state": "pending"}

    monkeypatch.setattr(
        post_publication_handoff,
        "ensure_post_publication_handoff",
        ensure_handoff,
    )
    if first_failure == "after_sentinel":
        monkeypatch.setattr(
            tool_commit, "_write_completed_sentinel_durable", write_then_crash
        )

    if first_failure == "after_sentinel":
        with pytest.raises(RuntimeError, match="simulated crash after durable sentinel"):
            tool_commit._resume_publication_transaction(version, source_v, checkpoint)
        first = None
    else:
        first = tool_commit._resume_publication_transaction(
            version,
            source_v,
            checkpoint,
        )
    second = tool_commit._resume_publication_transaction(
        version,
        source_v,
        checkpoint,
    )

    if first_failure == "cas_rejected":
        assert first["committed"] is False
        assert first["completed_sentinel_written"] is True
        assert "checkpoint CAS did not clear" in first["error"]
    assert (candidate / ".completed").read_text(encoding="utf-8") == (
        f"publication_id={intent['publication_id']}\n"
    )
    assert second["committed"] is True
    assert second["checkpoint_cleared"] is True
    assert len(ensure_calls) == 2
    assert len(handoff_calls) == (1 if first_failure == "after_sentinel" else 2)
    assert len(clear_calls) == (1 if first_failure == "after_sentinel" else 2)
    assert clear_calls[-1]["expected_checkpoint_stage"] == "publishing"
    assert clear_calls[-1]["expected_checkpoint_revision"] == 10
    assert second["post_publication_handoff_identity_digest"] == "d" * 64


def test_publication_intent_digest_covers_strategy_and_remote_requirement():
    from publication_transaction import (
        build_publication_intent,
        publication_intent_structure_errors,
    )

    version = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    checkpoint = {
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "workflow_run_id": f"generation:{version}:test",
        "checkpoint_revision": 1,
        "stage": "verified",
    }
    intent = build_publication_intent(
        checkpoint=checkpoint,
        candidate_artifact_hash="a" * 64,
        certificate_digest="b" * 64,
        certificate_policy_id="official-full-v5",
        official_status={"status": "certified"},
        certificate_relative_path=f"official_certificates/{bot_name(version)}.json",
        certificate_file_sha256="c" * 64,
        certificate_attestation_digest="d" * 64,
        final_gate_ledger_digest="e" * 64,
        strategy_tag="test",
        rating_info="",
        baseline_head="1" * 40,
        baseline_remote_main="2" * 40,
        baseline_remote_completion_refs={},
        prepublication_strict_bots=[],
        remote_publication_required=True,
        remote_publication_enabled=True,
    )
    assert publication_intent_structure_errors(intent) == []
    remote_ref_drift = dict(intent)
    remote_ref_drift["baseline_remote_completion_refs"] = {
        f"refs/tags/{bot_tag(source_v)}": "3" * 40,
    }
    assert "publication_intent_digest_mismatch" in publication_intent_structure_errors(
        remote_ref_drift
    )
    intent["remote_publication_required"] = False
    assert "publication_intent_digest_mismatch" in publication_intent_structure_errors(
        intent
    )


def test_staging_intent_from_verified_stage_passes_structure_validation():
    """A staging-tier generation publishes from the real precommit-pass stage.

    ``run_precommit_eval`` sets checkpoint ``stage="verified"`` on pass
    (``tool_eval.py``) and the router maps ``verified -> commit_bot`` for both
    tiers; the staging commit branch then builds the intent from that same
    checkpoint. The staging ``origin_checkpoint_stage`` must therefore be
    ``verified`` and must pass structural validation — otherwise every
    staging-tier generation loops forever on ``commit_bot`` with
    ``publication_intent_origin_stage_invalid`` (born-broken in bc668676).
    """
    from publication_transaction import (
        PUBLICATION_INTENT_KIND_STAGING,
        build_staging_publication_intent,
        publication_intent_structure_errors,
    )

    version = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    checkpoint = {
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "workflow_run_id": f"generation:{version}:test",
        "checkpoint_revision": 17,
        "stage": "verified",
    }
    intent = build_staging_publication_intent(
        checkpoint=checkpoint,
        candidate_artifact_hash="a" * 64,
        final_gate_ledger_digest="e" * 64,
        strategy_tag="staging test",
        rating_info="",
        baseline_head="1" * 40,
        baseline_remote_main="2" * 40,
        baseline_remote_completion_refs={},
        prepublication_strict_bots=[],
        remote_publication_required=True,
        remote_publication_enabled=True,
    )
    assert intent["kind"] == PUBLICATION_INTENT_KIND_STAGING
    assert intent["origin_checkpoint_stage"] == "verified"
    # Regression anchor: the staging precommit-pass -> commit path must clear
    # structural validation; demanding phantom stage names blocked it at birth.
    assert publication_intent_structure_errors(intent) == []


@pytest.mark.parametrize("existing_kind", ["wrong_bytes", "symlink", "directory"])
def test_completed_sentinel_rejects_unbound_existing_path(
    tmp_path, existing_kind
):
    import tool_commit

    candidate = tmp_path / "bots" / bot_name(STRICT_TARGET_V + 1)
    candidate.mkdir(parents=True)
    sentinel = candidate / ".completed"
    if existing_kind == "wrong_bytes":
        sentinel.write_text("publication_id=wrong\n", encoding="utf-8")
    elif existing_kind == "symlink":
        target = tmp_path / "sentinel-target"
        target.write_text("publication_id=" + "a" * 64 + "\n", encoding="utf-8")
        sentinel.symlink_to(target)
    else:
        sentinel.mkdir()

    with pytest.raises(RuntimeError, match="completed sentinel"):
        tool_commit._write_completed_sentinel_durable(candidate, "a" * 64)


def test_completed_sentinel_exact_recovery_is_idempotent(tmp_path):
    import tool_commit

    candidate = tmp_path / "bots" / bot_name(STRICT_TARGET_V + 1)
    candidate.mkdir(parents=True)
    publication_id = "a" * 64
    assert tool_commit._write_completed_sentinel_durable(
        candidate, publication_id
    ) is True
    assert tool_commit._write_completed_sentinel_durable(
        candidate, publication_id
    ) is True
    sentinel = candidate / ".completed"
    assert not sentinel.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == (
        f"publication_id={publication_id}\n"
    )


def test_private_index_commit_is_immune_to_post_seal_worktree_drift(
    tmp_path, monkeypatch
):
    import bot_artifact
    import evolution_infra

    repo = tmp_path / "repo"
    _init_repo(repo)
    baseline = _git(repo, "rev-parse", "HEAD")
    candidate, _certificate = _write_candidate_and_certificate(repo)
    intent = _intent(
        repo,
        baseline,
        remote_required=False,
        remote_enabled=False,
    )
    results = repo / ".runtime"
    results.mkdir()
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", repo)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: candidate)
    monkeypatch.setattr(
        evolution_infra, "publish_runtime_expected_head", lambda *_a, **_k: ""
    )
    monkeypatch.setattr(bot_artifact, "ROOT", repo)
    original_commit_object = evolution_infra._publication_commit_object

    def drift_after_tree_is_sealed(tree_oid, parent_oid, message):
        commit_oid = original_commit_object(tree_oid, parent_oid, message)
        (candidate / "national_bot.py").write_text(
            "# drift after immutable tree\n", encoding="utf-8"
        )
        return commit_oid

    monkeypatch.setattr(
        evolution_infra,
        "_publication_commit_object",
        drift_after_tree_is_sealed,
    )

    commit_oid = evolution_infra._create_publication_commit(intent)

    version = STRICT_TARGET_V + 1
    assert _git(repo, "rev-parse", "HEAD") == commit_oid
    assert _git(
        repo, "show", f"{commit_oid}:bots/{bot_name(version)}/national_bot.py"
    ) == (
        "# native"
    )
    assert (candidate / "national_bot.py").read_text(encoding="utf-8") == (
        "# drift after immutable tree\n"
    )


def test_pre_push_authority_reopens_latest_publishing_checkpoint(
    tmp_path, monkeypatch
):
    import official_certification
    import publication_transaction
    import national_runtime_authority
    import tool_commit

    version = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    candidate = tmp_path / "bots" / bot_name(version)
    candidate.mkdir(parents=True)
    intent = {"publication_id": "a" * 64}
    latest = {
        "stage": "publishing",
        "checkpoint_revision": 12,
        "gate_results": {
            "official_full": {"status": {"status": "drifted"}}
        },
    }
    observed = []
    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: latest)
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_checkpoint_errors",
        lambda _intent, checkpoint: observed.append(("checkpoint", checkpoint)) or [],
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "build_pending_local_publication_proof",
        lambda _path: {"proof": "current"},
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda: (bot_name(version),),
    )

    def ledger(_v, _source_v, checkpoint, **_kwargs):
        observed.append(("ledger", checkpoint))
        return {"missing_gates": [], "failed_gates": []}

    monkeypatch.setattr(tool_commit, "validate_commit_gate_ledger", ledger)
    monkeypatch.setattr(
        publication_transaction,
        "publication_gate_ledger_digest",
        lambda _ledger: "d" * 64,
    )

    def live_errors(_intent, **kwargs):
        observed.append(("live", kwargs["checkpoint"]))
        assert kwargs["official_status"] == {"status": "drifted"}
        return ["publication_intent_official_status_drift"]

    monkeypatch.setattr(
        publication_transaction, "publication_intent_live_errors", live_errors
    )
    monkeypatch.setattr(
        tool_commit,
        "_existing_local_bot_tag_matches_certificate",
        lambda *_a, **_k: (True, ""),
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(tool_commit, "evolution_git_push_required", lambda: True)

    with pytest.raises(RuntimeError, match="pre-push publication authority changed"):
        tool_commit._revalidate_publication_authority_before_push(
            version,
            source_v,
            intent=intent,
            bot_dir=candidate,
        )

    assert observed == [
        ("checkpoint", latest),
        ("ledger", latest),
        ("live", latest),
    ]
