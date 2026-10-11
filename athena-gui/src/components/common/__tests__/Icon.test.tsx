import { afterEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { ICON_NAMES, Icon } from "../Icon";

afterEach(() => vi.restoreAllMocks());

describe("Icon", () => {
  it("renders an SVG for every registered name", () => {
    expect(ICON_NAMES.length).toBeGreaterThan(0);
    for (const name of ICON_NAMES) {
      const { container, unmount } = render(<Icon name={name} size={16} />);
      expect(container.querySelector("svg"), name).not.toBeNull();
      unmount();
    }
  });

  it("rejects unknown names at the type level and at runtime", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    // @ts-expect-error 未注册的图标名必须在类型层面被拒绝
    expect(() => render(<Icon name="not-an-icon" />)).toThrow();
  });
});
