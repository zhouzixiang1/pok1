import { useCallback } from "react";
import { Link, useLocation } from "react-router";
import { HorizontaLDots, GridIcon, PieChartIcon, TableIcon, PageIcon, BoltIcon, ChatIcon, PlugInIcon, BoxIcon, DocsIcon, FileIcon } from "../icons";
import { useSidebar } from "../context/SidebarContext";

type NavItem = {
  name: string;
  icon: React.ReactNode;
  path?: string;
  subItems?: { name: string; path: string }[];
};

const navItems: NavItem[] = [
  {
    icon: <GridIcon />,
    name: "总览",
    path: "/",
  },
  {
    icon: <BoltIcon />,
    name: "进化监控",
    path: "/evolution",
  },
  {
    icon: <ChatIcon />,
    name: "对局回放",
    path: "/matches",
  },
  {
    icon: <PieChartIcon />,
    name: "评分趋势",
    path: "/rating-trends",
  },
  {
    icon: <TableIcon />,
    name: "对局矩阵",
    path: "/match-matrix",
  },
  {
    icon: <PageIcon />,
    name: "代际日志",
    path: "/logs",
  },
  {
    icon: <PlugInIcon />,
    name: "控制面板",
    path: "/control",
  },
  {
    icon: <BoxIcon />,
    name: "机器人管理",
    path: "/bots",
  },
  {
    icon: <DocsIcon />,
    name: "经验池",
    path: "/experience",
  },
  {
    icon: <FileIcon />,
    name: "提示词编辑器",
    path: "/prompts",
  },
];

const LogoIcon = ({ className }: { className?: string }) => (
  <svg className={className} width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
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

  return (
    <aside
      className={`fixed mt-16 flex flex-col lg:mt-0 top-0 px-5 left-0 bg-white dark:bg-gray-900 dark:border-gray-800 text-gray-900 h-screen transition-all duration-300 ease-in-out z-50 border-r border-gray-200
        ${isExpanded || isMobileOpen ? "w-[290px]" : isHovered ? "w-[290px]" : "w-[90px]"}
        ${isMobileOpen ? "translate-x-0" : "-translate-x-full"}
        lg:translate-x-0`}
      onMouseEnter={() => !isExpanded && setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      <div className={`py-8 flex ${!isExpanded && !isHovered ? "lg:justify-center" : "justify-start"}`}>
        <Link to="/" className="flex items-center gap-2">
          {isExpanded || isHovered || isMobileOpen ? (
            <span className="text-xl font-bold text-gray-800 dark:text-white flex items-center gap-2">
              <LogoIcon className="text-brand-500" />
              进化系统
            </span>
          ) : (
            <LogoIcon className="text-brand-500" />
          )}
        </Link>
      </div>
      <div className="flex flex-col overflow-y-auto duration-300 ease-linear no-scrollbar">
        <nav className="mb-6">
          <div className="flex flex-col gap-4">
            <div>
              <h2
                className={`mb-4 text-xs uppercase flex leading-[20px] text-gray-400 ${
                  !isExpanded && !isHovered ? "lg:justify-center" : "justify-start"
                }`}
              >
                {isExpanded || isHovered || isMobileOpen ? "菜单" : <HorizontaLDots className="size-6" />}
              </h2>
              <ul className="flex flex-col gap-4">
                {navItems.map((nav) => (
                  <li key={nav.name}>
                    {nav.path && (
                      <Link
                        to={nav.path}
                        className={`menu-item group ${isActive(nav.path) ? "menu-item-active" : "menu-item-inactive"}`}
                      >
                        <span
                          className={`menu-item-icon-size ${isActive(nav.path) ? "menu-item-icon-active" : "menu-item-icon-inactive"}`}
                        >
                          {nav.icon}
                        </span>
                        {(isExpanded || isHovered || isMobileOpen) && (
                          <span className="menu-item-text">{nav.name}</span>
                        )}
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </nav>
      </div>
    </aside>
  );
};

export default AppSidebar;
