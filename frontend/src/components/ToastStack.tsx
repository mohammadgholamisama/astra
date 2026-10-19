import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, X, XCircle } from "lucide-react";

export type ToastKind = "ok" | "err";

export type ToastItem = {
  id: number;
  type: ToastKind;
  text: string;
};

type UseToastStackOptions = {
  /** Auto-dismiss delay in ms */
  ttlMs?: number;
  /** Max visible toasts; older ones drop first */
  max?: number;
};

export function useToastStack(options: UseToastStackOptions = {}) {
  const { ttlMs = 4200, max = 6 } = options;
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seq = useRef(1);
  const timers = useRef<Map<number, number>>(new Map());

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer != null) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (type: ToastKind, text: string) => {
      const trimmed = (text || "").trim();
      if (!trimmed) return;
      const id = seq.current++;
      setToasts((prev) => {
        const next = [...prev, { id, type, text: trimmed }];
        if (next.length <= max) return next;
        const dropped = next.slice(0, next.length - max);
        for (const t of dropped) {
          const timer = timers.current.get(t.id);
          if (timer != null) {
            window.clearTimeout(timer);
            timers.current.delete(t.id);
          }
        }
        return next.slice(-max);
      });
      const timer = window.setTimeout(() => dismiss(id), ttlMs);
      timers.current.set(id, timer);
    },
    [dismiss, max, ttlMs]
  );

  useEffect(
    () => () => {
      for (const timer of timers.current.values()) {
        window.clearTimeout(timer);
      }
      timers.current.clear();
    },
    []
  );

  return { toasts, push, dismiss };
}

export function ToastStack({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[];
  onDismiss: (id: number) => void;
}) {
  if (!toasts.length) return null;
  return (
    <div className="toast-stack" aria-live="polite" aria-relevant="additions">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast-stack-item ${t.type}`}
          role={t.type === "err" ? "alert" : "status"}
        >
          <span className="toast-stack-ico" aria-hidden>
            {t.type === "ok" ? (
              <CheckCircle2 size={18} strokeWidth={2.2} />
            ) : (
              <XCircle size={18} strokeWidth={2.2} />
            )}
          </span>
          <p className="toast-stack-text">{t.text}</p>
          <button
            type="button"
            className="toast-stack-close"
            aria-label="بستن"
            onClick={() => onDismiss(t.id)}
          >
            <X size={15} strokeWidth={2.25} />
          </button>
        </div>
      ))}
    </div>
  );
}
