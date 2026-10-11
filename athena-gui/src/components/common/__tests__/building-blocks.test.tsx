import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { Button } from "@primer/react";
import { renderUi } from "../../../test/render";
import { hypothesisBorderColor, hypothesisVariant } from "../hypothesisStatus";
import { StatusBadge } from "../StatusBadge";
import { Panel } from "../Panel";
import { KeyValueList } from "../KeyValueList";
import { EmptyState } from "../EmptyState";
import { CodeBlock } from "../CodeBlock";

describe("hypothesisStatus", () => {
  it("maps each known hypothesis status to its Primer Label variant", () => {
    expect(hypothesisVariant("SUPPORTED")).toBe("success");
    expect(hypothesisVariant("REFUTED")).toBe("danger");
    expect(hypothesisVariant("PROPOSED")).toBe("accent");
    expect(hypothesisVariant("REJECTED")).toBe("secondary");
    expect(hypothesisVariant("INCONCLUSIVE")).toBe("attention");
  });

  it("uses Primer emphasis border colors for graph nodes", () => {
    expect(hypothesisBorderColor("SUPPORTED")).toBe("var(--borderColor-success-emphasis)");
    expect(hypothesisBorderColor("REFUTED")).toBe("var(--borderColor-danger-emphasis)");
  });

  it("falls back to secondary for statuses the frontend does not know", () => {
    expect(hypothesisVariant("SOMETHING_NEW")).toBe("secondary");
    expect(hypothesisBorderColor("SOMETHING_NEW")).toBe("var(--borderColor-emphasis)");
  });
});

describe("StatusBadge", () => {
  it("renders a Primer Label with the lifecycle variant", () => {
    renderUi(<StatusBadge status="FAILED" />);
    expect(screen.getByText("FAILED")).toHaveAttribute("data-variant", "danger");
  });

  it("renders unknown statuses as a secondary label without failing", () => {
    renderUi(<StatusBadge status="ARCHIVED" />);
    expect(screen.getByText("ARCHIVED")).toHaveAttribute("data-variant", "secondary");
  });
});

describe("Panel", () => {
  it("renders an h3 title and its actions", () => {
    renderUi(<Panel title="实验日志 (2)" actions={<Button size="small">刷新</Button>}>内容</Panel>);
    expect(screen.getByRole("heading", { level: 3, name: "实验日志 (2)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新" })).toBeInTheDocument();
    expect(screen.getByText("内容")).toBeInTheDocument();
  });
});

describe("KeyValueList", () => {
  it("renders labels and values and shows a dash for empty values", () => {
    renderUi(
      <KeyValueList
        items={[
          { label: "路径", value: "/work/exp-1", mono: true },
          { label: "分支", value: "" },
        ]}
      />,
    );
    expect(screen.getByText("路径")).toBeInTheDocument();
    expect(screen.getByText("/work/exp-1")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("EmptyState and CodeBlock", () => {
  it("shows the guidance message and preformatted code", () => {
    renderUi(
      <>
        <EmptyState icon="log" message="暂无实验记录。" />
        <CodeBlock>{"line 1\nline 2"}</CodeBlock>
      </>,
    );
    expect(screen.getByText("暂无实验记录。")).toBeInTheDocument();
    expect(screen.getByText(/line 1/).tagName).toBe("PRE");
  });
});
