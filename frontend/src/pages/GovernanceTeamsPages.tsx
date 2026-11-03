import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Button, Card, ConfirmDialog, EmptyState, Field, Notice, PageHeader, Pagination, TextArea, TextInput } from "../components/ui";
import { describeError } from "../services/api";
import { govApi } from "../services/governanceApi";
import type { TeamRole } from "../types/governance";
import { GovernanceNav, Guard, can, useMe } from "./GovernanceShared";

export function GovernanceTeamsPage() {
  const me = useMe();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const q = useQuery({ queryKey: ["gov-teams", search, page], queryFn: () => govApi.teams({ q: search || undefined, page }), placeholderData: (prev) => prev });
  const create = useMutation({
    mutationFn: () => govApi.createTeam(name.trim(), description.trim()),
    onSuccess: async () => { setCreating(false); setName(""); setDescription(""); await qc.invalidateQueries({ queryKey: ["gov-teams"] }); },
  });
  return (
    <>
      <GovernanceNav />
      <PageHeader title="Teams" subtitle="Teams own repositories and review work. Members only see their own teams' repositories; organization administrators, leads and auditors see all." actions={can(me.data, "team.create") ? <Button onClick={() => { create.reset(); setCreating(true); }}>New team</Button> : undefined} />
      <div className="mb-4 max-w-sm"><Field label="Search teams" htmlFor="team-search"><TextInput id="team-search" type="search" value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} /></Field></div>
      <Guard query={q} what="teams">
        {(data) => data.items.length === 0 ? <EmptyState title="No teams">{search ? "No team matches this search." : "No teams exist yet, or you are not a member of any."}</EmptyState> : (
          <>
            <ul className="grid gap-3 sm:grid-cols-2" aria-label="Teams">
              {data.items.map((t) => (
                <li key={t.id} className="rounded-lg border border-slate-200 bg-white p-4">
                  <div className="flex items-start justify-between gap-2"><Link to={`/governance/teams/${t.id}`} className="font-semibold text-sky-900 underline">{t.name}</Link>{t.status === "archived" ? <Badge tone="warning">archived</Badge> : t.my_role ? <Badge tone="info">you: {t.my_role}</Badge> : null}</div>
                  <p className="mt-1 text-sm text-slate-600">{t.description || "No description."}</p>
                  <p className="mt-2 text-xs text-slate-500">{t.member_count} member(s)</p>
                </li>
              ))}
            </ul>
            <Pagination page={data.page} pages={data.pages} total={data.total} onPage={setPage} />
          </>
        )}
      </Guard>
      {creating && (
        <ConfirmDialog title="Create team" confirmLabel="Create team" busy={create.isPending} onConfirm={() => name.trim() && create.mutate()} onCancel={() => setCreating(false)} error={create.isError ? describeError(create.error) : undefined}>
          <Field label="Name" htmlFor="new-team-name"><TextInput id="new-team-name" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} required /></Field>
          <Field label="Description" htmlFor="new-team-desc"><TextArea id="new-team-desc" rows={2} maxLength={1000} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        </ConfirmDialog>
      )}
    </>
  );
}

