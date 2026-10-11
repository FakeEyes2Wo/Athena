import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { ReactFlowProvider } from "reactflow";
import { renderUi } from "../../test/render";
import { FlowNodeCard } from "../common/FlowNodeCard";
import MetricChart from "../MetricChart";
import HypothesisGraphViz from "../HypothesisGraphViz";

const bridgeMocks = vi.hoisted(() => ({
  experimentsList: vi.fn(),
  hypothesisGraph: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  ...bridgeMocks,
}));

beforeEach(() => {
  for (const mock of Object.values(bridgeMocks)) mock.mockReset();
});

function renderNode(status: string) {
  return renderUi(
    <ReactFlowProvider>
      <FlowNodeCard statement="加入 120 分钟滑动窗口统计特征" primaryText="0.7412" status={status} badge={status} />
    </ReactFlowProvider>,
  );
}

describe("FlowNodeCard", () => {
  it("colors the label and left border by hypothesis status", () => {
    renderNode("SUPPORTED");
    const label = screen.getByText("SUPPORTED");
    expect(label).toHaveAttribute("data-variant", "success");
    expect(label.parentElement).toHaveStyle({ borderLeftColor: "var(--borderColor-success-emphasis)" });
    expect(screen.getByText("0.7412")).toBeInTheDocument();
  });

  it("falls back to neutral styling for an unknown status", () => {
    renderNode("ARCHIVED");
    const label = screen.getByText("ARCHIVED");
    expect(label).toHaveAttribute("data-variant", "secondary");
    expect(label.parentElement).toHaveStyle({ borderLeftColor: "var(--borderColor-emphasis)" });
  });
});

describe("MetricChart", () => {
  it("shows the empty state when no experiment has a metric", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [] });
    renderUi(<MetricChart />);
    expect(await screen.findByText("暂无评估指标。运行实验后此处展示 primary 指标趋势。")).toBeInTheDocument();
  });

  it("shows the error when loading experiments fails", async () => {
    bridgeMocks.experimentsList.mockRejectedValue(new Error("metrics unavailable"));
    renderUi(<MetricChart />);
    expect(await screen.findByText("metrics unavailable")).toBeInTheDocument();
  });
});

describe("HypothesisGraphViz", () => {
  it("shows the empty state before any hypothesis exists", async () => {
    bridgeMocks.hypothesisGraph.mockResolvedValue({ nodes: [], edges: [] });
    renderUi(<HypothesisGraphViz />);
    expect(await screen.findByText("暂无假设图数据。启动搜索产生假设后，此处将展示谱系与取代关系。")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "假设图 (0 个假设)" })).toBeInTheDocument();
  });

  it("shows the error when loading the graph fails", async () => {
    bridgeMocks.hypothesisGraph.mockRejectedValue(new Error("graph unavailable"));
    renderUi(<HypothesisGraphViz />);
    expect(await screen.findByText("graph unavailable")).toBeInTheDocument();
  });
});
