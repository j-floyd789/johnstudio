// Small primitive components — local-owned, shadcn-shaped.
import React from "react";

type Cn = (...c: (string | false | null | undefined)[]) => string;
export const cn: Cn = (...c) => c.filter(Boolean).join(" ");

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("card", className)}>{children}</div>;
}

export function Button({
  variant = "ghost",
  disabled,
  onClick,
  children,
  title,
  className,
  type = "button",
}: {
  variant?: "primary" | "ghost" | "danger";
  disabled?: boolean;
  onClick?: () => void;
  children: React.ReactNode;
  title?: string;
  className?: string;
  type?: "button" | "submit";
}) {
  const cls =
    variant === "primary" ? "btn-primary" : variant === "danger" ? "btn-danger" : "btn-ghost";
  return (
    <button
      type={type}
      className={cn(cls, disabled && "opacity-40 cursor-not-allowed", className)}
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  );
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn("input", props.className)} />;
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn("input min-h-[80px]", props.className)} />;
}

export function Badge({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "ok" | "warn" | "bad" | "accent";
  children: React.ReactNode;
}) {
  const map = {
    neutral: "text-ink-2 border-line",
    ok: "text-ok border-ok/40 bg-ok/5",
    warn: "text-warn border-warn/40 bg-warn/5",
    bad: "text-bad border-bad/40 bg-bad/5",
    accent: "text-accent border-accent/40 bg-accent/5",
  };
  return <span className={cn("badge", map[tone])}>{children}</span>;
}

export function Empty({
  title,
  body,
  cta,
}: {
  title: string;
  body?: string;
  cta?: React.ReactNode;
}) {
  return (
    <div className="card text-center py-10 text-ink-2">
      <div className="text-ink-1 font-medium mb-1">{title}</div>
      {body && <div className="text-sm">{body}</div>}
      {cta && <div className="mt-4 flex justify-center">{cta}</div>}
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="card max-w-xl w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-base font-semibold">{title}</h3>
          <button onClick={onClose} className="text-ink-3 hover:text-ink-0">
            ✕
          </button>
        </div>
        <div className="text-sm text-ink-1">{children}</div>
        {footer && <div className="mt-4 flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  );
}

// ---------- Toasts ----------

type Toast = { id: number; kind: "ok" | "warn" | "bad"; msg: string };
const ToastCtx = React.createContext<{
  push: (kind: Toast["kind"], msg: string) => void;
}>({ push: () => {} });

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<Toast[]>([]);
  const push = React.useCallback((kind: Toast["kind"], msg: string) => {
    setItems((arr) => [...arr, { id: Date.now() + Math.random(), kind, msg }]);
  }, []);
  React.useEffect(() => {
    if (!items.length) return;
    const t = setTimeout(() => setItems((arr) => arr.slice(1)), 4000);
    return () => clearTimeout(t);
  }, [items]);
  return (
    <ToastCtx.Provider value={{ push }}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
        {items.map((t) => (
          <div
            key={t.id}
            className={cn(
              "card text-sm",
              t.kind === "ok" && "border-ok/40",
              t.kind === "warn" && "border-warn/40",
              t.kind === "bad" && "border-bad/40",
            )}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  return React.useContext(ToastCtx);
}

// Tabs ------------------------------------------------------------------

export function Tabs({
  value,
  onChange,
  items,
}: {
  value: string;
  onChange: (v: string) => void;
  items: { value: string; label: string }[];
}) {
  return (
    <div className="border-b border-line flex gap-1 overflow-x-auto">
      {items.map((it) => (
        <button
          key={it.value}
          onClick={() => onChange(it.value)}
          className={cn(
            "px-3 py-2 text-sm border-b-2 -mb-px transition-colors",
            it.value === value
              ? "border-accent text-ink-0"
              : "border-transparent text-ink-2 hover:text-ink-0",
          )}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}

// CodeBlock ----------------------------------------------------------------

export function CodeBlock({ children, className }: { children: string; className?: string }) {
  return (
    <pre
      className={cn(
        "card bg-bg-1 overflow-x-auto text-xs leading-relaxed whitespace-pre",
        className,
      )}
    >
      <code>{children}</code>
    </pre>
  );
}
