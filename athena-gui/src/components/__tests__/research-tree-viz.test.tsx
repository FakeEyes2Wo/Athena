import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderUi } from "../../test/render";

const bridgeMocks = vi.hoisted(() => ({
  treeGet: vi.fn(),
  treeSave: vi.fn(),
}));

const flowMocks = vi.hoisted(() => ({
  onNodesChange: undefined as undefined | ((changes: Array<{
    id: string;
    type: string;
    position?: { x: number; y: number };
  }>) => void),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/tauri-bridge")>();
  return {
    ...actual,
    treeGet: bridgeMocks.treeGet,
    treeSave: bridgeMocks.treeSave,
  };
});

vi.mock("reactflow", async () => {
  const React = await import("react");
  return {
    default: ({
      children,
      nodes,
      edges,
      onNodesChange,
    }: {
      children: React.ReactNode;
      nodes: Array<{
        id: string;
        position: { x: number; y: number };
        data: { hypothesis: { statement: string } };
      }>;
      edges: unknown[];
      onNodesChange: (changes: Array<{
        id: string;
        type: string;
        position?: { x: number; y: number };
      }>) => void;
    }) => {
      flowMocks.onNodesChange = onNodesChange;
      return (
        <div data-testid="research-tree-flow" data-edge-count={edges.length}>
          {nodes.map((node) => (
            <span key={node.id} data-testid={`node-${node.id}-position`}>
              {node.data.hypothesis.statement}:{node.position.x},{node.position.y}
            </span>
          ))}
          {children}
        </div>
      );
    },
    Background: () => null,
    Controls: () => null,
    Handle: () => null,
    MiniMap: () => null,
    Position: { Top: "top", Bottom: "bottom" },
    MarkerType: { ArrowClosed: "arrowclosed" },
    useNodesState: (initialNodes: Array<{ id: string; position: { x: number; y: number } }>) => {
      const [nodes, setNodes] = React.useState(initialNodes);
      const onNodesChange = React.useCallback((changes: Array<{
        id: string;
        type: string;
        position?: { x: number; y: number };
      }>) => {
        setNodes((currentNodes) => currentNodes.map((node) => {
          const change = changes.find((candidate) => candidate.id === node.id && candidate.type === "position");
          return change?.position ? { ...node, position: change.position } : node;
        }));
      }, []);
      return [nodes, setNodes, onNodesChange] as const;
    },
    useEdgesState: (initialEdges: unknown[]) => {
      const [edges, setEdges] = React.useState(initialEdges);
      return [edges, setEdges, vi.fn()] as const;
    },
  };
});

import ResearchTreeViz from "../ResearchTreeViz";

const EMPTY_TREE = { version: 2, sota_id: null, hypotheses: {}, experiments: {} };
const LOADED_TREE = {
  version: 2,
  sota_id: "experiment-1",
  hypotheses: {
    "hypothesis-1": {
      id: "hypothesis-1",
      parent_id: null,
      statement: "Raise the regularization parameter",
      intervention: "Set regularization to 0.1",
      expected_effect: "Reduce overfitting",
      status: "SUPPORTED",
      evidence_refs: [],
      patience_grant: 0,
      patience_evidence_ref: null,
      sources: [],
    },
  },
  experiments: {
    "experiment-1": {
      parent_id: null,
      hypothesis_id: "hypothesis-1",
      commit: "a".repeat(40),
      plan: {
        kind: "search",
        change: "regularization=0.1",
        rubrics: [],
        run_config_ref: "artifact://baseline",
        budget: {},
        acceptance_rule: "Improve validation score",
      },
      gitwork: { path: "worktree", branch: "experiment-1", base_commit: "a".repeat(40) },
      status: "SUCCEEDED",
      eval: {
        experiment_id: "experiment-1",
        primary: 0.82,
        secondary: {},
        per_sample: "artifact://samples/experiment-1",
      },
      verdict: null,
      artifacts: {},
      error: null,
    },
  },
};

const TREE_WITH_ONE_CHILD = {
  version: 2,
  sota_id: "experiment-root",
  hypotheses: LOADED_TREE.hypotheses,
  experiments: {
    "experiment-root": {
      ...LOADED_TREE.experiments["experiment-1"],
      parent_id: null,
    },
    "experiment-first": {
      ...LOADED_TREE.experiments["experiment-1"],
      parent_id: "experiment-root",
    },
  },
};

const TREE_WITH_TWO_CHILDREN = {
  ...TREE_WITH_ONE_CHILD,
  experiments: {
    ...TREE_WITH_ONE_CHILD.experiments,
    "experiment-second": {
      ...LOADED_TREE.experiments["experiment-1"],
      parent_id: "experiment-root",
    },
  },
};

describe("ResearchTreeViz", () => {
  beforeEach(() => {
    bridgeMocks.treeGet.mockReset();
    bridgeMocks.treeSave.mockReset();
    bridgeMocks.treeGet.mockResolvedValue({ tree: EMPTY_TREE });
    bridgeMocks.treeSave.mockResolvedValue({ saved: true, path: "research-tree.json" });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads the research tree when the panel opens", async () => {
    await act(async () => {
      renderUi(<ResearchTreeViz />);
    });

    expect(bridgeMocks.treeGet).toHaveBeenCalledTimes(1);
  });

  it("renders graph nodes loaded after the panel first mounts", async () => {
    bridgeMocks.treeGet.mockResolvedValue({ tree: LOADED_TREE });

    await act(async () => {
      renderUi(<ResearchTreeViz />);
    });

    expect(screen.getByTestId("research-tree-flow")).toHaveTextContent(
      "Raise the regularization parameter",
    );
  });

  it("saves and reloads the research tree from the controls", async () => {
    await act(async () => {
      renderUi(<ResearchTreeViz />);
    });

    expect(bridgeMocks.treeGet).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.click(screen.getAllByRole("button")[1]);
    });

    expect(bridgeMocks.treeSave).toHaveBeenCalledTimes(1);
    expect(bridgeMocks.treeGet).toHaveBeenCalledTimes(2);
  });

  it("retains a dragged node position when save reloads the same node", async () => {
    const refreshedTree = {
      ...LOADED_TREE,
      experiments: { ...LOADED_TREE.experiments },
    };
    bridgeMocks.treeGet
      .mockResolvedValueOnce({ tree: LOADED_TREE })
      .mockResolvedValueOnce({ tree: refreshedTree });

    await act(async () => {
      renderUi(<ResearchTreeViz />);
    });

    await act(async () => {
      flowMocks.onNodesChange?.([{
        id: "experiment-1",
        type: "position",
        position: { x: 420, y: 240 },
      }]);
    });

    expect(screen.getByTestId("node-experiment-1-position")).toHaveTextContent("420,240");

    await act(async () => {
      fireEvent.click(screen.getAllByRole("button")[1]);
    });

    expect(bridgeMocks.treeGet).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("node-experiment-1-position")).toHaveTextContent("420,240");
  });

  it("re-centers unmodified siblings after a refresh adds another child", async () => {
    bridgeMocks.treeGet
      .mockResolvedValueOnce({ tree: TREE_WITH_ONE_CHILD })
      .mockResolvedValueOnce({ tree: TREE_WITH_TWO_CHILDREN });

    await act(async () => {
      renderUi(<ResearchTreeViz />);
    });

    expect(screen.getByTestId("node-experiment-first-position")).toHaveTextContent("0,150");

    await act(async () => {
      fireEvent.click(screen.getAllByRole("button")[0]);
    });

    expect(screen.getByTestId("node-experiment-first-position")).toHaveTextContent("-140,150");
    expect(screen.getByTestId("node-experiment-second-position")).toHaveTextContent("140,150");
  });

  it("saves and reloads every thirty seconds after autosave is enabled", async () => {
    vi.useFakeTimers();
    await act(async () => {
      renderUi(<ResearchTreeViz />);
    });

    expect(bridgeMocks.treeGet).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("checkbox"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(bridgeMocks.treeSave).toHaveBeenCalledTimes(1);
    expect(bridgeMocks.treeGet).toHaveBeenCalledTimes(2);
  });
});
