// Mapa de la historia (detalle de un grupo): dibuja en SVG los nodos y aristas que devuelve
// /grupos/<id>/grafo. Acomodo por fuerzas, calculado una sola vez (sin animación) y con posición
// inicial fija, así que el mismo grupo se ve siempre igual. Sin librerías: la CSP solo admite
// scripts propios y el proyecto no suma dependencias.
(function () {
  "use strict";

  var W = 820, H = 520, PAD = 34;
  var SVG_NS = "http://www.w3.org/2000/svg";
  var RADIUS = {medio: 15, nota: 7, actor: 10, cuenta: 11, posteo: 5};
  var LABELED = {medio: true, actor: true, cuenta: true};  // las notas y posteos, al pasar el mouse
  var KIND_NAMES = {medio: "Medio", nota: "Nota", actor: "Nombre", cuenta: "Cuenta", posteo: "Posteo"};

  function el(name, attrs, parent) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(node);
    return node;
  }

  // Fruchterman-Reingold con enfriamiento. Con el tope de 60 nodos son unas pocas decenas de ms.
  function layout(nodes, edges) {
    var n = nodes.length, k = Math.sqrt((W * H) / Math.max(n, 1)) * 0.75;
    var golden = Math.PI * (3 - Math.sqrt(5));
    nodes.forEach(function (d, i) {  // espiral: posición inicial determinista
      var r = 18 * Math.sqrt(i + 0.5);
      d.x = W / 2 + r * Math.cos(i * golden);
      d.y = H / 2 + r * Math.sin(i * golden);
    });
    var index = {};
    nodes.forEach(function (d, i) { index[d.id] = i; });
    var links = edges.map(function (e) { return [index[e.source], index[e.target]]; });
    var temp = W / 8;
    for (var it = 0; it < 350; it++) {
      var dx = new Array(n).fill(0), dy = new Array(n).fill(0);
      for (var i = 0; i < n; i++) {
        for (var j = i + 1; j < n; j++) {
          var ox = nodes[i].x - nodes[j].x, oy = nodes[i].y - nodes[j].y;
          var dist = Math.max(Math.sqrt(ox * ox + oy * oy), 0.01);
          var f = (k * k) / dist;
          dx[i] += (ox / dist) * f; dy[i] += (oy / dist) * f;
          dx[j] -= (ox / dist) * f; dy[j] -= (oy / dist) * f;
        }
      }
      links.forEach(function (l) {
        var a = nodes[l[0]], b = nodes[l[1]];
        var ox = a.x - b.x, oy = a.y - b.y;
        var dist = Math.max(Math.sqrt(ox * ox + oy * oy), 0.01);
        var f = (dist * dist) / k;
        dx[l[0]] -= (ox / dist) * f; dy[l[0]] -= (oy / dist) * f;
        dx[l[1]] += (ox / dist) * f; dy[l[1]] += (oy / dist) * f;
      });
      for (var m = 0; m < n; m++) {
        // Un poco de gravedad al centro para que los nodos sueltos no se vayan al borde.
        dx[m] += (W / 2 - nodes[m].x) * 0.02 * k / 10;
        dy[m] += (H / 2 - nodes[m].y) * 0.02 * k / 10;
        var len = Math.max(Math.sqrt(dx[m] * dx[m] + dy[m] * dy[m]), 0.01);
        nodes[m].x += (dx[m] / len) * Math.min(len, temp);
        nodes[m].y += (dy[m] / len) * Math.min(len, temp);
        nodes[m].x = Math.min(W - PAD, Math.max(PAD, nodes[m].x));
        nodes[m].y = Math.min(H - PAD, Math.max(PAD, nodes[m].y));
      }
      temp *= 0.985;
    }
  }

  function draw(box, data) {
    var nodes = data.nodes, edges = data.edges;
    if (nodes.length < 2) {
      box.querySelector(".graph-status").textContent = "No hay conexiones para dibujar.";
      return;
    }
    layout(nodes, edges);
    // En un mapa chico entran también los títulos de las notas; en uno grande, al pasar el mouse.
    var labelAll = nodes.length <= 16;
    var byId = {};
    nodes.forEach(function (d) { byId[d.id] = d; });

    var svg = el("svg", {viewBox: "0 0 " + W + " " + H, role: "img", "class": "graph-svg",
                         "aria-label": "Mapa de la historia: " + nodes.length + " elementos y " +
                         edges.length + " conexiones. La lista de conexiones está debajo."});
    var defs = el("defs", {}, svg);
    var marker = el("marker", {id: "g-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
                               markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse"}, defs);
    el("path", {d: "M0 0 L10 5 L0 10 Z", "class": "g-arrow"}, marker);

    var gEdges = el("g", {}, svg), gNodes = el("g", {}, svg);
    var neighbors = {};
    edges.forEach(function (e) {
      var a = byId[e.source], b = byId[e.target];
      var ox = b.x - a.x, oy = b.y - a.y, dist = Math.max(Math.sqrt(ox * ox + oy * oy), 0.01);
      // Las flechas terminan en el borde del círculo de destino, no en su centro.
      var cut = e.directed ? RADIUS[b.kind] + 3 : 0;
      var line = el("line", {x1: a.x.toFixed(1), y1: a.y.toFixed(1),
                             x2: (b.x - (ox / dist) * cut).toFixed(1),
                             y2: (b.y - (oy / dist) * cut).toFixed(1),
                             "class": "g-edge g-" + e.kind}, gEdges);
      if (e.directed) line.setAttribute("marker-end", "url(#g-arrow)");
      el("title", {}, line).textContent = e.label;
      e.line = line;
      (neighbors[e.source] = neighbors[e.source] || []).push(e);
      (neighbors[e.target] = neighbors[e.target] || []).push(e);
    });

    nodes.forEach(function (d) {
      var wrap = d.url ? el("a", {href: d.url}, gNodes) : el("g", {}, gNodes);
      wrap.setAttribute("class", "g-item");
      var c = el("circle", {cx: d.x.toFixed(1), cy: d.y.toFixed(1), r: RADIUS[d.kind] || 6,
                            "class": "g-node g-" + d.kind + (d.external ? " g-external" : "")}, wrap);
      el("title", {}, c).textContent = KIND_NAMES[d.kind] + ": " + d.title;
      var label = el("text", {x: d.x.toFixed(1), y: (d.y + (RADIUS[d.kind] || 6) + 12).toFixed(1),
                              "text-anchor": "middle",
                              "class": "g-label" + (LABELED[d.kind] || (labelAll && d.label) ? "" : " g-label-hover")}, wrap);
      label.textContent = d.label;
      d.wrap = wrap;
      // Al pasar el mouse (o con el foco del teclado) se resaltan el nodo y sus conexiones.
      function on() {
        svg.classList.add("g-focus");
        wrap.classList.add("g-on");
        (neighbors[d.id] || []).forEach(function (e) {
          e.line.classList.add("g-on");
          byId[e.source].wrap.classList.add("g-on");
          byId[e.target].wrap.classList.add("g-on");
        });
      }
      function off() {
        svg.classList.remove("g-focus");
        Array.prototype.forEach.call(svg.querySelectorAll(".g-on"), function (x) {
          x.classList.remove("g-on");
        });
      }
      wrap.addEventListener("mouseenter", on);
      wrap.addEventListener("mouseleave", off);
      wrap.addEventListener("focus", on);
      wrap.addEventListener("blur", off);
    });

    var status = box.querySelector(".graph-status");
    box.insertBefore(svg, status);
    status.textContent = data.truncated
      ? "Se muestran hasta " + data.max_nodes + " elementos y 15 posteos: hay más conexiones que no entran en el mapa."
      : "";

    // La misma información como lista (lectores de pantalla, impresión).
    var list = document.querySelector("#grafo .graph-list ul");
    if (list) {
      edges.forEach(function (e) {
        var li = document.createElement("li");
        var sep = e.directed ? " → " : " — ";
        li.textContent = byId[e.source].title + sep + e.label + sep + byId[e.target].title;
        list.appendChild(li);
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-graph-url]"), function (box) {
      fetch(box.getAttribute("data-graph-url"), {credentials: "same-origin"})
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) { draw(box, data); })
        .catch(function () {
          box.querySelector(".graph-status").textContent = "No se pudo cargar el mapa.";
        });
    });
  });
})();
