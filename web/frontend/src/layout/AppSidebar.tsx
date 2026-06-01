import { useCallback } from "react";
import { Link, useLocation } from "react-router";
import { GridIcon, PieChartIcon, TableIcon, PageIcon, BoltIcon, ChatIcon, PlugInIcon, BoxIcon, DocsIcon, FileIcon } from "../icons";
import { useSidebar } from "../context/SidebarContext";
import { cn } from "../lib/utils";

type NavItem = {
  name: string;
  icon: React.ReactNode;
  path?: string;
  group?: string;
};

const navItems: NavItem[] = [
  { icon: <GridIcon />, name: "总览", path: "/", group: "概览" },
  { icon: <BoltIcon />, name: "进化监控", path: "/evolution", group: "概览" },
  { icon: <ChatIcon />, name: "对局回放", path: "/matches", group: "对局" },
  { icon: <PieChartIcon />, name: "评分趋势", path: "/rating-trends", group: "对局" },
  { icon: <TableIcon />, name: "对局矩阵", path: "/match-matrix", group: "对局" },
  { icon: <PageIcon />, name: "迭代日志", path: "/logs", group: "管理" },
  { icon: <PlugInIcon />, name: "控制面板", path: "/control", group: "管理" },
  { icon: <BoxIcon />, name: "Bot 管理", path: "/bots", group: "管理" },
  { icon: <DocsIcon />, name: "经验池", path: "/experience", group: "管理" },
  { icon: <FileIcon />, name: "提示词编辑器", path: "/prompts", group: "管理" },
];

const GROUP_ORDER = ["概览", "对局", "管理"];

const LogoIcon = ({ className }: { className?: string }) => (
  <svg className={className} width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="5" r="2"/>
    <circle cx="12" cy="19" r="2"/>
    <path d="M12 7v3.5a2.5 2.5 0 0 1-2.5 2.5H7"/>
    <path d="M12 17v-3.5a2.5 2.5 0 0 1 2.5-2.5H17"/>
    <path d="M7 12h3.5a2.5 2.5 0 0 1 2.5 2.5V17"/>
    <path d="M17 12h-3.5a2.5 2.5 0 0 1-2.5-2.5V7"/>
  </svg>
);

const AppSidebar: React.FC = () => {
  const { isExpanded, isMobileOpen, isHovered, setIsHovered } = useSidebar();
  const location = useLocation();

  const isActive = useCallback(
    (path: string) => location.pathname === path,
    [location.pathname]
  );

  const showLabels = isExpanded || isHovered || isMobileOpen;

  const grouped = GROUP_ORDER.map((g) => ({
    label: g,
    items: navItems.filter((n) => n.group === g),
  }));

  return (
    <aside
      className={cn(
        "fixed mt-16 flex flex-col lg:mt-0 top-0 left-0 h-screen transition-all duration-300 ease-in-out z-50",
        "bg-white dark:bg-surface-0 border-r border-gray-200 dark:border-border-subtle",
        isExpanded || isMobileOpen ? "w-[260px]" : isHovered ? "w-[260px]" : "w-[72px]",
        isMobileOpen ? "translate-x-0" : "-translate-x-full",
        "lg:translate-x-0",
      )}
      onMouseEnter={() => !isExpanded && setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      <div className={cn(
        "h-16 flex items-center border-b border-gray-100 dark:border-border-subtle",
        !isExpanded && !isHovered ? "lg:justify-center" : "px-5",
      )}>
        <Link to="/" className="flex items-center gap-2.5">
          <LogoIcon className="text-brand-500 shrink-0" />
          {showLabels && (
            <span className="text-base font-bold text-gray-800 dark:text-white tracking-tight">
              Bot 自进化
            </span>
          )}
        </Link>
      </div>

      <div className="flex flex-col overflow-y-auto duration-300 ease-linear no-scrollbar flex-1 py-4">
        <nav className="flex flex-col gap-5 px-3">
          {grouped.map((group, gi) => (
            <div key={group.label}>
              {showLabels ? (
                <h2 className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-[0.1em] text-gray-400 dark:text-gray-500">
                  {group.label}
                </h2>
              ) : (
                gi > 0 && <div className="mx-3 my-2 border-t border-gray-100 dark:border-border-subtle" />
              )}
              <ul className="flex flex-col gap-0.5">
                {group.items.map((nav) => {
                  const active = nav.path && isActive(nav.path);
                  return (
                    <li key={nav.name}>
                      {nav.path && (
                        <Link
                          to={nav.path}
                          className={cn(
                            "group relative flex items-center gap-3 rounded-lg text-sm font-medium transition-all duration-150",
                            active
                              ? "bg-brand-50 text-brand-600 dark:bg-brand-500/[0.12] dark:text-brand-400"
                              : "text-gray-600 hover:bg-gray-50 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/[0.04] dark:hover:text-gray-200",
                            showLabels ? "px-3 py-2" : "px-0 py-2.5 justify-center",
                          )}
                        >
                          {active && (
                            <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-r-full bg-brand-500" />
                          )}
                          <span className={cn(
                            "shrink-0 [&>svg]:!size-[18px]",
                            active
                              ? "text-brand-500 dark:text-brand-400"
                              : "text-gray-400 group-hover:text-gray-600 dark:text-gray-500 dark:group-hover:text-gray-300",
                          )}>
                            {nav.icon}
                          </span>
                          {showLabels && (
                            <span className="truncate">{nav.name}</span>
                          )}
                        </Link>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>
      </div>
    </aside>
  );
};

export default AppSidebar;
