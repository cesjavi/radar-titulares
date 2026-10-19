// Aplica el tema elegido en el panel antes de pintar la página (se carga sin defer en <head>
// para que no parpadee; va en un archivo porque la CSP no admite scripts en línea).
(function () {
  "use strict";
  try {
    var theme = localStorage.getItem("radar-theme");
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
  } catch (e) { /* sin almacenamiento: sigue al sistema */ }
})();
