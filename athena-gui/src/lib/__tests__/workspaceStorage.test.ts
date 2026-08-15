import { beforeEach, describe, expect, it } from "vitest";
import {
  addRecentRoot,
  loadRecentRoots,
  persistRecentRoots,
} from "../workspaceStorage";

describe("workspaceStorage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("addRecentRoot prepends and de-duplicates", () => {
    expect(addRecentRoot("/b", ["/a", "/c"])).toEqual(["/b", "/a", "/c"]);
    expect(addRecentRoot("/b", ["/b", "/a"])).toEqual(["/b", "/a"]);
  });

  it("addRecentRoot ignores blank and caps at 8 entries", () => {
    expect(addRecentRoot("  ", ["/a"])).toEqual(["/a"]);
    const many = ["/1", "/2", "/3", "/4", "/5", "/6", "/7", "/8"];
    expect(addRecentRoot("/9", many)).toEqual([
      "/9",
      "/1",
      "/2",
      "/3",
      "/4",
      "/5",
      "/6",
      "/7",
    ]);
  });

  it("persists and reloads recent roots", () => {
    persistRecentRoots(["/b", "/a"]);
    expect(loadRecentRoots()).toEqual(["/b", "/a"]);
  });

  it("returns [] on empty or malformed storage", () => {
    expect(loadRecentRoots()).toEqual([]);
    localStorage.setItem("athena.workspace.recent", "{not json");
    expect(loadRecentRoots()).toEqual([]);
    localStorage.setItem("athena.workspace.recent", JSON.stringify([1, "/a", "", " /b "]));
    expect(loadRecentRoots()).toEqual(["/a", "/b"]);
  });
});
