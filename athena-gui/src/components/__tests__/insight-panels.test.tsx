import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderUi } from "../../test/render";
import LLMIOPanel from "../LLMIOPanel";
import ReportPanel from "../ReportPanel";
import AlgorithmsPanel from "../AlgorithmsPanel";
import EDAPreview from "../EDAPreview";

const bridgeMocks = vi.hoisted(() => ({
  tracesList: vi.fn(),
  traceGet: vi.fn(),
  experimentsList: vi.fn(),
  generateReport: vi.fn(),
  graphAlgorithms: vi.fn(),
  graphAlgorithm: vi.fn(),
  edaReport: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  ...bridgeMocks,
}));

beforeEach(() => {
  for (const mock of Object.values(bridgeMocks)) mock.mockReset();
});

describe("LLMIOPanel", () => {
  it("replays the selected agent's messages with role labels", async () => {
    bridgeMocks.tracesList.mockResolvedValue({
      traces: [{ agent_id: "ideator-1", file: "a.jsonl", size_bytes: 10, messages: 2, updated_at: 0 }],
    });
    bridgeMocks.traceGet.mockResolvedValue({
      agent_id: "ideator-1",
      messages: [
        { seq: 1, ts: "", role: "user", kind: "text", content: "propose hypotheses" },
        { seq: 2, ts: "", role: "tool", kind: "tool_return", content: "rows=891" },
      ],
    });

    renderUi(<LLMIOPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /ideator-1/ }));

    expect(await screen.findByText("propose hypotheses")).toBeInTheDocument();
    expect(screen.getByText("user")).toHaveAttribute("data-variant", "accent");
    expect(screen.getByText("tool")).toHaveAttribute("data-variant", "attention");
    expect(screen.getByText("工具返回")).toBeInTheDocument();
  });

  it("marks only the selected agent with aria-current", async () => {
    bridgeMocks.tracesList.mockResolvedValue({
      traces: [
        { agent_id: "ideator-1", file: "a.jsonl", size_bytes: 10, messages: 2, updated_at: 0 },
        { agent_id: "coder-2", file: "b.jsonl", size_bytes: 10, messages: 1, updated_at: 0 },
      ],
    });
    bridgeMocks.traceGet.mockResolvedValue({ agent_id: "ideator-1", messages: [] });

    renderUi(<LLMIOPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /ideator-1/ }));

    await waitFor(() => expect(screen.getByRole("button", { name: /ideator-1/ })).toHaveAttribute("aria-current", "true"));
    expect(screen.getByRole("button", { name: /coder-2/ })).not.toHaveAttribute("aria-current");
  });

  it("shows the error when loading a trace fails", async () => {
    bridgeMocks.tracesList.mockResolvedValue({
      traces: [{ agent_id: "ideator-1", file: "a.jsonl", size_bytes: 10, messages: 2, updated_at: 0 }],
    });
    bridgeMocks.traceGet.mockRejectedValue(new Error("trace file missing"));

    renderUi(<LLMIOPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /ideator-1/ }));

    expect(await screen.findByText("trace file missing")).toBeInTheDocument();
  });
});

describe("ReportPanel", () => {
  it("summarizes experiments and shows a report generation failure", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [] });
    bridgeMocks.generateReport.mockRejectedValue(new Error("report backend unavailable"));

    renderUi(<ReportPanel />);
    expect(await screen.findByText("暂无实验数据。完成实验后此处汇总 SOTA、最佳指标与状态分布。")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "生成报告" }));
    expect(await screen.findByText("report backend unavailable")).toBeInTheDocument();
  });
});

describe("AlgorithmsPanel", () => {
  it("runs the selected algorithm with its parameters and shows the JSON result", async () => {
    bridgeMocks.graphAlgorithms.mockResolvedValue({
      algorithms: [{ name: "ancestors", label: "祖先链", params: [{ name: "node_id", type: "str", required: true }] }],
    });
    bridgeMocks.graphAlgorithm.mockResolvedValue({ path: ["exp-0", "exp-1"] });

    renderUi(<AlgorithmsPanel />);
    fireEvent.change(await screen.findByRole("textbox", { name: /node_id/ }), { target: { value: "exp-1" } });
    fireEvent.click(screen.getByRole("button", { name: "运行" }));

    await waitFor(() => expect(bridgeMocks.graphAlgorithm).toHaveBeenCalledWith("ancestors", { node_id: "exp-1" }));
    expect(await screen.findByText(/"exp-1"/)).toBeInTheDocument();
  });

  it("shows the error when running an algorithm fails", async () => {
    bridgeMocks.graphAlgorithms.mockResolvedValue({
      algorithms: [{ name: "ancestors", label: "祖先链", params: [] }],
    });
    bridgeMocks.graphAlgorithm.mockRejectedValue(new Error("node not found"));

    renderUi(<AlgorithmsPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "运行" }));

    expect(await screen.findByText("node not found")).toBeInTheDocument();
  });
});

describe("EDAPreview", () => {
  it("renders the Markdown report and figures", async () => {
    bridgeMocks.edaReport.mockResolvedValue({
      eda_dir: "/work/eda",
      report: "# 数据概览\n\n共 891 行",
      figures: [{ name: "hist.png", mime: "image/png", data: "AAAA" }],
    });

    renderUi(<EDAPreview />);

    expect(await screen.findByRole("heading", { level: 1, name: "数据概览" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "hist.png" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "图表 (1)" })).toBeInTheDocument();
  });

  it("shows the empty state before PREPARE has produced an EDA directory", async () => {
    bridgeMocks.edaReport.mockResolvedValue({ eda_dir: null, report: null, figures: [] });

    renderUi(<EDAPreview />);

    expect(await screen.findByText("暂无 EDA 报告。运行 PREPARE 阶段后将在此预览数据探索结果。")).toBeInTheDocument();
  });
});
