import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../services/api";
import { cx } from "../components/ui";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/repositories", label: "Repositories", end: false },
  { to: "/tasks", label: "Tasks", end: false },
  { to: "/governance", label: "Governance", end: false },
  { to: "/integrations", label: "Integrations", end: false },
  { to: "/operations", label: "Operations", end: false },
  { to: "/settings", label: "Settings & help", end: false },
];

export function Layout() {
  const ready = useQuery({ queryKey: ["ready"], queryFn: api.ready, refetchInterval: 30_000, retry: false });
  const state = ready.isPending ? "checking" : ready.isError ? "offline" : "online";
  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="bg-slate-900 text-slate-200 md:w-60 md:shrink-0">
        <div className="flex items-center gap-2 px-5 py-4">
          <span className="grid h-8 w-8 place-items-center rounded-md bg-sky-500/20 text-lg font-bold text-sky-300" aria-hidden>
            N
          </span>
          <div>
            <p className="text-base font-semibold leading-tight text-white">NOVA</p>
            <p className="text-xs text-slate-400">Engineering &amp; Verification</p>
          </div>
        </div>
        <nav aria-label="Primary" className="flex flex-wrap gap-1 px-3 pb-3 md:flex-col md:flex-nowrap md:pb-0">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cx(
                  "rounded-md px-3 py-2 text-sm font-medium",
                  isActive ? "bg-slate-800 text-white" : "text-slate-300 hover:bg-slate-800/60 hover:text-white",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <p className="hidden px-5 pt-6 text-xs leading-relaxed text-slate-400 md:block">
          Self-hosted. Repository files change and tests run only after an explicit approval.
        </p>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-end gap-2 border-b border-slate-200 bg-white px-6 py-2.5 text-xs text-slate-600">
          <span
            className={cx(
              "h-2 w-2 rounded-full",
              state === "online" ? "bg-emerald-500" : state === "offline" ? "bg-red-500" : "bg-slate-300",
            )}
            aria-hidden
          />
          <span aria-live="polite">
            API {state === "online" ? "ready" : state === "offline" ? "unavailable or not migrated" : "checking…"}
          </span>
        </header>
        <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
