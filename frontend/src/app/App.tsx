import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider, type RouteObject } from "react-router-dom";
import { ApiError } from "../services/api";
import { ErrorBoundary } from "./ErrorBoundary";
import { Layout } from "./Layout";

/** Route-level code splitting: each page is its own chunk, fetched on first navigation to it. */
function page<M extends Record<string, unknown>>(load: () => Promise<M>, name: keyof M & string): NonNullable<RouteObject["lazy"]> {
  return async () => ({ Component: (await load())[name] as React.ComponentType });
}

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    HydrateFallback: () => <p role="status" className="p-6 text-sm text-slate-600">Loading…</p>,
    children: [
      { path: "/", lazy: page(() => import("../pages/DashboardPage"), "DashboardPage") },
      { path: "/repositories", lazy: page(() => import("../pages/RepositoriesPage"), "RepositoriesPage") },
      { path: "/repositories/new", lazy: page(() => import("../pages/RegisterRepositoryPage"), "RegisterRepositoryPage") },
      { path: "/repositories/:repositoryId", lazy: page(() => import("../pages/RepositoryDetailPage"), "RepositoryDetailPage") },
      { path: "/repositories/:repositoryId/intelligence", lazy: page(() => import("../pages/IntelligencePage"), "IntelligencePage") },
      { path: "/repositories/:repositoryId/health", lazy: page(() => import("../pages/HealthPage"), "HealthPage") },
      { path: "/repositories/:repositoryId/testing", lazy: page(() => import("../pages/TestingPage"), "TestingPage") },
      { path: "/repositories/:repositoryId/failures", lazy: page(() => import("../pages/FailuresPage"), "FailuresPage") },
      { path: "/repositories/:repositoryId/repairs", lazy: page(() => import("../pages/RepairsPage"), "RepairsPage") },
      { path: "/repositories/:repositoryId/repairs/:repairId", lazy: page(() => import("../pages/RepairsPage"), "RepairDetailPage") },
      { path: "/governance", lazy: page(() => import("../pages/GovernanceHomePage"), "GovernanceHomePage") },
      { path: "/governance/approvals", lazy: page(() => import("../pages/GovernanceQueuePages"), "GovernanceApprovalsPage") },
      { path: "/governance/queue", lazy: page(() => import("../pages/GovernanceQueuePages"), "GovernanceQueuePage") },
      { path: "/governance/workflows/:workflowId", lazy: page(() => import("../pages/GovernanceWorkflowPage"), "GovernanceWorkflowPage") },
      { path: "/governance/teams", lazy: page(() => import("../pages/GovernanceTeamsPages"), "GovernanceTeamsPage") },
      { path: "/governance/teams/:teamId", lazy: page(() => import("../pages/GovernanceTeamsPages"), "GovernanceTeamDetailPage") },
      { path: "/governance/policies", lazy: page(() => import("../pages/GovernancePoliciesPage"), "GovernancePoliciesPage") },
      { path: "/governance/audit", lazy: page(() => import("../pages/GovernanceAuditPage"), "GovernanceAuditPage") },
      { path: "/integrations", lazy: page(() => import("../pages/IntegrationsPages"), "IntegrationsHomePage") },
      { path: "/integrations/new", lazy: page(() => import("../pages/IntegrationsPages"), "IntegrationNewPage") },
      { path: "/integrations/webhooks", lazy: page(() => import("../pages/IntegrationsWebhooksPage"), "IntegrationWebhooksPage") },
      { path: "/integrations/ci-evidence", lazy: page(() => import("../pages/IntegrationsPages"), "IntegrationCiEvidencePage") },
      { path: "/integrations/extensions", lazy: page(() => import("../pages/IntegrationsPages"), "IntegrationExtensionsPage") },
      { path: "/integrations/:installationId", lazy: page(() => import("../pages/IntegrationDetailPage"), "IntegrationDetailPage") },
      { path: "/operations", lazy: page(() => import("../pages/OperationsPage"), "OperationsPage") },
      { path: "/repositories/:repositoryId/security", lazy: page(() => import("../pages/SecurityPage"), "SecurityPage") },
      { path: "/repositories/:repositoryId/workflow", lazy: page(() => import("../pages/WorkflowCenterPage"), "WorkflowCenterPage") },
      { path: "/repositories/:repositoryId/workflow/change-requests/:crId", lazy: page(() => import("../pages/ChangeRequestPage"), "ChangeRequestPage") },
      { path: "/repositories/:repositoryId/verification", lazy: page(() => import("../pages/VerificationPage"), "VerificationPage") },
      { path: "/repositories/:repositoryId/verification/:runId", lazy: page(() => import("../pages/VerificationRunPage"), "VerificationRunPage") },
      { path: "/repositories/:repositoryId/refactors", lazy: page(() => import("../pages/RefactorPage"), "RefactorPage") },
      { path: "/repositories/:repositoryId/refactors/:refactorId", lazy: page(() => import("../pages/RefactorDetailPage"), "RefactorDetailPage") },
      { path: "/repositories/:repositoryId/reviews", lazy: page(() => import("../pages/ReviewsPage"), "ReviewsPage") },
      { path: "/repositories/:repositoryId/reviews/:reviewId", lazy: page(() => import("../pages/ReviewsPage"), "ReviewDetailPage") },
      { path: "/repositories/:repositoryId/failures/:failureId", lazy: page(() => import("../pages/FailuresPage"), "FailureDetailPage") },
      { path: "/repositories/:repositoryId/files/:fileId", lazy: page(() => import("../pages/FileDetailPage"), "FileDetailPage") },
      { path: "/repositories/:repositoryId/indexing/:snapshotId", lazy: page(() => import("../pages/IndexingPage"), "IndexingPage") },
      { path: "/tasks", lazy: page(() => import("../pages/TasksPage"), "TasksPage") },
      { path: "/tasks/new", lazy: page(() => import("../pages/NewTaskPage"), "NewTaskPage") },
      { path: "/tasks/:taskId", lazy: page(() => import("../pages/TaskDetailPage"), "TaskDetailPage") },
      { path: "/settings", lazy: page(() => import("../pages/SettingsPage"), "SettingsPage") },
      { path: "*", lazy: page(() => import("../pages/NotFoundPage"), "NotFoundPage") },
    ],
  },
];

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: false,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
  });
}

const queryClient = makeQueryClient();
const router = createBrowserRouter(routes);

export function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
