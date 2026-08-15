import {
  Calculator,
  ChartColumn,
  ChartLine,
  ChevronLeft,
  ChevronRight,
  Clock,
  FileDiff,
  FileText,
  FlaskConical,
  FolderOpen,
  FolderTree,
  GitBranch,
  Loader2,
  MessageCircle,
  MessagesSquare,
  Moon,
  Network,
  PanelLeft,
  PanelLeftClose,
  ScrollText,
  Search,
  Send,
  Settings,
  Sparkles,
  Sun,
  Trash2,
  Trophy,
  Wallet,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";

/** Semantic icon registry: name → Lucide SVG component. */
const ICONS = {
  metrics: ChartLine,
  tree: GitBranch,
  network: Network,
  algorithms: Calculator,
  experiments: FlaskConical,
  llm: MessagesSquare,
  settings: Settings,
  search: Search,
  wallet: Wallet,
  trophy: Trophy,
  report: FileText,
  sun: Sun,
  moon: Moon,
  sparkles: Sparkles,
  wrench: Wrench,
  diff: FileDiff,
  files: FolderTree,
  folder: FolderOpen,
  log: ScrollText,
  send: Send,
  loader: Loader2,
  chat: MessageCircle,
  chart: ChartColumn,
  clock: Clock,
  close: X,
  chevronLeft: ChevronLeft,
  chevronRight: ChevronRight,
  panelLeft: PanelLeft,
  panelLeftClose: PanelLeftClose,
  trash: Trash2,
} as const satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof ICONS;

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
}

/** Crisp, consistent SVG icon (replaces emoji for cross-platform fidelity). */
export function Icon({ name, size = 16, className }: IconProps) {
  const Component: LucideIcon = ICONS[name];
  return <Component size={size} className={className} />;
}
