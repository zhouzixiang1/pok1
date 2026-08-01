import type { ReactNode } from "react";
import { useControlStatusValue } from "../../context/DataProvider";
import { operatorSituationView } from "../../domain/operatorSituationView";
import { EvolutionPageHeader } from "./EvolutionPageHeader";
import { PhaseAProjectionStrip } from "./PhaseAProjectionStrip";
import { OperatorSituation } from "./OperatorSituation";

interface EvolutionPageScaffoldProps {
  title: string;
  subtitle?: string;
  children: ReactNode;
}

/**
 * Unified scaffold for all evolution pages. Internally consumes the shared
 * useControlStatusValue() (single /health poll) and renders the standard
 * page-header triad: EvolutionPageHeader + PhaseAProjectionStrip +
 * OperatorSituation. Eliminates the 6-8 page duplicate of this triad.
 */
export function EvolutionPageScaffold({
  title,
  subtitle,
  children,
}: EvolutionPageScaffoldProps) {
  const { status, health, loading, error, lastUpdated } = useControlStatusValue();
  const manualRequired = operatorSituationView(status, health)?.manualRequired === true;

  return (
    <>
      <EvolutionPageHeader
        title={title}
        subtitle={subtitle}
        status={status}
        health={health}
        loading={loading}
        error={error}
        lastUpdated={lastUpdated}
        variant="full"
      />
      <PhaseAProjectionStrip status={status} manualRequired={manualRequired} />
      <OperatorSituation status={status} health={health} className="mb-4" />
      {children}
    </>
  );
}
