import type { KeyboardEvent } from "react";
import {
  Button,
  Menu,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
} from "@fluentui/react-components";
import { ChevronDown, Columns3, FileText, ScanLine, Table2 } from "lucide-react";

type ResultTab = {
  id: string;
  label: string;
  icon?: "table" | "text" | "compare" | "multimodal";
};

const professionalTabs: ResultTab[] = [
  { id: "structure", label: "结构复核" },
  { id: "compare", label: "模型对比", icon: "compare" },
  { id: "multimodal", label: "视觉审校", icon: "multimodal" },
];

function tabIcon(icon: ResultTab["icon"]) {
  if (icon === "table") return <Table2 size={15} />;
  if (icon === "text") return <FileText size={15} />;
  if (icon === "compare") return <Columns3 size={15} />;
  if (icon === "multimodal") return <ScanLine size={15} />;
  return undefined;
}

export function ResultViewNavigation({
  tab,
  onSelect,
  tableCount,
  hasFusion = false,
}: {
  tab: string;
  onSelect: (value: string) => void;
  tableCount?: number;
  hasFusion?: boolean;
}) {
  const visibleTabs: ResultTab[] = [
    { id: "table", label: "表格", icon: "table" },
    { id: "text", label: "文字", icon: "text" },
  ];
  if (hasFusion) visibleTabs.push({ id: "review", label: "快速校对" });
  const activeProfessional = professionalTabs.find(item => item.id === tab);
  if (activeProfessional) visibleTabs.push(activeProfessional);
  const focusTab = visibleTabs.some(item => item.id === tab) ? tab : "table";

  const moveTab = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const buttons = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('button[role="tab"]'),
    );
    const currentIndex = buttons.indexOf(event.target as HTMLButtonElement);
    if (currentIndex < 0) return;
    event.preventDefault();
    const index = event.key === "Home" ? 0
      : event.key === "End" ? visibleTabs.length - 1
      : (currentIndex + (event.key === "ArrowRight" ? 1 : visibleTabs.length - 1)) % visibleTabs.length;
    onSelect(visibleTabs[index].id);
    buttons[index]?.focus();
  };

  return (
    <div className="result-view-navigation">
      <div className="result-tabs" role="tablist" aria-label="结果视图" onKeyDown={moveTab}>
        {visibleTabs.map(item => (
          <button
            key={item.id}
            id={`tab-${item.id}`}
            role="tab"
            aria-selected={tab === item.id}
            aria-controls="result-content"
            tabIndex={focusTab === item.id ? 0 : -1}
            className={tab === item.id ? "active" : ""}
            onClick={() => onSelect(item.id)}
          >
            {tabIcon(item.icon)}
            {item.label}
            {item.id === "table" && tableCount ? ` ${tableCount}` : ""}
          </button>
        ))}
      </div>
      <Menu>
        <MenuTrigger disableButtonEnhancement>
          <Button size="small" appearance="subtle" icon={<ChevronDown size={14} />} iconPosition="after">
            专业工具
          </Button>
        </MenuTrigger>
        <MenuPopover>
          <MenuList>
            {professionalTabs.map(item => (
              <MenuItem key={item.id} icon={tabIcon(item.icon)} onClick={() => onSelect(item.id)}>
                {item.label}
              </MenuItem>
            ))}
          </MenuList>
        </MenuPopover>
      </Menu>
    </div>
  );
}
