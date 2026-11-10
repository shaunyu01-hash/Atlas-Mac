import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  RouterProvider,
  redirect,
  useNavigate,
} from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { supabase } from "./lib/supabase.ts";
import { CustomFieldsPage } from "./pages/CustomFieldsPage.tsx";
import { LoginPage } from "./pages/LoginPage.tsx";
import { SuppliersPage } from "./pages/SuppliersPage.tsx";
import "./styles.css";

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 1 } } });

async function requireSession() {
  const { data } = await supabase.auth.getSession();
  if (!data.session) throw redirect({ to: "/login" });
}

function Shell() {
  const navigate = useNavigate();
  const nav = "rounded px-2 py-1 text-muted hover:text-ink [&.active]:bg-canvas [&.active]:text-ink";
  return (
    <div className="min-h-screen">
      <header className="flex items-center gap-4 border-b border-line bg-surface px-4 py-2">
        <span className="font-semibold text-accent">Atlas PO</span>
        <nav className="flex gap-1">
          <Link to="/suppliers" className={nav}>
            Suppliers
          </Link>
          <Link to="/settings/fields" className={nav}>
            Custom fields
          </Link>
        </nav>
        <button
          type="button"
          className="ml-auto text-muted hover:text-ink"
          onClick={async () => {
            await supabase.auth.signOut();
            queryClient.clear();
            await navigate({ to: "/login" });
          }}
        >
          Sign out
        </button>
      </header>
      <main className="mx-auto max-w-7xl p-4">
        <Outlet />
      </main>
    </div>
  );
}

const rootRoute = createRootRoute({ component: Outlet });
const loginRoute = createRoute({ getParentRoute: () => rootRoute, path: "/login", component: LoginPage });
const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  beforeLoad: requireSession,
  component: Shell,
});
const indexRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/suppliers" });
  },
});
const suppliersRoute = createRoute({ getParentRoute: () => appRoute, path: "/suppliers", component: SuppliersPage });
const fieldsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/settings/fields",
  component: CustomFieldsPage,
});

const router = createRouter({
  routeTree: rootRoute.addChildren([loginRoute, appRoute.addChildren([indexRoute, suppliersRoute, fieldsRoute])]),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const root = document.getElementById("root");
if (!root) throw new Error("No #root element");
createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
