import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_GUI_SETTINGS } from "../../lib/tauri-bridge";
import { SettingsPanel } from "../SettingsPanel";

const bridgeMocks = vi.hoisted(() => ({
  settingsGet: vi.fn(),
  settingsSet: vi.fn(),
  setProjectRoot: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  settingsGet: bridgeMocks.settingsGet,
  settingsSet: bridgeMocks.settingsSet,
  setProjectRoot: bridgeMocks.setProjectRoot,
}));

function settings(overrides: Record<string, unknown> = {}) {
  return {
    ...DEFAULT_GUI_SETTINGS,
    project_root: "C:/project",
    auto_validate: true,
    skip_validate: false,
    ...overrides,
  };
}

describe("SettingsPanel validation controls", () => {
  beforeEach(() => {
    bridgeMocks.settingsGet.mockReset();
    bridgeMocks.settingsSet.mockReset();
    bridgeMocks.setProjectRoot.mockReset();
  });

  it("saves skip validate while preserving the automatic validation choice", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());
    bridgeMocks.settingsSet.mockImplementation(async (patch) => settings(patch));

    render(<SettingsPanel onClose={vi.fn()} />);

    const skip = await screen.findByRole("checkbox", { name: "跳过 VALIDATE" });
    const automatic = screen.getByRole("checkbox", { name: "自动验证" });
    fireEvent.click(skip);

    expect(automatic).toBeDisabled();
    expect(automatic).toBeChecked();
    const help = screen.getByText(/SEARCH.*Final/);
    expect(skip).toHaveAttribute("aria-describedby", "skip-validate-help");
    expect(help.id).toBe("skip-validate-help");
    expect(help).toBeVisible();
    expect(help).toHaveTextContent("不会运行独立最终评估");
    expect(help).toHaveTextContent("不会产生最终测试或泛化指标");

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());
    const lastSettingsPatch = bridgeMocks.settingsSet.mock.calls[
      bridgeMocks.settingsSet.mock.calls.length - 1
    ]?.[0];
    expect(lastSettingsPatch).toMatchObject({
      auto_validate: true,
      skip_validate: true,
    });

    fireEvent.click(skip);
    expect(automatic).toBeEnabled();
    expect(automatic).toBeChecked();
  });

  it("renders a loaded skipped-validation snapshot", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings({ skip_validate: true }));

    render(<SettingsPanel onClose={vi.fn()} />);

    const skip = await screen.findByRole("checkbox", { name: "跳过 VALIDATE" });
    const automatic = screen.getByRole("checkbox", { name: "自动验证" });
    expect(skip).toBeChecked();
    expect(automatic).toBeDisabled();
    expect(automatic).toBeChecked();
    expect(screen.getByText(/SEARCH.*Final/)).toBeVisible();
  });

  it("does not submit a loaded masked API key", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings({
      model_connection: {
        ...DEFAULT_GUI_SETTINGS.model_connection,
        llm_api_key: "sec********alue",
      },
    }));
    bridgeMocks.settingsSet.mockImplementation(async (patch) => settings(patch));

    render(<SettingsPanel onClose={vi.fn()} />);
    await screen.findByText("C:/project");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());

    const calls = bridgeMocks.settingsSet.mock.calls;
    const patch = calls[calls.length - 1]?.[0];
    expect(patch.model_connection).not.toHaveProperty("llm_api_key");
  });

  it("submits an API key only after the user enters a new value", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings({
      model_connection: {
        ...DEFAULT_GUI_SETTINGS.model_connection,
        llm_api_key: "sec********alue",
      },
    }));
    bridgeMocks.settingsSet.mockImplementation(async (patch) => settings(patch));

    render(<SettingsPanel onClose={vi.fn()} />);
    await screen.findByText("C:/project");
    const input = screen.getByLabelText("API Key") as HTMLInputElement;
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveValue("");
    fireEvent.change(input, { target: { value: "new-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());

    const calls = bridgeMocks.settingsSet.mock.calls;
    const patch = calls[calls.length - 1]?.[0];
    expect(patch.model_connection).toMatchObject({ llm_api_key: "new-secret" });
  });

  it("keeps spaces inside the dataset path and trims it on save", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());
    bridgeMocks.settingsSet.mockImplementation(async (patch) => settings(patch));

    render(<SettingsPanel onClose={vi.fn()} />);
    await screen.findByText("C:/project");
    const dataRoot = screen.getByRole("textbox", { name: /数据集根目录/ });
    fireEvent.change(dataRoot, { target: { value: "D:/My Data " } });
    expect(dataRoot).toHaveValue("D:/My Data ");

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());
    expect(bridgeMocks.settingsSet.mock.calls[0][0]).toMatchObject({ data_root: "D:/My Data" });
  });

  it("blocks saving invalid numbers and explains why", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());

    render(<SettingsPanel onClose={vi.fn()} />);
    await screen.findByText("C:/project");
    fireEvent.change(screen.getByRole("spinbutton", { name: /并发数/ }), { target: { value: "0" } });

    expect(screen.getByText("请输入不小于 1 的整数")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  });

  it("switches projects through the workspace picker instead of a raw path field", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());
    const onSwitchWorkspace = vi.fn();

    render(<SettingsPanel onClose={vi.fn()} onSwitchWorkspace={onSwitchWorkspace} />);
    await screen.findByText("C:/project");
    fireEvent.click(screen.getByRole("button", { name: "切换工作区" }));

    expect(onSwitchWorkspace).toHaveBeenCalledOnce();
    expect(bridgeMocks.setProjectRoot).not.toHaveBeenCalled();
  });
});
