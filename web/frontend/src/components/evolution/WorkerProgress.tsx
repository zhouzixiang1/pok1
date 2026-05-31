import { StatusDot, CheckIcon, CrossIcon } from "./icons";

export type WorkerStatus = "running" | "done" | "failed";

export interface WorkerInfo {
  id: number;
  role?: string;
  status: WorkerStatus;
}

export function parseWorkerStatus(msg: string): { id: number; role?: string; status: WorkerStatus } | null {
  const startMatch = msg.match(/Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(start|begin|running|launch)/i);
  if (startMatch) return { id: parseInt(startMatch[1]), role: startMatch[2], status: "running" };
  const doneMatch = msg.match(/Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(done|finish|success|complete)/i);
  if (doneMatch) return { id: parseInt(doneMatch[1]), role: doneMatch[2], status: "done" };
  const failMatch = msg.match(/Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(fail|error|timeout)/i);
  if (failMatch) return { id: parseInt(failMatch[1]), role: failMatch[2], status: "failed" };
  return null;
}

export function WorkerProgress({ workers }: { workers: WorkerInfo[] }) {
  if (workers.length === 0) return null;
  return (
    <div className="p-3">
      <h3 className="mb-2 text-xs font-semibold uppercase text-gray-500">Worker</h3>
      <div className="space-y-1">
        {workers.map((w) => (
          <div key={w.id} className="flex items-center gap-2 text-xs">
            <span className={
              w.status === "running" ? "text-blue-light-500 animate-pulse" :
              w.status === "done" ? "text-success-500" : "text-error-500"
            }>
              {w.status === "running" ? <StatusDot className="inline" /> : w.status === "done" ? <CheckIcon className="inline" /> : <CrossIcon className="inline" />}
            </span>
            <span className="text-gray-600 dark:text-gray-300">
              Worker {w.id}{w.role ? ` (${w.role})` : ""}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
