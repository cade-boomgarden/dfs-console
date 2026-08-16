import { useEffect, useState } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { BrowserRouter, Link, NavLink, Route, Routes, useParams } from "react-router-dom";
import { api } from "./api";
import Login from "./pages/Login";
import Slates from "./pages/Slates";
import SlateOverview from "./pages/SlateOverview";
import Pool from "./pages/Pool";
import Builder from "./pages/Builder";
import Builds from "./pages/Builds";
import SetDetail from "./pages/SetDetail";
import Contests from "./pages/Contests";
import Jobs from "./pages/Jobs";
import Review from "./pages/Review";

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });

function SlateNav() {
  const { slateId } = useParams();
  const tabs = [
    ["", "Overview"], ["pool", "Pool"], ["builder", "Builder"],
    ["builds", "Builds"], ["contests", "Contests"],
  ] as const;
  return (
    <nav className="flex gap-1">
      {tabs.map(([path, label]) => (
        <NavLink key={path} to={`/slate/${slateId}/${path}`} end={path === ""}
          className={({ isActive }) =>
            `px-3 py-1 rounded text-xs ${isActive ? "bg-[var(--raised)] text-[var(--ink)]" : "text-[var(--dim)] hover:text-[var(--ink)]"}`}>
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-full">
      <header className="border-b hairline px-4 h-11 flex items-center gap-6 sticky top-0 bg-[var(--bg)] z-10">
        <Link to="/" className="font-semibold tracking-widest text-[var(--amber)] text-sm">DFS·CONSOLE</Link>
        <Routes>
          <Route path="/slate/:slateId/*" element={<SlateNav />} />
          <Route path="*" element={null} />
        </Routes>
        <div className="ml-auto flex gap-1">
          <NavLink to="/jobs" className={({ isActive }) => `px-3 py-1 rounded text-xs ${isActive ? "bg-[var(--raised)]" : "text-[var(--dim)] hover:text-[var(--ink)]"}`}>Jobs</NavLink>
          <NavLink to="/review" className={({ isActive }) => `px-3 py-1 rounded text-xs ${isActive ? "bg-[var(--raised)]" : "text-[var(--dim)] hover:text-[var(--ink)]"}`}>Review</NavLink>
          <button className="px-3 py-1 text-xs text-[var(--dim)] hover:text-[var(--ink)]"
            onClick={() => api.post("/api/auth/logout").then(() => location.assign("/"))}>Sign out</button>
        </div>
      </header>
      <main className="p-4 max-w-[1400px] mx-auto">{children}</main>
    </div>
  );
}

function Gate() {
  const [unauth, setUnauth] = useState(false);
  const me = useQuery({ queryKey: ["me"], queryFn: () => api.get("/api/auth/me") });
  useEffect(() => {
    const h = () => setUnauth(true);
    window.addEventListener("dfs:unauth", h);
    return () => window.removeEventListener("dfs:unauth", h);
  }, []);
  if (me.isLoading) return <div className="p-8 text-[var(--dim)]">Loading…</div>;
  if (me.isError || unauth) return <Login onSuccess={() => { setUnauth(false); me.refetch(); }} />;
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Slates />} />
        <Route path="/slate/:slateId" element={<SlateOverview />} />
        <Route path="/slate/:slateId/pool" element={<Pool />} />
        <Route path="/slate/:slateId/builder" element={<Builder />} />
        <Route path="/slate/:slateId/builds" element={<Builds />} />
        <Route path="/slate/:slateId/sets/:setId" element={<SetDetail />} />
        <Route path="/slate/:slateId/contests" element={<Contests />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/review" element={<Review />} />
      </Routes>
    </Shell>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <Gate />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
