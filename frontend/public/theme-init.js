// Applies the viewer's saved light/dark choice before the app renders, so the
// page never flashes the other theme. Same-origin file: the CSP allows no
// inline scripts. Without a saved choice the OS preference applies (CSS).
try {
  var t = localStorage.getItem("sfci.theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch (e) {}
