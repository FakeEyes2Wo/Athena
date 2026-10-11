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
    expect(skip).toHaveAccessibleDescription(/不会运行独立最终评估/);
    expect(skip).toHaveAccessibleDescription(/不会产生最终测试或泛化指标/);
    expect(help).toBeVisible();

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
    await screen.findByDisplayValue("C:/project");
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
    await screen.findByDisplayValue("C:/project");
    // 弹窗经 Portal 渲染，不在 render() 返回的 container 内。
    const input = document.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(input, { target: { value: "new-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());

    const calls = bridgeMocks.settingsSet.mock.calls;
    const patch = calls[calls.length - 1]?.[0];
    expect(patch.model_connection).toMatchObject({ llm_api_key: "new-secret" });
  });

  it("closes on Escape", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());
    const onClose = vi.fn();

    render(<SettingsPanel onClose={onClose} />);
    const dialog = await screen.findByRole("dialog", { name: "运行设置" });
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the dialog open and shows the error when saving fails", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());
    bridgeMocks.settingsSet.mockRejectedValue(new Error("settings write failed"));
    const onClose = vi.fn();

    render(<SettingsPanel onClose={onClose} />);
    await screen.findByDisplayValue("C:/project");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByText("settings write failed")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "运行设置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
