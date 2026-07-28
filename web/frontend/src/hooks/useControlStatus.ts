import { useCallback, useEffect, useRef, useState } from "react";
import { controlApi, RetryableControlError, type ActiveGeneration, type ControlHealth, type ControlStatus } from "../api/control";

export function authorityNextVersion(status: ControlStatus | null): number | null {
  if (!status) return null;
  // Only the explicit pre-reset state owns the high-water projection.  During
  // recovery or an unavailable authority read, claiming a target version
  // would turn an error fallback into version authority.
  if (!status.epoch_initialized) {
    return status.epoch_state === "reset_required"
      ? status.version_authority_high_water + 1
      : null;
  }
  return status.active_generation?.next_v ?? status.next_v;
}

function generationIdentity(value: ActiveGeneration | null | undefined): string {
  if (!value) return "none";
  return [
    value.next_v,
    value.generation_ordinal,
    value.canonical_version,
    value.canonical_bot_name,
    value.canonical_tag,
    value.source_v ?? "none",
    value.parent2_v ?? "none",
    value.stage,
    value.run_id,
    value.workflow_run_id ?? "none",
    value.checkpoint_revision,
  ].join(":");
}

function assertMatchingObservation(status: ControlStatus, health: ControlHealth): void {
  if (
    !status
    || !health
    || !health.status
    || !health.task
    || !health.daemon
    || !health.pipeline
    || !Array.isArray(health.issues)
    || !["healthy", "degraded", "stopped"].includes(health.overall)
  ) {
    throw new Error("control health snapshot is structurally incomplete");
  }
  const healthStatus = health.status;
  const digestPattern = /^[0-9a-f]{64}$/;
  const stabilityVerification = status.stability_observation?.verification;
  const stabilityAuthority = stabilityVerification?.authority;
  if (
    healthStatus?.evaluation_epoch !== "national_tcp_policy_v1"
    || status.evaluation_epoch !== "national_tcp_policy_v1"
    || health.running !== status.running
    || healthStatus.epoch_state !== status.epoch_state
    || healthStatus.epoch_initialized !== status.epoch_initialized
    || healthStatus.version_authority_high_water !== status.version_authority_high_water
    || healthStatus.reset_receipt_valid !== status.reset_receipt_valid
    || healthStatus.reset_receipt_digest !== status.reset_receipt_digest
    || healthStatus.stream_authority_digest !== status.stream_authority_digest
    || healthStatus.runtime_reconciliation_claimed !== status.runtime_reconciliation_claimed
    || healthStatus.runtime_reconciliation_kind !== status.runtime_reconciliation_kind
    || healthStatus.runtime_reconciliation_claim_digest !== status.runtime_reconciliation_claim_digest
    || healthStatus.runtime_reconciliation_claim_valid !== status.runtime_reconciliation_claim_valid
    || JSON.stringify(healthStatus.runtime_reconciliation_claim_issues)
      !== JSON.stringify(status.runtime_reconciliation_claim_issues)
    || healthStatus.publication_recovery_ready !== status.publication_recovery_ready
    || JSON.stringify(healthStatus.unpaired_completion_versions)
      !== JSON.stringify(status.unpaired_completion_versions)
    || JSON.stringify(healthStatus.unpaired_high_water_versions)
      !== JSON.stringify(status.unpaired_high_water_versions)
    || JSON.stringify(healthStatus.strict_published_bot_identities)
      !== JSON.stringify(status.strict_published_bot_identities)
    || generationIdentity(health.active_generation) !== generationIdentity(status.active_generation)
    || generationIdentity(healthStatus.active_generation) !== generationIdentity(status.active_generation)
    || !digestPattern.test(status.stability_observation_digest)
    || !digestPattern.test(healthStatus.stability_observation_digest)
    || healthStatus.stability_observation_digest !== status.stability_observation_digest
    || healthStatus.post_publication_handoff?.projection_digest
      !== status.post_publication_handoff?.projection_digest
  ) {
    throw new Error("control status/health identity changed during observation");
  }
  if (
    stabilityVerification?.state === "fresh"
    && (
      !stabilityAuthority
      || stabilityAuthority.evaluation_epoch !== status.evaluation_epoch
      || stabilityAuthority.epoch_stream_authority_digest
        !== status.stream_authority_digest
      || !/^[0-9a-f]{40}$/.test(stabilityAuthority.repository_head)
      || !stabilityAuthority.repository_branch
      || stabilityAuthority.repository_branch === "HEAD"
    )
  ) {
    throw new Error("stability observation belongs to a different epoch or HEAD");
  }
  const route = health.pipeline.route;
  const generation = status.active_generation;
  const handoff = status.post_publication_handoff;
  if (
    handoff.status !== "none"
    && (
      health.pipeline.authority !== "post_publication_handoff_journal"
      || health.pipeline.handoff_projection_digest !== handoff.projection_digest
      || health.pipeline.handoff_identity_digest !== handoff.identity_digest
      || health.pipeline.handoff_owner_scope !== handoff.owner_scope
      || health.pipeline.stage !== "post_publication_handoff"
    )
  ) {
    throw new Error("control pipeline handoff belongs to a different journal revision");
  }
  if (route && generation && (
    health.pipeline.next_v !== generation.next_v
    || health.pipeline.source_v !== generation.source_v
    || health.pipeline.route?.parent2_v !== generation.parent2_v
    || health.pipeline.stage !== generation.stage
    || health.pipeline.run_id !== generation.run_id
    || health.pipeline.workflow_run_id !== generation.workflow_run_id
    || health.pipeline.checkpoint_revision !== generation.checkpoint_revision
  )) {
    throw new Error("control pipeline route belongs to a different checkpoint revision");
  }
  if (!Number.isFinite(health.checked_at)) {
    throw new Error("control health snapshot has no authoritative observation time");
  }
}

export function useControlStatus(pollMs = 5_000) {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [health, setHealth] = useState<ControlHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<Promise<void> | null>(null);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    if (inFlight.current) return inFlight.current;
    const request = (async () => {
      try {
        // Health already embeds the exact status snapshot used to derive task,
        // daemon, pipeline and handoff health.  Fetching /status concurrently
        // creates a false mismatch whenever an Archivist step increments its
        // journal revision between the two independent requests.
        const nextHealth = await controlApi.health();
        const nextStatus = nextHealth.status;
        assertMatchingObservation(nextStatus, nextHealth);
        if (!mounted.current) return;
        setStatus(nextStatus);
        setHealth(nextHealth);
        setError(null);
      } catch (err) {
        if (!mounted.current) return;
        // A retryable observer 503 (projection refreshing during active
        // generation) must NOT wipe the dashboard. Keep the previous good
        // status/health and set a transient "refreshing" error that the UI
        // can show as a neutral state instead of the scary red banner.
        if (err instanceof RetryableControlError) {
          setError(err.message);
          return;
        }
        // Fail closed for genuine non-retryable authority errors: no page may
        // retain a formerly healthy task, route, mutation permission, or
        // epoch-bound evidence after either half of the paired observation
        // becomes unavailable or changes identity.
        setStatus(null);
        setHealth(null);
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (mounted.current) setLoading(false);
      }
    })();
    inFlight.current = request;
    try {
      await request;
    } finally {
      if (inFlight.current === request) inFlight.current = null;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    let stopped = false;
    let timer: number | null = null;
    const tick = async () => {
      await refresh();
      if (!stopped && pollMs > 0) timer = window.setTimeout(tick, pollMs);
    };
    void tick();
    return () => {
      stopped = true;
      mounted.current = false;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [pollMs, refresh]);

  return { status, health, loading, error, refresh };
}
