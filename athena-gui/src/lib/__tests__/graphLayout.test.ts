import { describe, expect, it } from "vitest";
import { layoutLineage } from "../graphLayout";

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
