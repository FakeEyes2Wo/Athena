import { useCallback, useRef, useState, type ReactNode } from "react";
import { Dialog } from "./Dialog";

export interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "danger" | "default";
}

/**
 * Promise-based confirmation dialog.
 *
 * ``const [confirm, confirmDialog] = useConfirm();`` then render ``confirmDialog``
 * and ``if (await confirm({...})) doIt();``.
 */
export function useConfirm(): [(options: ConfirmOptions) => Promise<boolean>, ReactNode] {
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const resolverRef = useRef<((value: boolean) => void) | null>(null);

  const settle = useCallback((value: boolean) => {
    resolverRef.current?.(value);
    resolverRef.current = null;
    setOptions(null);
  }, []);

  const confirm = useCallback((next: ConfirmOptions) => {
    resolverRef.current?.(false);
    setOptions(next);
    return new Promise<boolean>((resolve) => {
      resolverRef.current = resolve;
    });
  }, []);

  const dialog = (
    <Dialog
      open={options !== null}
      size="sm"
      title={options?.title ?? ""}
      description={options?.message}
      onClose={() => settle(false)}
      hideClose
      footer={
        <>
          <button type="button" className="btn" onClick={() => settle(false)}>
            {options?.cancelLabel ?? "取消"}
          </button>
          <button
            type="button"
            className={options?.tone === "danger" ? "btn btn--danger" : "btn btn--primary"}
            onClick={() => settle(true)}
            data-autofocus
          >
            {options?.confirmLabel ?? "确定"}
          </button>
        </>
      }
    />
  );

  return [confirm, dialog];
}
