import { isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

/** Opens the desktop-native single-directory picker when running in Tauri. */
export async function selectWorkspaceDirectory(
  defaultPath?: string | null,
): Promise<string | null> {
  if (!isTauri()) return null;

  const selected = await open({
    directory: true,
    multiple: false,
    ...(defaultPath ? { defaultPath } : {}),
  });
  return typeof selected === "string" ? selected : null;
}
