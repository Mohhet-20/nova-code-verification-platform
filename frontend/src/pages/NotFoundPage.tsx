import { ButtonLink, EmptyState } from "../components/ui";

export function NotFoundPage() {
  return (
    <EmptyState title="Page not found" action={<ButtonLink to="/">Back to overview</ButtonLink>}>
      The page you requested does not exist.
    </EmptyState>
  );
}
