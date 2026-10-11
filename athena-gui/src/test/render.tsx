import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { AppProviders } from "../components/common/AppProviders";

/** 在应用根 Provider（主题 + Primer）内渲染组件。 */
export function renderUi(ui: ReactElement) {
  return render(ui, { wrapper: AppProviders });
}
