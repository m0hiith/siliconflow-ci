import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { ApiError } from "./api/client";
import { Layout } from "./components/Layout";
import { EmptyState } from "./components/States";
import { Analytics } from "./pages/Analytics";
import { BuildDetail } from "./pages/BuildDetail";
import { Builds } from "./pages/Builds";
import { Overview } from "./pages/Overview";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A 4xx will not fix itself; retry only server and network errors.
      retry: (failures, error) =>
        !(error instanceof ApiError && error.status < 500) && failures < 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Overview />} />
            <Route path="builds" element={<Builds />} />
            <Route path="builds/:buildId" element={<BuildDetail />} />
            <Route path="analytics" element={<Analytics />} />
            <Route path="*" element={<EmptyState title="Page not found" />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
