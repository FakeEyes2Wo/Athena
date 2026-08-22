/** Ensure Tauri generated schema directories exist before dev/build. */
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "src-tauri");
const dirs = [
  resolve(root, "gen", "schemas"),
  resolve(root, "gen", "android"),
  resolve(root, "gen", "ios"),
];

for (const dir of dirs) {
  mkdirSync(dir, { recursive: true });
}
