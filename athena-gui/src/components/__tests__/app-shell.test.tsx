import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { AppShell } from "../shell/AppShell";

describe("AppShell", () => {
  it("renders session sidebar, conversation, right rail, and context surface", () => {
    renderUi(
      <AppShell
        sidebar={<div>sidebar</div>}
        conversation={<div>conversation</div>}
        rightRail={<div>right rail</div>}
        contextSurface={<div>context surface</div>}
      />,
    );

    expect(screen.getByText("sidebar")).toBeInTheDocument();
    expect(screen.getByText("conversation")).toBeInTheDocument();
    expect(screen.getByText("right rail")).toBeInTheDocument();
    expect(screen.getByText("context surface")).toBeInTheDocument();
  });
});
