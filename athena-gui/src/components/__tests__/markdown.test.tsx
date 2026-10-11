import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "../common/Markdown";

const PNG = "data:image/png;base64,iVBORw0KGgo=";

describe("Markdown", () => {
  it("renders prose and opens links outside the app", () => {
    render(<Markdown>{"**粗体** 与 [链接](https://example.com)"}</Markdown>);
    expect(screen.getByText("粗体").tagName).toBe("STRONG");
    expect(screen.getByRole("link", { name: "链接" })).toHaveAttribute("target", "_blank");
  });

  it("drops inline data images by default", () => {
    render(<Markdown>{`![fig](${PNG})`}</Markdown>);
    expect(screen.getByRole("img", { name: "fig" })).not.toHaveAttribute("src", PNG);
  });

  it("renders backend-embedded figures when data images are allowed", () => {
    render(<Markdown dataImages>{`![fig](${PNG})`}</Markdown>);
    expect(screen.getByRole("img", { name: "fig" })).toHaveAttribute("src", PNG);
  });

  it("never allows script URLs, even with data images enabled", () => {
    render(<Markdown dataImages>{"[x](javascript:alert(1))"}</Markdown>);
    expect(screen.getByText("x").getAttribute("href") ?? "").not.toContain("javascript");
  });
});

describe("Markdown GFM", () => {
  it("renders pipe tables as real tables", () => {
    render(<Markdown>{"| | pred 0 | pred 1 |\n|---|---|---|\n| true 0 | 503005 | 691 |"}</Markdown>);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "pred 0" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "503005" })).toBeInTheDocument();
  });
});
