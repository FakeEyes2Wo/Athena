import type { ReactElement } from "react";
import { render } from "@testing-library/react";

export function renderUi(ui: ReactElement) {
  return render(ui);
}
