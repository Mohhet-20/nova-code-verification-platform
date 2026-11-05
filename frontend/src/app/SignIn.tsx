import { useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, describeError, guestAccessEnabled, setStoredToken } from "../services/api";
import { Button, Field, TextInput } from "../components/ui";

/**
 * Shown by the layout whenever the API refuses a request for lack of a valid token. On the hosted demo it offers a
 * guest session (an engineer in the demo workspace, never the owner); an access key always works as well: the
 * deployment's owner key or a personal token issued from Governance.
 */
export function SignIn() {
  const queryClient = useQueryClient();
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState<"guest" | "key" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const guest = guestAccessEnabled();

  async function enter(token: string) {
    setStoredToken(token);
    await queryClient.resetQueries();
  }

  async function exploreDemo() {
    setBusy("guest");
    setError(null);
    try {
      await enter((await api.guestSession()).token);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  async function signInWithKey(e: FormEvent) {
    e.preventDefault();
    const token = key.trim();
    if (!token) return;
    setBusy("key");
    setError(null);
    try {
      await api.verifyKey(token);
      await enter(token);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid min-h-full place-items-center bg-slate-50 px-4 py-12">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-md bg-slate-900 text-xl font-bold text-sky-300" aria-hidden>
            N
          </span>
          <div>
            <h1 className="text-lg font-semibold leading-tight text-slate-900">Sign in to NOVA</h1>
            <p className="text-sm text-slate-600">Engineering &amp; Verification Platform</p>
          </div>
        </div>
        <div className="space-y-5 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          {guest && (
            <div className="space-y-3">
              <Button className="w-full" onClick={() => void exploreDemo()} disabled={busy !== null}>
                {busy === "guest" ? "Starting your session…" : "Explore the live demo"}
              </Button>
              <p className="text-xs leading-relaxed text-slate-600">
                Opens NOVA&apos;s own backend source, already indexed, analysed, security scanned and reviewed. You join as an
                engineer: browse everything, start analyses and reviews, draft repairs. Running repository code is switched off
                on this public deployment, and the demo resets when the service restarts.
              </p>
              <div className="flex items-center gap-3 text-xs text-slate-400" aria-hidden>
                <span className="h-px flex-1 bg-slate-200" />
                or
                <span className="h-px flex-1 bg-slate-200" />
              </div>
            </div>
          )}
          <form onSubmit={(e) => void signInWithKey(e)} className="space-y-3">
            <Field label="Access key" htmlFor="signin-key" hint="The deployment's owner key, or a personal token issued from Governance.">
              <TextInput
                id="signin-key"
                type="password"
                autoComplete="current-password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
            </Field>
            <Button type="submit" variant={guest ? "secondary" : "primary"} className="w-full" disabled={busy !== null || !key.trim()}>
              {busy === "key" ? "Signing in…" : "Sign in"}
            </Button>
          </form>
          {error && (
            <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800">
              {error}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