export function GovernanceTeamDetailPage() {
  const { teamId = "" } = useParams();
  const me = useMe();
  const qc = useQueryClient();
  const team = useQuery({ queryKey: ["gov-team", teamId], queryFn: () => govApi.team(teamId) });
  const members = useQuery({ queryKey: ["gov-team-members", teamId], queryFn: () => govApi.teamMembers(teamId), enabled: team.isSuccess });
  const owned = useQuery({ queryKey: ["gov-team-owned", teamId], queryFn: () => govApi.ownership(teamId), enabled: team.isSuccess });
  const work = useQuery({ queryKey: ["gov-team-work", teamId], queryFn: () => govApi.teamWork(teamId), enabled: team.isSuccess });
  const org = useQuery({ queryKey: ["gov-org-members"], queryFn: () => govApi.orgMembers(), enabled: team.isSuccess && can(me.data, "team.members.manage", teamId) });
  const [removing, setRemoving] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newUser, setNewUser] = useState("");
  const [newRole, setNewRole] = useState<TeamRole>("member");
  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: ["gov-team-members", teamId] }), qc.invalidateQueries({ queryKey: ["gov-team", teamId] })]);
  const remove = useMutation({ mutationFn: (uid: string) => govApi.removeTeamMember(teamId, uid), onSuccess: async () => { setRemoving(null); await refresh(); } });
  const add = useMutation({ mutationFn: () => govApi.addTeamMember(teamId, newUser, newRole), onSuccess: async () => { setAdding(false); setNewUser(""); await refresh(); } });
  const manage = can(me.data, "team.members.manage", teamId);
  const memberIds = new Set(members.data?.items.map((m) => m.user_id));
  return (
    <>
      <GovernanceNav />
      <Guard query={team} what="this team">
        {(t) => (
          <div className="space-y-5">
            <PageHeader title={t.name} subtitle={t.description || "No description."} actions={t.status === "archived" ? <Badge tone="warning">archived</Badge> : undefined} />
            <Card title="Work in progress">
              {work.isPending ? <p className="text-sm">Loading…</p> : work.isError ? <Notice tone="danger">{describeError(work.error)}</Notice> : (
                <dl className="grid gap-3 sm:grid-cols-4 text-sm">
                  <div><dt className="text-slate-500">Repositories</dt><dd className="text-lg font-semibold">{work.data.owned_repositories}</dd></div>
                  <div><dt className="text-slate-500">Awaiting review</dt><dd className="text-lg font-semibold">{work.data.workflows_by_state.IN_REVIEW ?? 0}</dd></div>
                  <div><dt className="text-slate-500">Unassigned in queue</dt><dd className="text-lg font-semibold">{work.data.queue_unassigned}</dd></div>
                  <div><dt className="text-slate-500">Outstanding approvals</dt><dd className="text-lg font-semibold">{work.data.outstanding_approvals}</dd></div>
                </dl>
              )}
            </Card>
            <Card title="Members" actions={manage && t.status === "active" ? <Button variant="secondary" onClick={() => { add.reset(); setAdding(true); }}>Add member</Button> : undefined}>
              {members.isPending ? <p className="text-sm">Loading…</p> : members.isError ? <Notice tone="danger">{describeError(members.error)}</Notice> : members.data.items.length === 0 ? <EmptyState title="No members" /> : (
                <ul className="divide-y divide-slate-100" aria-label="Team members">
                  {members.data.items.map((m) => (
                    <li key={m.user_id} className="flex items-center justify-between gap-3 py-2 text-sm">
                      <span><strong>{m.display_name || m.handle}</strong> <span className="text-slate-500">@{m.handle}</span></span>
                      <span className="flex items-center gap-2"><Badge tone={m.role === "admin" ? "info" : "neutral"}>{m.role}</Badge>{manage && <Button variant="ghost" onClick={() => { remove.reset(); setRemoving(m.user_id); }}>Remove</Button>}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Owned repositories">
              {owned.isPending ? <p className="text-sm">Loading…</p> : owned.isError ? <Notice tone="danger">{describeError(owned.error)}</Notice> : owned.data.items.length === 0 ? <p className="text-sm text-slate-600">This team does not own any repository yet.</p> : (
                <ul className="space-y-1 text-sm" aria-label="Owned repositories">{owned.data.items.map((o) => <li key={o.repository_id}><Link className="text-sky-900 underline" to={`/repositories/${o.repository_id}`}>{o.repository_name}</Link>{o.accountable_handle ? ` · accountable: @${o.accountable_handle}` : ""}</li>)}</ul>
              )}
            </Card>
          </div>
        )}
      </Guard>
      {removing && (
        <ConfirmDialog title="Remove from team?" confirmLabel="Remove member" tone="danger" busy={remove.isPending} onConfirm={() => remove.mutate(removing)} onCancel={() => setRemoving(null)} error={remove.isError ? describeError(remove.error) : undefined}>
          <p>They lose access to this team's repositories and review work immediately. Their past decisions stay in the history.</p>
        </ConfirmDialog>
      )}
      {adding && (
        <ConfirmDialog title="Add team member" confirmLabel="Add member" busy={add.isPending} onConfirm={() => newUser && add.mutate()} onCancel={() => setAdding(false)} error={add.isError ? describeError(add.error) : undefined}>
          <Field label="Organization member" htmlFor="add-user">
            <select id="add-user" className="block w-full rounded-md border border-slate-300 px-2 py-2 text-sm" value={newUser} onChange={(e) => setNewUser(e.target.value)}>
              <option value="">Select a person…</option>
              {org.data?.items.filter((m) => !memberIds.has(m.user_id)).map((m) => <option key={m.user_id} value={m.user_id}>{m.display_name || m.handle} (@{m.handle})</option>)}
            </select>
          </Field>
          <Field label="Team role" htmlFor="add-role">
            <select id="add-role" className="block w-full rounded-md border border-slate-300 px-2 py-2 text-sm" value={newRole} onChange={(e) => setNewRole(e.target.value as TeamRole)}>
              <option value="member">Member</option><option value="reviewer">Reviewer</option><option value="admin">Admin</option>
            </select>
          </Field>
          {org.isError && <p className="text-xs text-amber-800">The organization member list is not available to your role: {describeError(org.error)}</p>}
        </ConfirmDialog>
      )}
    </>
  );
}
