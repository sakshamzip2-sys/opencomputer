import { createContext, useCallback, useContext, useState } from "react";

export interface Toast { id: number; kind: "info" | "ok" | "warn" | "err"; text: string; }

interface Ctx {
  toasts: Toast[];
  push: (kind: Toast["kind"], text: string) => void;
  remove: (id: number) => void;
}

const ToastContext = createContext<Ctx | null>(null);

export function useToast(): Ctx {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast outside ToastProvider");
  return ctx;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const remove = useCallback(
    (id: number) => setToasts((t) => t.filter((x) => x.id !== id)),
    [],
  );
  const push = useCallback(
    (kind: Toast["kind"], text: string) => {
      const id = Date.now() + Math.random();
      setToasts((t) => [...t, { id, kind, text }]);
      setTimeout(() => remove(id), 5000);
    },
    [remove],
  );

  return (
    <ToastContext.Provider value={{ toasts, push, remove }}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            onClick={() => remove(t.id)}
            className={`min-w-[200px] cursor-pointer rounded border px-3 py-2 text-sm shadow-lg ${
              t.kind === "ok"
                ? "border-green-800 bg-green-950/80 text-green-200"
                : t.kind === "warn"
                ? "border-amber-800 bg-amber-950/80 text-amber-200"
                : t.kind === "err"
                ? "border-red-800 bg-red-950/80 text-red-200"
                : "border-zinc-700 bg-zinc-900/90 text-zinc-200"
            }`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
