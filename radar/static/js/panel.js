// Comportamiento del panel. La CSP (script-src 'self') bloquea los scripts y manejadores
// inline, así que todo va acá.
(function () {
  "use strict";

  // <select data-autosubmit>: aplica el filtro al cambiar (htmx atiende el submit si el form lo pide).
  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (el.matches && el.matches("select[data-autosubmit]") && el.form) el.form.requestSubmit();
  });

  // <form data-confirm="…">: pide confirmación antes de enviar.
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var msg = form.getAttribute && form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) ev.preventDefault();
  }, true);

  // <button data-print>: imprime la página (o la guarda como PDF desde el diálogo del navegador).
  document.addEventListener("click", function (ev) {
    if (ev.target.closest && ev.target.closest("[data-print]")) window.print();
  });

  // <a data-click-id="N">: registra que se abrió la nota original (señal de interés para el
  // orden del resumen). No demora la navegación: el pedido sigue aunque se cierre la página.
  document.addEventListener("click", function (ev) {
    var link = ev.target.closest && ev.target.closest("a[data-click-id]");
    if (!link) return;
    var token = "";
    try { token = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || ""; }
    catch (e) { return; }
    fetch("/noticias/" + encodeURIComponent(link.getAttribute("data-click-id")) + "/click", {
      method: "POST", keepalive: true, credentials: "same-origin",
      headers: {"X-CSRF-Token": token}
    }).catch(function () {});
  });

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("form-recolectar");
    if (form) setupCollect(form);
  });

  function setupCollect(form) {
    var allToggle = document.getElementById("check-all-media");
    var checks = Array.prototype.slice.call(form.querySelectorAll(".media-check"));
    var btn = document.getElementById("btn-recolectar");
    var status = document.getElementById("chain-status");
    if (!btn || checks.length === 0) return;

    function selected() { return checks.filter(function (c) { return c.checked; }); }
    function labelOf(c) { return c.closest("label") ? c.closest("label").textContent.trim() : c.value; }

    function update() {
      var n = selected().length;
      if (allToggle) {
        allToggle.checked = n === checks.length;
        allToggle.indeterminate = n > 0 && n < checks.length;
      }
      btn.disabled = n === 0;
      if (n === 0) btn.textContent = "Elegí al menos un medio";
      else if (n === 1) btn.textContent = "Recolectar (" + labelOf(selected()[0]) + ")";
      else btn.textContent = "Recolectar " + n + " medios, uno tras otro";
    }

    if (allToggle) {
      allToggle.addEventListener("change", function () {
        checks.forEach(function (c) { c.checked = allToggle.checked; });
        update();
      });
    }
    checks.forEach(function (c) { c.addEventListener("change", update); });
    update();

    // En un servidor propio el servidor encadena los medios en un hilo: envío normal.
    // En Vercel cada medio va en su propio pedido (y su propia invocación), uno tras otro.
    if (form.getAttribute("data-chain") !== "1") return;

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      runChain(form, selected(), btn, status, labelOf, update);
    });
  }

  function errorFrom(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var el = doc.querySelector("p.alert-error, #manual-run .alert-error");
    if (!el) return null;
    var detail = el.querySelector(".error-text");
    return (detail ? detail.textContent : el.textContent).replace(/\s+/g, " ").trim();
  }

  function post(form, fields) {
    var fd = new FormData();
    fd.append("csrf_token", form.querySelector("[name=csrf_token]").value);
    fields.forEach(function (kv) { fd.append(kv[0], kv[1]); });
    return fetch(form.action, { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 504) return { stop: true, error: "se agotó el tiempo de la función" };
        return r.text().then(function (html) {
          // 400: pedido rechazado (otra ejecución en curso, etc.); no tiene sentido seguir.
          if (!r.ok) return { stop: true, error: errorFrom(html) || ("error " + r.status) };
          // 303 → /configuracion: la tarea ya corrió; su estado dice si falló.
          var err = errorFrom(html);
          return err ? { error: err } : {};
        });
      })
      .catch(function () { return { stop: true, error: "sin conexión con el servidor" }; });
  }

  function refreshState() {
    if (window.htmx && document.getElementById("manual-run")) {
      window.htmx.ajax("GET", "/configuracion/ejecutar/estado", { target: "#manual-run", swap: "outerHTML" });
    }
  }

  function runChain(form, medios, btn, status, labelOf, update) {
    var extra = [];
    ["force", "no_enrich"].forEach(function (name) {
      var el = form.querySelector("[name=" + name + "]");
      if (el && el.checked) extra.push([name, "on"]);
    });
    var noAnalyze = form.querySelector("[name=no_analyze]");
    var analyze = !(noAnalyze && noAnalyze.checked);
    var controls = Array.prototype.slice.call(form.querySelectorAll("input, button"));
    controls.forEach(function (c) { c.disabled = true; });
    var lines = [];
    var leaving = function (e) { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", leaving);
    status.hidden = false;

    function show(current) {
      status.innerHTML = "";
      lines.forEach(function (l) {
        var div = document.createElement("div");
        div.className = l.ok ? "chain-ok" : "chain-error";
        div.textContent = (l.ok ? "✓ " : "✗ ") + l.text;
        status.appendChild(div);
      });
      if (current) {
        var div = document.createElement("div");
        div.className = "muted";
        div.textContent = "… " + current;
        status.appendChild(div);
      }
    }

    function finish(stopped) {
      window.removeEventListener("beforeunload", leaving);
      controls.forEach(function (c) { c.disabled = false; });
      update();
      var fails = lines.filter(function (l) { return !l.ok; }).length;
      lines.push({ ok: !fails && !stopped,
                   text: stopped ? "Se detuvo la cadena." : (fails ? "Terminó con " + fails + " problema(s)." : "Terminó.") });
      show(null);
      refreshState();
    }

    var i = 0;
    function next() {
      if (i < medios.length) {
        var c = medios[i];
        var name = labelOf(c);
        show("Recolectando " + name + " (" + (i + 1) + "/" + medios.length + ")");
        i += 1;
        return post(form, [["tarea", "recolectar"], ["medios", c.value]].concat(extra)).then(function (res) {
          lines.push({ ok: !res.error, text: name + (res.error ? ": " + res.error : "") });
          refreshState();
          return res.stop ? finish(true) : next();
        });
      }
      if (analyze) {
        analyze = false;
        show("Analizando relaciones, grupos y alertas");
        return post(form, [["tarea", "analizar"]]).then(function (res) {
          lines.push({ ok: !res.error, text: "Análisis" + (res.error ? ": " + res.error : "") });
          return finish(!!res.stop);
        });
      }
      return finish(false);
    }
    next();
  }
})();
