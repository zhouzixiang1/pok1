import { cn } from "../../lib/utils";

interface SkeletonProps {
  className?: string;
}

function SkeletonBase({ className }: SkeletonProps) {
  return (
    <div
      className={cn(
        "animate-shimmer rounded bg-gray-200 dark:bg-gray-700/50",
        className,
      )}
    />
  );
}

export const Skeleton = Object.assign(SkeletonBase, {
  Line({ className }: { className?: string }) {
    return <SkeletonBase className={cn("h-4 w-full", className)} />;
  },
  Text({ className }: { className?: string }) {
    return <SkeletonBase className={cn("h-3 w-24", className)} />;
  },
  Circle({ className }: { className?: string }) {
    return <SkeletonBase className={cn("h-8 w-8 rounded-full", className)} />;
  },
  Card({ count = 1, className }: { count?: number; className?: string }) {
    return (
      <div className={cn("space-y-4", className)}>
        {Array.from({ length: count }).map((_, i) => (
          <div key={i} className="card p-5 space-y-3">
            <SkeletonBase className="h-3 w-1/3" />
            <SkeletonBase className="h-6 w-1/2" />
            <SkeletonBase className="h-3 w-2/3" />
          </div>
        ))}
      </div>
    );
  },
});
