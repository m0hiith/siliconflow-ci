import { NavLink, Outlet } from "react-router-dom";

export function Layout() {
  return (
    <div className="app">
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
    </div>
  );
}
