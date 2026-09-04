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
    expect(screen.getByText(/不会运行独立最终评估/)).toBeVisible();

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
});
