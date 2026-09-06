import { invoke, isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { wsBackend } from "./ws-backend";

export interface WorkspaceDirectory {
  name: string;
  path: string;
}

export interface WorkspaceDirectoryListing {
  path: string;
  parent: string | null;
  directories: WorkspaceDirectory[];
}

/** Lists host directories through the local browser gateway. */
export function listWorkspaceDirectories(
  path?: string | null,
): Promise<WorkspaceDirectoryListing> {
  return wsBackend.call("workspace_directories", { path: path ?? null }) as Promise<WorkspaceDirectoryListing>;
}

/** Opens the desktop-native single-directory picker when running in Tauri. */
export async function selectWorkspaceDirectory(
  defaultPath?: string | null,
): Promise<string | null> {
  if (!isTauri()) return null;

  const safeDefaultPath = await invoke<string | null>(
    "workspace_dialog_start_directory",
    { requested: defaultPath ?? null },
  );
  const selected = await open({
    directory: true,
    multiple: false,
    ...(safeDefaultPath ? { defaultPath: safeDefaultPath } : {}),
  });
  return typeof selected === "string" ? selected : null;
}
