import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { describeError } from "../services/api";
import { govApi } from "../services/governanceApi";
import { Button, ConfirmDialog } from "./ui";

/** Starts a governed approval workflow for a record. The server decides whether the caller may, and which definition and policies apply. */
export function RequestGovernanceReview({ resourceType, resourceId, label = "Request governance review" }: { resourceType: string; resourceId: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const create = useMutation({
    mutationFn: () => govApi.createWorkflow(resourceType, resourceId),
    onSuccess: (wf) => navigate(`/governance/workflows/${wf.id}`),
  });
  return (
    <>
      <Button variant="secondary" onClick={() => { create.reset(); setOpen(true); }}>{label}</Button>
      {open && (
        <ConfirmDialog title="Request governance review?" confirmLabel="Create workflow" busy={create.isPending} onConfirm={() => create.mutate()} onCancel={() => setOpen(false)} error={create.isError ? describeError(create.error) : undefined}>
          <p>This creates a draft approval workflow for this record. You can then submit it to the review queue, where an independent reviewer must decide.</p>
          <p>Nothing is approved by creating it, and you cannot approve your own request.</p>
        </ConfirmDialog>
      )}
    </>
  );
}
