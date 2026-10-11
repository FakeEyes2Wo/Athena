import { describe, expect, it } from "vitest";
import { initialViewport, layoutLineage } from "../graphLayout";

describe("layoutLineage", () => {
  it("returns empty map for no nodes", () => {
    expect(layoutLineage([], [])).toEqual(new Map());
  });

  it("lays out a chain into increasing vertical layers", () => {
    const positions = layoutLineage(
      ["a", "b", "c"],
      [
        { source: "a", target: "b" },
        { source: "b", target: "c" },
      ],
    );
    expect([...positions.keys()].sort()).toEqual(["a", "b", "c"]);
    expect(positions.get("a")!.y).toBeLessThan(positions.get("b")!.y);
    expect(positions.get("b")!.y).toBeLessThan(positions.get("c")!.y);
  });

  it("centers a parent over two children", () => {
    const positions = layoutLineage(
      ["root", "left", "right"],
      [
        { source: "root", target: "left" },
        { source: "root", target: "right" },
      ],
    );
    const root = positions.get("root")!;
    const left = positions.get("left")!;
    const right = positions.get("right")!;
    expect(left.y).toBeGreaterThan(root.y);
    expect(right.y).toBeGreaterThan(root.y);
    // parent x sits between its children.
    const mid = (left.x + right.x) / 2;
    expect(Math.abs(root.x - mid)).toBeLessThan(80);
  });

  it("handles isolated nodes (no edges) without throwing", () => {
    const positions = layoutLineage(["solo", "pair-a", "pair-b"], [
      { source: "pair-a", target: "pair-b" },
    ]);
    expect([...positions.keys()].sort()).toEqual(["pair-a", "pair-b", "solo"]);
  });
});

describe("initialViewport", () => {
  const opts = { nodeWidth: 200, nodeHeight: 100, padding: 20 };

  it("fits a small graph and centers it", () => {
    const view = initialViewport([{ x: 0, y: 0 }, { x: 0, y: 200 }], { width: 1000, height: 800 }, opts);
    expect(view.zoom).toBe(1);
    expect(view.x).toBe(400);
    expect(view.y).toBe(250);
  });

  it("keeps a wide tree readable and top-aligned on its root", () => {
    const leaves = Array.from({ length: 20 }, (_, i) => ({ x: i * 260, y: 200 }));
    const root = { x: 9.5 * 260, y: 0 };
    const view = initialViewport([root, ...leaves], { width: 1000, height: 600 }, opts);
    expect(view.zoom).toBe(0.75);
    expect(view.y).toBe(20);
    // root center lands in the middle of the viewport
    expect(view.x + (root.x + 100) * view.zoom).toBeCloseTo(500);
  });

  it("does not leave empty space beside a root near the graph edge", () => {
    const leaves = Array.from({ length: 20 }, (_, i) => ({ x: i * 260, y: 200 }));
    const view = initialViewport([{ x: 0, y: 0 }, ...leaves], { width: 1000, height: 600 }, opts);
    expect(view.x).toBe(20);
  });
});
