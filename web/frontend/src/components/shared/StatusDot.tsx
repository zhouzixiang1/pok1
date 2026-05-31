import { cn } from "../../lib/utils";

interface StatusDotProps {
  status: "active" | "idle" | "error";
  size?: "sm" | "md";
  pulse?: boolean;
  className?: string;
}

const colorMap = {
  active: "bg-success-500",
  idle: "bg-gray-400",
  error: "bg-error-500",
};

const sizeMap = {
  sm: "w-1.5 h-1.5",
  md: "w-2 h-2",
};

export function StatusDot({ status, size = "md", pulse, className }: StatusDotProps) {
  return (
    <span className={cn("relative inline-flex", className)}>
      {pulse && (
        <span
          className={cn(
            "absolute inset-0 rounded-full animate-ping opacity-75",
            colorMap[status],
          )}
        />
      )}
      <span className={cn("relative rounded-full", colorMap[status], sizeMap[size])} />
    </span>
  );
}
