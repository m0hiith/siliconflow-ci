import { NavLink, Outlet } from "react-router-dom";
import { DemoBanner } from "../demo/DemoBanner";
import { Tour } from "../demo/Tour";

export function Layout() {
  return (
    <div className="app">
      <DemoBanner />
      <header className="topbar">
        <span className="brand">
          <span className="brand__mark" aria-hidden>
            ▚
          </span>
          siliconflow<span className="muted">/ci</span>
        </span>
        <nav className="nav">
          <NavLink to="/" end>
            Overview
          </NavLink>
          <NavLink to="/builds">Builds</NavLink>
          <NavLink to="/analytics">Analytics</NavLink>
        </nav>
      </header>
      <main className="main">
        <Outlet />
      </main>
      <Tour />
    </div>
  );
}
