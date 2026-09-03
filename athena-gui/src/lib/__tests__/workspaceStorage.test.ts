import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  addRecentRoot,
  loadRecentRoots,
  loadWorkspaceSessions,
  persistRecentRoots,
  persistWorkspaceSessions,
} from "../workspaceStorage";

describe("workspaceStorage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("addRecentRoot prepends new roots without promoting known roots", () => {
    expect(addRecentRoot("/b", ["/a", "/c"])).toEqual(["/b", "/a", "/c"]);
    expect(addRecentRoot("/b", ["/b", "/a"])).toEqual(["/b", "/a"]);
    expect(addRecentRoot("/b", ["/a", "/b"])).toEqual(["/a", "/b"]);
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

  it("namespaces cached session summaries by workspace", () => {
    persistWorkspaceSessions("/alpha", [{ id: "s-alpha", title: "Alpha" }]);
    persistWorkspaceSessions("/beta", [{ id: "s-beta", title: "Beta" }]);

    expect(loadWorkspaceSessions("/alpha")).toEqual([{ id: "s-alpha", title: "Alpha" }]);
    expect(loadWorkspaceSessions("/beta")).toEqual([{ id: "s-beta", title: "Beta" }]);
    expect(localStorage).toHaveLength(2);
  });

  it("returns [] for malformed cached session summaries", () => {
    persistWorkspaceSessions("/alpha", [{ id: "valid", title: "Valid" }]);
    const key = localStorage.key(0);
    expect(key).not.toBeNull();

    localStorage.setItem(key!, "{not json");
    expect(loadWorkspaceSessions("/alpha")).toEqual([]);

    localStorage.setItem(key!, JSON.stringify({ id: "not-an-array", title: "Invalid" }));
    expect(loadWorkspaceSessions("/alpha")).toEqual([]);
  });

  it("normalizes summaries and keeps the first row for each non-empty id", () => {
    persistWorkspaceSessions("/alpha", [
      { id: " s-1 ", title: " First " },
      { id: "s-1", title: "Duplicate" },
      { id: "", title: "Blank id" },
      { id: "s-2", title: "  " },
      { id: 3, title: "Wrong id type" },
      { id: "s-3", title: 4 },
    ] as unknown as Array<{ id: string; title: string }>);

    expect(loadWorkspaceSessions("/alpha")).toEqual([{ id: "s-1", title: "First" }]);
  });

  it("caps cached summaries at 200 rows", () => {
    const sessions = Array.from({ length: 205 }, (_, index) => ({
      id: `s-${index}`,
      title: `Session ${index}`,
    }));

    persistWorkspaceSessions("/alpha", sessions);

    expect(loadWorkspaceSessions("/alpha")).toHaveLength(200);
    const cached = loadWorkspaceSessions("/alpha");
    expect(cached[cached.length - 1]).toEqual({ id: "s-199", title: "Session 199" });
  });

  it("removes stale cached summaries when persisting an empty list", () => {
    persistWorkspaceSessions("/alpha", [{ id: "stale", title: "Stale" }]);

    persistWorkspaceSessions("/alpha", []);

    expect(loadWorkspaceSessions("/alpha")).toEqual([]);
    expect(localStorage).toHaveLength(0);
  });

  it("degrades to an empty cache when localStorage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => persistWorkspaceSessions("/alpha", [{ id: "s-1", title: "Alpha" }])).not.toThrow();

    vi.restoreAllMocks();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(loadWorkspaceSessions("/alpha")).toEqual([]);
  });
});
