import { Link, NavLink, Outlet } from "react-router-dom";
import { SilabsLogo } from "../brand/SilabsLogo";
import { ThemeToggle } from "../brand/ThemeToggle";
import { DemoBanner } from "../demo/DemoBanner";
import { Tour } from "../demo/Tour";

export function Layout() {
  return (
    <div className="app">
      <div className="chrome">
      <DemoBanner />
      <header className="topbar">
        <Link to="/" className="brand" aria-label="SiliconFlow CI home">
          <SilabsLogo className="brand__logo" />
          <span className="brand__divider" aria-hidden />
          <span className="brand__name">
            SiliconFlow<span className="brand__ci">CI</span>
          </span>
        </Link>
        <nav className="nav" aria-label="main">
          <NavLink to="/" end>
            Overview
          </NavLink>
          <NavLink to="/builds">Builds</NavLink>
          <NavLink to="/analytics">Analytics</NavLink>
          <NavLink to="/how-it-works">How it works</NavLink>
        </nav>
        <div className="topbar__tools">
          <ThemeToggle />
        </div>
      </header>
      </div>
      <main className="main">
        <Outlet />
      </main>
      <footer className="footer">
        <span>
          <b>SiliconFlow CI</b> · build, test and release gating for embedded firmware
        </span>
        <span className="footer__legal">
          Independent portfolio project, not affiliated with or endorsed by Silicon Labs. Silicon Labs
          and the Silicon Labs logo are trademarks of Silicon Laboratories Inc.
        </span>
      </footer>
      <Tour />
    </div>
  );
}
