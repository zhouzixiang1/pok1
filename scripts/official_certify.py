#!/usr/bin/env python3
"""Manage signed official-full-v5 certification for strict policy bots.

Formal publication requires five 70-hand self-play rounds plus three 70-hand
eligible-opponent rounds. The EXE verdict is compliance-only and never supplies
Glicko/H2H strength evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "web" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

from official_certification import (  # noqa: E402
    build_spec,
    select_official_opponent,
    status_payload,
)
from bot_namespace import (  # noqa: E402
    ARCHIVED_VERSION_HIGH_WATER,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
)
from official_bootstrap import (  # noqa: E402
    DEFAULT_BOOTSTRAP_CONTROL_ID,
    authorize_operator_bootstrap_selection,
    select_first_strict_control,
)
from official_certification_job import job_snapshot, reconcile_jobs, start_or_poll_job  # noqa: E402
from official_certificate_signing import signing_environment_report  # noqa: E402
from official_platform_harness import (  # noqa: E402
    build_formal_quality_admission,
    check_environment,
)
from official_verdict_ledger import (  # noqa: E402
    initialize_verdict_ledger,
    ledger_head_path,
    ledger_integrity,
    ledger_path,
)


def _ledger_report() -> dict:
    path = ledger_path()
    try:
        integrity = ledger_integrity(fresh=True)
    except Exception as exc:
        integrity = {
            "valid": False,
            "issues": [
                f"official_verdict_ledger_validation_error:{type(exc).__name__}:{str(exc)[:240]}"
            ],
            "entry_count": 0,
            "head": None,
        }
    return {
        **integrity,
        "path": str(path),
        "head_path": str(ledger_head_path(path)),
        "init_command": "python3 scripts/official_certify.py init-ledger",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    mode_help = {
        "smoke": "Start or poll a durable short official quality-gate smoke job.",
        "compliance": "Start or poll a durable short official protocol-compliance job.",
        "full": "Start or poll a durable manual 5+3, 70-hand official certification job.",
    }
    for name in ("smoke", "compliance", "full"):
        p = sub.add_parser(name, help=mode_help[name])
        p.add_argument(
            "candidate",
            help=(
                "Strict national_tcp_policy_v1 bot directory. The low-level "
                "diagnostic harness, not formal full mode, accepts standalone scripts."
            ),
        )
        p.add_argument(
            "--opponent",
            default=None,
            help="Preferred eligible opponent. Invalid preferences never bypass policy.",
        )
        p.add_argument("--force", action="store_true", help="Ignore a valid certification cache entry.")
        p.add_argument(
            "--wait-if-busy",
            action="store_true",
            help="Poll the durable job until it reaches a terminal state.",
        )
        p.add_argument(
            "--published",
            action="store_true",
            help=(
                "Certify an already-published strict bot whose pipeline "
                "checkpoint has been cleared (full mode only). Proves the "
                "candidate bytes equal the published tag bytes instead of the "
                "checkpoint-owned quality/capability/probe admission."
            ),
        )
    bootstrap = sub.add_parser(
        "bootstrap-first-strict",
        help=(
            "Explicit one-time 5+3, 70-hand certification of v143 against "
            "the current system-owned first-strict typed-policy control."
        ),
    )
    bootstrap.add_argument("candidate", help="The in-flight bots/national_v143 directory.")
    bootstrap.add_argument(
        "--control-id",
        default=DEFAULT_BOOTSTRAP_CONTROL_ID,
        help="Repository-owned current control id; archived bots are never selected.",
    )
    bootstrap.add_argument(
        "--acknowledge-one-time-first-strict-control",
        action="store_true",
        help="Required acknowledgement that one successful signed run consumes this control authorization.",
    )
    bootstrap.add_argument("--force", action="store_true", help="Retry a terminal durable job.")
    bootstrap.add_argument(
        "--wait-if-busy",
        action="store_true",
        help="Poll the durable job until it reaches a terminal state.",
    )
    finalize = sub.add_parser(
        "finalize-first-strict",
        help=(
            "Publish the already-certified parked v143 through the normal "
            "content-bound commit_bot transaction."
        ),
    )
    finalize.add_argument(
        "--acknowledge-publish-first-strict",
        action="store_true",
        help="Required acknowledgement that the command commits, tags, and pushes v143.",
    )
    p = sub.add_parser("status", help="Read certification status for a bot.")
    p.add_argument(
        "candidate", help="Strict national_tcp_policy_v1 bot directory/subject."
    )
    pub = sub.add_parser(
        "publish-certified",
        help=(
            "Publish a completed full certificate for an already-certified bot: "
            "writes official_certificates/<bot>.json, re-annotates the completion "
            "tag with official-* metadata, and creates the national-cloud-"
            "certified-v<N> tag. Used after `full --published` to admit a staging "
            "bot into the rating pool."
        ),
    )
    pub.add_argument(
        "candidate", help="Strict national_tcp_policy_v1 bot directory/subject."
    )
    pub.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Without this flag the command is a dry-run that reports what it "
            "would do. With it, the cert file is committed and the tags are "
            "created/updated."
        ),
    )
    pub.add_argument(
        "--acknowledge-reannotate-completion-tag",
        action="store_true",
        help=(
            "Required acknowledgement that this command re-annotates the "
            "existing completion tag (national-cloud-bot-v<N>) in place, which "
            "is an explicit operator action outside the create-only publication "
            "transaction."
        ),
    )
    sub.add_parser(
        "jobs-status",
        help="Show durable official certification jobs.",
    )
    sub.add_parser(
        "doctor",
        help="Check official EXE, certificate signing, and verdict-ledger prerequisites.",
    )
    sub.add_parser(
        "init-ledger",
        help="Explicitly create the signed verdict-ledger genesis (idempotent when valid).",
    )
    p = sub.add_parser(
        "reconcile-jobs",
        help="Reconcile durable official certification jobs.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Maximum durable jobs to reconcile in this run.",
    )
    return parser.parse_args(argv)


def _publish_certified(args) -> int:
    """Publish a completed full certificate for an already-certified bot.

    Closes the two-tier gap: ``full --published`` signs the certificate + writes
    the verdict ledger, but does NOT publish the certificate to the repo nor
    annotate the completion tag.  This command:
      1. Calls ``publish_certificate_attestation`` to write
         ``official_certificates/<bot>.json`` (the published attestation wrapper).
      2. Commits that file so ``git show <tag>:official_certificates/<bot>.json``
         resolves (required by ``_validate_published_attestation_at_tag``).
      3. Re-annotates the completion tag (``national-cloud-bot-v<N>``) in place
         with ``official-certificate`` / ``official-candidate-hash`` /
         ``official-policy`` metadata (the keys ``certificate_validation`` reads).
      4. Creates the ``national-cloud-certified-v<N>`` tag at the same commit
         for tier classification (``resolve_version_namespace_authority``).

    After this, ``resolve_national_bot_spec(..., ROLE_RATING_POOL).eligible``
    becomes True and the rating daemon admits the bot.

    Without ``--execute`` it is a dry-run.  ``--acknowledge-reannotate-completion-
    tag`` is required because re-annotating an existing completion tag is an
    explicit operator action outside the create-only publication transaction.
    """
    import subprocess

    from bot_artifact import hash_path, published_bot_identity
    from bot_namespace import bot_tag, bot_name, certified_tag, parse_bot_version
    from official_certification import (
        FULL_POLICY_ID,
        published_certificate_path,
        read_status,
    )
    from official_certification_authority import (
        official_full_certified,
        publish_certificate_attestation,
    )

    candidate_path = Path(args.candidate).expanduser().resolve()
    try:
        version = parse_bot_version(candidate_path.name)
    except (TypeError, ValueError):
        version = None
    if version is None:
        print(json.dumps({
            "status": "publish-certified-blocked",
            "reason": "candidate_not_a_strict_bot",
            "candidate": str(candidate_path),
        }, ensure_ascii=False, indent=2))
        return 2

    # The bot must already carry a completed full certificate (from
    # `full --published`).  Check without require_published (the publication
    # gap is exactly what we are about to close).
    status = read_status(candidate_path)
    if not official_full_certified(status, candidate_path, require_published=False):
        print(json.dumps({
            "status": "publish-certified-blocked",
            "reason": "bot_not_full_certified",
            "candidate": str(candidate_path),
            "status_label": status.get("status"),
            "mode": status.get("mode"),
        }, ensure_ascii=False, indent=2))
        return 2

    certificate_digest = str(status.get("certificate_digest") or "")
    candidate_hash = hash_path(candidate_path)
    published = published_bot_identity(str(candidate_path))
    completion_tag = bot_tag(version)
    cert_tag = certified_tag(version)
    cert_rel = published_certificate_path(candidate_path).relative_to(ROOT).as_posix()

    plan = {
        "status": "publish-certified-plan",
        "candidate": str(candidate_path),
        "version": version,
        "bot": candidate_path.name,
        "completion_tag": completion_tag,
        "certified_tag": cert_tag,
        "certificate_digest": certificate_digest,
        "candidate_hash": candidate_hash,
        "policy_id": FULL_POLICY_ID,
        "published_attestation_path": cert_rel,
        "completion_tag_metadata_to_write": {
            "official-certificate": certificate_digest,
            "official-candidate-hash": candidate_hash,
            "official-policy": FULL_POLICY_ID,
        },
        "published_identity_ok": bool(published.get("published")),
        "execute": bool(args.execute),
    }
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    # Execute.
    if not args.acknowledge_reannotate_completion_tag:
        print(json.dumps({
            "status": "publish-certified-blocked",
            "reason": "acknowledge_reannotate_completion_tag_required",
            "command": (
                "python scripts/official_certify.py publish-certified "
                f"{args.candidate} --execute "
                "--acknowledge-reannotate-completion-tag"
            ),
        }, ensure_ascii=False, indent=2))
        return 2

    # 1. Write the published attestation wrapper.
    publish_result = publish_certificate_attestation(status, candidate_path)
    if not publish_result.get("ok"):
        print(json.dumps({
            "status": "publish-certified-failed",
            "reason": "publish_certificate_attestation_failed",
            "publish_result": publish_result,
        }, ensure_ascii=False, indent=2))
        return 2

    cert_abs = ROOT / cert_rel
    # 2. Commit the cert file so it is reachable at the tagged commit.
    subprocess.run(["git", "add", "--", cert_rel], cwd=str(ROOT), check=True)
    commit_msg = f"cert(publish): official certificate for {candidate_path.name}\n\nofficial-certificate: {certificate_digest}\nofficial-candidate-hash: {candidate_hash}\nofficial-policy: {FULL_POLICY_ID}"
    commit_rc = subprocess.run(
        ["git", "commit", "-m", commit_msg, "--", cert_rel],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    # A no-op commit (file already committed) is fine.
    if commit_rc.returncode not in (0,):
        print(json.dumps({
            "status": "publish-certified-failed",
            "reason": "git_commit_failed",
            "stdout": commit_rc.stdout[:500],
            "stderr": commit_rc.stderr[:500],
        }, ensure_ascii=False, indent=2))
        return 2

    # 3. Re-annotate the completion tag with official-* metadata (force update).
    tag_msg = (
        f"{candidate_path.name}: official-certified\n\n"
        f"official-certificate: {certificate_digest}\n"
        f"official-candidate-hash: {candidate_hash}\n"
        f"official-policy: {FULL_POLICY_ID}\n"
    )
    tag_rc = subprocess.run(
        ["git", "tag", "-a", "-f", completion_tag, "-m", tag_msg, "HEAD"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if tag_rc.returncode != 0:
        print(json.dumps({
            "status": "publish-certified-failed",
            "reason": "completion_tag_reannotate_failed",
            "stderr": tag_rc.stderr[:500],
        }, ensure_ascii=False, indent=2))
        return 2

    # 4. Create the certified tag at the same commit (tier classification).
    cert_tag_msg = (
        f"{candidate_path.name}: certified-tier\n\n"
        f"certified-version: {version}\n"
        f"publication-tier: certified\n"
    )
    subprocess.run(
        ["git", "tag", "-a", "-f", cert_tag, "-m", cert_tag_msg, completion_tag],
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )

    print(json.dumps({
        "status": "publish-certified-complete",
        "candidate": str(candidate_path),
        "version": version,
        "completion_tag": completion_tag,
        "certified_tag": cert_tag,
        "certificate_digest": certificate_digest,
        "published_attestation": cert_rel,
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cmd in {
        "smoke",
        "compliance",
        "full",
        "bootstrap-first-strict",
        "finalize-first-strict",
        "init-ledger",
        "reconcile-jobs",
    }:
        try:
            from epoch_authority import require_policy_epoch_initialized

            require_policy_epoch_initialized(f"official_certify.{args.cmd}")
        except Exception as exc:
            print(json.dumps({
                "status": "policy-epoch-mutation-blocked",
                "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
                "command": getattr(
                    getattr(exc, "state", {}),
                    "get",
                    lambda *_a, **_k: None,
                )("operator_command"),
            }, ensure_ascii=False, indent=2))
            return 2
    if args.cmd == "finalize-first-strict":
        if not args.acknowledge_publish_first_strict:
            print(json.dumps({
                "status": "publication-acknowledgement-required",
                "reason": "acknowledge_publish_first_strict_required",
                "command": (
                    "python scripts/official_certify.py finalize-first-strict "
                    "--acknowledge-publish-first-strict"
                ),
            }, ensure_ascii=False, indent=2))
            return 2
        if ROOT.name != ".evolution_pok":
            print(json.dumps({
                "status": "publication-preflight-blocked",
                "reason": "finalize_requires_autonomous_evolution_runtime_checkout",
                "checkout": str(ROOT),
            }, ensure_ascii=False, indent=2))
            return 2

        from evolution_infra import git_publish_status, read_pipeline_checkpoint
        from national_runtime_authority import strict_published_bot_names

        published = list(strict_published_bot_names())
        if bot_name(FIRST_STRICT_POLICY_VERSION) in published:
            print(json.dumps({
                "status": "already-published",
                "committed": True,
                "version": FIRST_STRICT_POLICY_VERSION,
                "bot": bot_name(FIRST_STRICT_POLICY_VERSION),
            }, ensure_ascii=False, indent=2))
            return 0
        checkpoint = read_pipeline_checkpoint() or {}
        if (
            checkpoint.get("next_v") != FIRST_STRICT_POLICY_VERSION
            or checkpoint.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
            or checkpoint.get("stage") not in {
                "official_bootstrap_required",
                "verified",
                "publishing",
            }
        ):
            print(json.dumps({
                "status": "publication-preflight-blocked",
                "reason": "first_strict_checkpoint_not_finalizable",
                "checkpoint": {
                    "next_v": checkpoint.get("next_v"),
                    "source_v": checkpoint.get("source_v"),
                    "stage": checkpoint.get("stage"),
                    "workflow_run_id": checkpoint.get("workflow_run_id"),
                },
            }, ensure_ascii=False, indent=2))
            return 2
        publish_state = git_publish_status()
        if not publish_state.get("ok"):
            print(json.dumps({
                "status": "publication-preflight-blocked",
                "reason": "runtime_git_not_synchronized",
                "git": publish_state,
            }, ensure_ascii=False, indent=2))
            return 2

        candidate = ROOT / "bots" / bot_name(FIRST_STRICT_POLICY_VERSION)
        certificate = status_payload(candidate)
        from official_certification import official_full_certified
        from official_bootstrap import (
            validate_completed_operator_bootstrap_authorization,
        )

        if not official_full_certified(certificate, candidate):
            print(json.dumps({
                "status": "publication-preflight-blocked",
                "reason": "first_strict_full_certificate_not_valid",
                "certificate_status": certificate.get("status"),
                "certificate_digest": certificate.get("certificate_digest"),
            }, ensure_ascii=False, indent=2))
            return 2
        authorization = validate_completed_operator_bootstrap_authorization(
            certificate,
            candidate,
            checkpoint=checkpoint,
        )
        if authorization.get("valid") is not True:
            print(json.dumps({
                "status": "publication-preflight-blocked",
                "reason": "first_strict_completed_authorization_invalid",
                "authorization": authorization,
            }, ensure_ascii=False, indent=2))
            return 2

        # The explicit runtime-only command owns remote publication semantics.
        # These are defaults rather than overrides so an operator can still
        # make policy stricter, never weaker through CLI arguments.
        os.environ.setdefault("POK_EVOLUTION_RUNTIME", "1")
        os.environ.setdefault("POK_REQUIRE_EVOLUTION_PUSH", "1")
        os.environ.setdefault("EVOLUTION_GIT_PUSH", "1")
        os.environ["POK_OPERATOR_FIRST_STRICT_FINALIZE"] = str(os.getpid())
        from tool_commit import commit_bot

        plan = checkpoint.get("master_plan") or {}
        strategy = (
            str(plan.get("strategy") or "fresh_policy_bootstrap")
            if isinstance(plan, dict)
            else "fresh_policy_bootstrap"
        )
        raw = asyncio.run(commit_bot.handler({
            "version": FIRST_STRICT_POLICY_VERSION,
            "source_v": ARCHIVED_VERSION_HIGH_WATER,
            "strategy": strategy,
            "review_approved": True,
        }))
        try:
            content = raw.get("content") if isinstance(raw, dict) else None
            first = content[0] if isinstance(content, list) and content else {}
            payload = json.loads(first.get("text") or "{}")
        except Exception as exc:
            payload = {
                "status": "publication-result-unreadable",
                "committed": False,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("committed") is True else 1
    if args.cmd == "status":
        print(json.dumps(status_payload(args.candidate), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "publish-certified":
        return _publish_certified(args)
    if args.cmd == "jobs-status":
        print(json.dumps(job_snapshot(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "init-ledger":
        signing = signing_environment_report()
        if not signing.get("ok"):
            payload = {
                "ok": False,
                "initialized": False,
                "signing": signing,
                "ledger": _ledger_report(),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        try:
            initialized = initialize_verdict_ledger()
        except Exception as exc:
            payload = {
                "ok": False,
                "initialized": False,
                "signing": signing,
                "ledger": _ledger_report(),
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        ledger = _ledger_report()
        payload = {
            "ok": bool(ledger.get("valid")),
            **initialized,
            "signing": signing,
            "ledger": ledger,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    if args.cmd == "doctor":
        platform = check_environment(require_formal_sandbox=True)
        signing = signing_environment_report()
        ledger = _ledger_report()
        payload = {
            "ok": (
                bool(platform.get("ok"))
                and bool(signing.get("ok"))
                and bool(ledger.get("valid"))
            ),
            "platform": platform,
            "signing": signing,
            "ledger": ledger,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    if args.cmd == "reconcile-jobs":
        payload = reconcile_jobs(limit=args.limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not payload.get("errors") else 1

    if (
        args.cmd == "bootstrap-first-strict"
        and not args.acknowledge_one_time_first_strict_control
    ):
        print(json.dumps({
            "status": "bootstrap-acknowledgement-required",
            "reason": "acknowledge_one_time_first_strict_control_required",
            "candidate": args.candidate,
            "control_id": args.control_id,
        }, ensure_ascii=False, indent=2))
        return 2

    if args.cmd in {"full", "bootstrap-first-strict"}:
        ledger = _ledger_report()
        if not ledger.get("valid"):
            print(json.dumps({
                "status": "formal-preflight-blocked",
                "reason": "official_verdict_ledger_unavailable",
                "candidate": args.candidate,
                "ledger": ledger,
            }, ensure_ascii=False, indent=2))
            return 2

    quality_admission = None
    if args.cmd == "full":
        # Normal manual full-v5 certification is not a substitute for the
        # checkpoint-owned native quality path.  Bind its exact candidate
        # bytes and current dynamic capability/probe ledger before selecting
        # an opponent or creating a durable official-EXE job.  The v143-only
        # explicit bootstrap has a distinct control authorization below.
        # ``--published`` substitutes a published-tag proof (candidate bytes ==
        # published tag bytes) for an already-published strict bot whose
        # pipeline checkpoint has been cleared.
        if args.published:
            from official_platform_harness import build_published_quality_admission
            quality_admission_report = build_published_quality_admission(args.candidate)
            admission_reason = "published_admission_invalid"
        else:
            quality_admission_report = build_formal_quality_admission(args.candidate)
            admission_reason = "current_dynamic_quality_capability_probe_ledger_invalid"
        if quality_admission_report.get("valid") is not True:
            print(json.dumps({
                "status": "formal-quality-admission-blocked",
                "reason": admission_reason,
                "candidate": args.candidate,
                "quality_admission": quality_admission_report,
            }, ensure_ascii=False, indent=2))
            return 2
        quality_admission = quality_admission_report["admission"]

    if args.cmd == "bootstrap-first-strict":
        selection = select_first_strict_control(
            args.control_id,
            args.candidate,
        )
    else:
        selection = select_official_opponent(
            args.candidate,
            preferred=args.opponent,
            allow_bootstrap_grandfather=False,
        )
    if not selection.get("selected"):
        print(json.dumps({
            "status": "opponent-selection-blocked",
            "candidate": args.candidate,
            "opponent_selection": selection,
        }, ensure_ascii=False, indent=2))
        return 2
    if args.cmd == "bootstrap-first-strict":
        authorization = authorize_operator_bootstrap_selection(
            selection,
            args.control_id,
            args.candidate,
        )
        if authorization.get("valid") is not True:
            print(json.dumps({
                "status": "bootstrap-authorization-blocked",
                "candidate": args.candidate,
                "control_id": args.control_id,
                "authorization": authorization,
            }, ensure_ascii=False, indent=2))
            return 2
        selection = authorization["selection"]
    spec = build_spec(
        "full" if args.cmd == "bootstrap-first-strict" else args.cmd,
        args.candidate,
        opponent=selection["opponent"]["path"],
        bootstrap_control_id=(
            args.control_id if args.cmd == "bootstrap-first-strict" else None
        ),
        quality_admission=quality_admission,
    )
    payload = start_or_poll_job(
        spec,
        opponent_selection=selection,
        retry_terminal=bool(args.force),
    )
    if args.wait_if_busy:
        while payload.get("pending"):
            time.sleep(5)
            payload = start_or_poll_job(
                spec,
                opponent_selection=selection,
                retry_terminal=False,
            )
    payload["opponent_selection"] = selection
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("pending"):
        return 0
    if payload.get("state") != "completed":
        return 2
    status = payload.get("status") or {}
    return 0 if status.get("status") in {
        "official-smoke-pass",
        "official-compliance-pass",
        "official-certified",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
