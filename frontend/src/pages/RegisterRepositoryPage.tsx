import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button, ButtonLink, Card, Field, Notice, PageHeader, TextArea, TextInput } from "../components/ui";
import { api, describeError } from "../services/api";

export function RegisterRepositoryPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [description, setDescription] = useState("");

  const register = useMutation({
    mutationFn: api.registerRepository,
    onSuccess: async (repo) => {
      await qc.invalidateQueries({ queryKey: ["repositories"] });
      await qc.invalidateQueries({ queryKey: ["overview"] });
      void navigate(`/repositories/${repo.id}`);
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    register.mutate({ name: name.trim(), root_path: path.trim(), description: description.trim() });
  };

  return (
    <>
      <PageHeader title="Register repository" subtitle="Point NOVA at a directory on the machine that runs the backend." />
      <div className="grid gap-6 lg:grid-cols-3">
        <form onSubmit={submit} className="space-y-5 lg:col-span-2" aria-label="Register repository">
          <Card>
            <div className="space-y-5">
              <Field label="Display name" htmlFor="repo-name">
                <TextInput id="repo-name" required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} placeholder="payments-service" />
              </Field>
              <Field
                label="Absolute path on the backend machine"
                htmlFor="repo-path"
                hint="Windows example: C:\Projects\my-repo · macOS/Linux example: /home/me/projects/my-repo"
              >
                <TextInput id="repo-path" required className="font-mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder="C:\Projects\my-repo" />
              </Field>
              <Field label="Description (optional)" htmlFor="repo-desc">
                <TextArea id="repo-desc" rows={2} maxLength={1000} value={description} onChange={(e) => setDescription(e.target.value)} />
              </Field>
              {register.isError && <Notice tone="danger">{describeError(register.error)}</Notice>}
              <div className="flex gap-2">
                <Button type="submit" disabled={register.isPending || !name.trim() || !path.trim()}>
                  {register.isPending ? "Registering…" : "Register"}
                </Button>
                <ButtonLink to="/repositories" variant="secondary">Cancel</ButtonLink>
              </div>
            </div>
          </Card>
        </form>
        <Card title="Why a typed path?">
          <p className="text-sm leading-relaxed text-slate-700">
            Browsers cannot safely hand a web page an arbitrary local directory path, so registration happens on the
            backend: you provide the path and the server validates it, resolves it to its canonical location, and refuses
            files, missing paths, filesystem roots and home directories.
          </p>
          <p className="mt-3 text-sm leading-relaxed text-slate-700">
            Credentials (<code>.env</code>, keys), dependency folders, binaries and gitignored files are never indexed.
            Nothing leaves your machine.
          </p>
        </Card>
      </div>
    </>
  );
}
