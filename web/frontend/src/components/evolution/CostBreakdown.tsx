export interface RoleCost {
  role: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

type CostPolicy = {
  enforcement_mode: "monitor_only" | "operator_hard_limit";
  hard_limit_usd: number | null;
  ledger_errors?: string[];
};

export function CostBreakdown({ costs, grand, gen, policy, onReset }: {
  costs: RoleCost[];
  grand: number;
  gen: number;
  onReset: () => void;
  policy?: CostPolicy | null;
}) {
  if (costs.length === 0 && grand === 0 && !policy) return null;
  const accountingIncomplete = Boolean(policy?.ledger_errors?.length);
  return (
    <div className="p-3">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-semibold uppercase text-gray-500">LLM 成本</h3>
        <button onClick={onReset} className="text-[10px] text-gray-400 hover:text-gray-600 underline">清空显示</button>
      </div>
      <div className="space-y-1">
        {costs.map((c) => (
          <div key={c.role} className="flex justify-between text-xs">
            <span className="text-gray-500 truncate max-w-[90px]">{c.role}</span>
            <span className="text-gray-400 font-mono">{(c.input_tokens + c.output_tokens).toLocaleString()} tokens</span>
            <span className="text-gray-800 dark:text-gray-200 font-mono">${c.cost_usd.toFixed(4)}</span>
          </div>
        ))}
      </div>
      <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-700 flex justify-between text-xs font-medium">
        <span className="text-gray-500">
          {policy?.enforcement_mode === "operator_hard_limit"
            ? `本代（operator 上限 $${(policy.hard_limit_usd ?? 0).toFixed(2)}） / 总计`
            : "本代（仅统计告警） / 总计"}
        </span>
        <span className="text-gray-800 dark:text-gray-200 font-mono">${gen.toFixed(3)} / ${grand.toFixed(3)}</span>
      </div>
      {accountingIncomplete && (
        <div className="mt-1 text-xs text-amber-600 dark:text-amber-400">
          计费不完整/未知，当前金额仅供参考
        </div>
      )}
    </div>
  );
}
