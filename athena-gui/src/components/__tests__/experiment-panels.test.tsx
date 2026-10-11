import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderUi } from "../../test/render";
import type { ExperimentDetail } from "../../lib/tauri-bridge";
import ExperimentLog from "../ExperimentLog";
import ExperimentManager from "../ExperimentManager";
import FileTree from "../FileTree";
import DiffViewer from "../DiffViewer";

const bridgeMocks = vi.hoisted(() => ({
  experimentsList: vi.fn(),
  experimentGet: vi.fn(),
  experimentTransition: vi.fn(),
  experimentSetSota: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  ...bridgeMocks,
}));

function experiment(id: string, overrides: Partial<ExperimentDetail["experiment"]> = {}, sota = false): ExperimentDetail {
  return {
    experiment: {
      id,
      parent_id: null,
      hypothesis_id: `h-${id}`,
      commit: "abc1234",
      plan: { kind: "search", change: "add window features", rubrics: ["macro F1"], run_config_ref: "", budget: {}, acceptance_rule: "beats SOTA" },
      gitwork: { path: `/work/${id}`, branch: `exp/${id}`, base_commit: "base000" },
      status: "SUCCEEDED",
      eval: { experiment_id: id, primary: 0.7412, secondary: {}, per_sample: "" },
      verdict: { winner: "candidate", p_value: 0.03 },
      artifacts: { model: `/work/${id}/model.pkl` },
      error: null,
      ...overrides,
    },
    hypothesis: {
      id: `h-${id}`,
      statement: `statement for ${id}`,
      intervention: "rolling window",
      expected_effect: "higher F1",
      status: "SUPPORTED",
      evidence_refs: [],
      parent_id: null,
      supersedes: [],
      priority: 1,
      order: null,
      patience: 1,
      turn_limit: null,
      sources: [],
    },
    sota,
  };
}

beforeEach(() => {
  for (const mock of Object.values(bridgeMocks)) mock.mockReset();
});

describe("ExperimentLog", () => {
  it("lists experiments on a timeline with lifecycle labels", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [experiment("exp-1"), experiment("exp-2", { status: "FAILED" })] });

    renderUi(<ExperimentLog />);

    expect(await screen.findByRole("heading", { level: 3, name: "实验日志 (2)" })).toBeInTheDocument();
    expect(screen.getByText("statement for exp-1")).toBeInTheDocument();
    expect(screen.getByText("FAILED")).toHaveAttribute("data-variant", "danger");
  });

  it("keeps the rendered timeline and shows the error when a refresh fails", async () => {
    bridgeMocks.experimentsList
      .mockResolvedValueOnce({ experiments: [experiment("exp-1")] })
      .mockRejectedValueOnce(new Error("gateway timeout"));

    renderUi(<ExperimentLog />);
    await screen.findByText("statement for exp-1");

    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    expect(await screen.findByText("gateway timeout")).toBeInTheDocument();
    expect(screen.getByText("statement for exp-1")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled());
  });
});

describe("ExperimentManager", () => {
  it("opens the detail for a clicked row", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [experiment("exp-1")] });
    bridgeMocks.experimentGet.mockResolvedValue(experiment("exp-1"));

    renderUi(<ExperimentManager />);
    fireEvent.click(await screen.findByText("statement for exp-1"));

    expect(await screen.findByText("实验详情")).toBeInTheDocument();
    expect(bridgeMocks.experimentGet).toHaveBeenCalledWith("exp-1");
    expect(screen.getByText("干预: rolling window · 预期: higher F1")).toBeInTheDocument();
  });

  it("refuses a FAILED transition without an error explanation", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [experiment("exp-1", { status: "RUNNING" })] });
    bridgeMocks.experimentGet.mockResolvedValue(experiment("exp-1", { status: "RUNNING" }));

    renderUi(<ExperimentManager />);
    fireEvent.click(await screen.findByText("statement for exp-1"));
    fireEvent.change(await screen.findByRole("combobox", { name: "推进状态" }), { target: { value: "FAILED" } });
    fireEvent.click(screen.getByRole("button", { name: "确认推进" }));

    expect(await screen.findByText("FAILED 状态需要填写 error 说明。")).toBeInTheDocument();
    expect(bridgeMocks.experimentTransition).not.toHaveBeenCalled();
  });
});

describe("FileTree and DiffViewer", () => {
  it("show the SOTA experiment's workspace, artifacts and change spec", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [experiment("exp-1"), experiment("exp-2", {}, true)] });

    renderUi(
      <>
        <FileTree />
        <DiffViewer />
      </>,
    );

    const workspaces = await screen.findAllByRole("heading", { name: "工作区" });
    expect(workspaces).toHaveLength(2);
    expect(screen.getAllByText("/work/exp-2")).toHaveLength(2);
    expect(screen.getByText("/work/exp-2/model.pkl")).toBeInTheDocument();
    expect(screen.getByText("exp-2 ★ SOTA")).toBeInTheDocument();
    expect(within(screen.getByRole("list")).getByText("macro F1")).toBeInTheDocument();
  });

  it("show empty states when there are no experiments", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [] });

    renderUi(
      <>
        <FileTree />
        <DiffViewer />
      </>,
    );

    expect(await screen.findByText("暂无实验。运行搜索后此处展示最佳实验的工作区与产物。")).toBeInTheDocument();
    expect(screen.getByText("暂无实验。运行搜索后此处展示最佳实验的变更规格。")).toBeInTheDocument();
  });
});
