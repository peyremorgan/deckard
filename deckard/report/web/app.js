"use strict";
const $ = (id) => document.getElementById(id);
const canvas = $("map"),
  ctx = canvas.getContext("2d");
const sectorTile = document.createElement("canvas");
sectorTile.width = sectorTile.height = 8;
const tileContext = sectorTile.getContext("2d");
tileContext.fillStyle = "rgba(4, 10, 19, 0.28)";
tileContext.fillRect(0, 0, 8, 1);
tileContext.fillRect(0, 0, 1, 8);
const sectorGrid = ctx.createPattern(sectorTile, "repeat");
const state = { x: "d:1", y: "d:1", xo: 0, yo: 0, metric: "allocated" };
const saved = new URLSearchParams(location.search);
for (const key of ["x", "y", "metric"])
  if (saved.has(key)) state[key] = saved.get(key);
for (const key of ["xo", "yo"])
  if (saved.has(key)) state[key] = Number(saved.get(key));
if (new URLSearchParams(location.search).get("embedded") === "1") { document.body.classList.add("embedded"); state.embedded = "1"; }
const history = [];
let view,
  manifest,
  layout,
  hover,
  zoom = 1,
  panX = 0,
  panY = 0,
  dragging,
  request = 0;
function bytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const p = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return (
    (n / 1024 ** p).toLocaleString(undefined, {
      maximumFractionDigits: p > 0 ? 2 : 0,
    }) +
    " " +
    u[p]
  );
}
const pct = (n) =>
  (100 * n).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "%";
const count = (n) => n.toLocaleString();
function text(tag, value, className) {
  const e = document.createElement(tag);
  e.textContent = value;
  if (className) e.className = className;
  return e;
}
function button(value, action) {
  const e = text("button", value);
  e.onclick = action;
  return e;
}
function offsets(items, total) {
  let position = 0;
  return items.map((item) => {
    const start = position;
    position += total ? item.weight / total : 0;
    return [start, position];
  });
}
function mix(a, b, t) {
  return (
    "#" +
    a
      .map((v, i) =>
        Math.round(v + (b[i] - v) * t)
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")
  );
}
function boundaries() {
  const r = canvas.getBoundingClientRect(),
    dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(r.width * dpr);
  canvas.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const band = Math.max(40, Math.min(68, r.width * 0.12));
  return {
    w: r.width,
    h: r.height,
    band,
    side: Math.min(r.width, r.height) - band - 12,
  };
}
function draw() {
  layout = boundaries();
  const { w, h, band, side } = layout;
  ctx.fillStyle = "#fffdf8";
  ctx.fillRect(0, 0, w, h);
  if (!view) return;
  const xx = offsets(view.x.items, view.x.total),
    yy = offsets(view.y.items, view.y.total);
  layout.xx = xx;
  layout.yy = yy;
  const posX = (v) => band + panX + v * side * zoom,
    posY = (v) => band + panY + v * side * zoom;
  ctx.save();
  ctx.beginPath();
  ctx.rect(band, band, side, side);
  ctx.clip();
  for (let a = 0; a < xx.length; a++)
    for (let b = 0; b < yy.length; b++) {
      const x = posX(xx[a][0]),
        y = posY(yy[b][0]),
        cw = (xx[a][1] - xx[a][0]) * side * zoom,
        ch = (yy[b][1] - yy[b][0]) * side * zoom;
      if (
        cw <= 0 ||
        ch <= 0 ||
        x > band + side ||
        y > band + side ||
        x + cw < band ||
        y + ch < band
      )
        continue;
      const cell = view.cells[a][b];
      ctx.fillStyle = cell.overlap
        ? "#eceee5"
        : cell.x_in_single || cell.y_in_single
          ? "#8974b6"
          : mix([245, 240, 226], [192, 73, 45], cell.similarity);
      ctx.fillRect(x, y, cw, ch);
      if (!cell.overlap) {
        ctx.fillStyle = sectorGrid;
        ctx.fillRect(x, y, cw, ch);
      }
      ctx.strokeStyle = "#fffdf8";
      ctx.lineWidth = 0.8;
      ctx.strokeRect(x, y, cw, ch);
      if (cell.overlap && cw > 18 && ch > 18) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(x, y, cw, ch);
        ctx.clip();
        ctx.strokeStyle = "#d8decf";
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let p = -ch; p < cw; p += 13) {
          ctx.moveTo(x + p, y + ch);
          ctx.lineTo(x + p + ch, y);
        }
        ctx.stroke();
        ctx.restore();
      }
      if (cw > 72 && ch > 43) {
        ctx.fillStyle =
          cell.x_in_single || cell.y_in_single
            ? "#fff"
            : cell.similarity > 0.6
              ? "#fff9ef"
              : "#6f6d5d";
        ctx.textAlign = "center";
        ctx.font =
          "500 " + Math.min(22, cw / 5, ch / 3) + "px -apple-system,sans-serif";
        ctx.fillText(
          cell.overlap ? "↗" : pct(cell.similarity),
          x + cw / 2,
          y + ch / 2 + 5,
        );
      }
      if (hover && hover.a === a && hover.b === b) {
        ctx.strokeStyle = "#2c4336";
        ctx.lineWidth = 2;
        ctx.strokeRect(x + 1, y + 1, cw - 2, ch - 2);
      }
    }
  ctx.restore();
  for (const axis of ["x", "y"]) {
    const items = view[axis].items,
      ranges = axis === "x" ? xx : yy;
    ctx.save();
    ctx.beginPath();
    ctx.rect(
      axis === "x" ? band : 0,
      axis === "x" ? 0 : band,
      axis === "x" ? side : band,
      axis === "x" ? band : side,
    );
    ctx.clip();
    items.forEach((item, i) => {
      const start = (axis === "x" ? posX : posY)(ranges[i][0]),
        len = (ranges[i][1] - ranges[i][0]) * side * zoom;
      if (len <= 0) return;
      const x = axis === "x" ? start : 0,
        y = axis === "x" ? 0 : start,
        cw = axis === "x" ? len : band - 5,
        ch = axis === "x" ? band - 5 : len;
      ctx.fillStyle = i % 2 ? "#e5e9dc" : "#dce3d3";
      ctx.fillRect(x, y, cw, ch);
      ctx.strokeStyle = "#fffdf8";
      ctx.lineWidth = 1;
      ctx.strokeRect(x, y, cw, ch);
      if (item.kind === "folder" && item.covered_elsewhere) {
        ctx.fillStyle = "#318d80";
        ctx.fillRect(x, y, axis === "x" ? cw : 4, axis === "x" ? 4 : ch);
      }
      if (len > 34) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(x + 3, y + 3, Math.max(0, cw - 6), Math.max(0, ch - 6));
        ctx.clip();
        ctx.fillStyle = "#42523f";
        ctx.textAlign = "center";
        ctx.font = "11px -apple-system,sans-serif";
        if (axis === "x") {
          ctx.fillText(item.name, x + cw / 2, y + ch * 0.46);
          ctx.font = "9px -apple-system,sans-serif";
          ctx.fillText(bytes(item.weight), x + cw / 2, y + ch * 0.72);
        } else {
          ctx.translate(x + cw * 0.45, y + ch / 2);
          ctx.rotate(-Math.PI / 2);
          ctx.fillText(item.name, 0, 0);
          ctx.font = "9px -apple-system,sans-serif";
          ctx.fillText(bytes(item.weight), 0, 14);
        }
        ctx.restore();
      }
    });
    ctx.restore();
  }
  ctx.strokeStyle = "#cbd3c2";
  ctx.lineWidth = 1;
  ctx.strokeRect(band, band, side, side);
  ctx.fillStyle = "#63765c";
  ctx.font = "10px -apple-system,sans-serif";
  ctx.textAlign = "center";
  ctx.fillText("Y ↕", band * 0.45, band * 0.45);
  ctx.fillText("X →", band * 0.45, band * 0.7);
  $("zoom").textContent = Math.round(zoom * 100) + "%";
}
function fit() {
  zoom = 1;
  panX = panY = 0;
  draw();
}
function zoomAt(next, cx = layout.side / 2, cy = layout.side / 2) {
  next = Math.max(1, Math.min(64, next));
  panX = cx - ((cx - panX) * next) / zoom;
  panY = cy - ((cy - panY) * next) / zoom;
  zoom = next;
  draw();
}
function locate(event) {
  if (!view || !layout) return null;
  const r = canvas.getBoundingClientRect(),
    x = event.clientX - r.left,
    y = event.clientY - r.top,
    { band, side, xx, yy } = layout;
  const find = (p, pan, ranges) =>
    ranges.findIndex(([a, b]) => p >= a && p < b);
  const a = find((x - band - panX) / (side * zoom), panX, xx),
    b = find((y - band - panY) / (side * zoom), panY, yy);
  if (
    x >= band &&
    x <= band + side &&
    y >= band &&
    y <= band + side &&
    a >= 0 &&
    b >= 0
  )
    return { a, b };
  if (y < band && x >= band && a >= 0) return { axis: "x", index: a };
  if (x < band && y >= band && b >= 0) return { axis: "y", index: b };
  return null;
}
function inspect(hit) {
  if (!hit) return;
  const d = $("details");
  d.replaceChildren();
  if (hit.axis) {
    const item = view[hit.axis].items[hit.index];
    $("selection").textContent = item.name;
    $("hint").textContent =
      "Click this band to open its " + hit.axis.toUpperCase() + " axis.";
    const dl = document.createElement("dl");
    dl.append(
      text("dt", "SIZE"),
      text("dd", bytes(item.weight)),
      text("dt", "REGULAR FILES"),
      text("dd", count(item.files)),
    );
    d.append(dl);
    if (item.covered_elsewhere)
      d.append(
        text(
          "p",
          "All file contents have matching occurrences elsewhere.",
          "coverage",
        ),
      );
    return;
  }
  const a = view.x.items[hit.a],
    b = view.y.items[hit.b],
    c = view.cells[hit.a][hit.b];
  $("selection").textContent = a.name + " × " + b.name;
  $("hint").textContent = c.overlap
    ? "This intersection includes the same files. Double-click to explore within these folders."
    : "Shared contents, weighted by bytes rather than file count.";
  const dl = document.createElement("dl");
  dl.append(
    text("dt", "HORIZONTAL · X"),
    text("dd", bytes(a.weight)),
    text("dt", "VERTICAL · Y"),
    text("dd", bytes(b.weight)),
    text("dt", "MATCHED BYTES"),
    text("dd", c.overlap ? "Overlapping scope" : bytes(c.shared)),
    text("dt", "SIMILARITY"),
    text("dd", c.overlap ? "—" : pct(c.similarity)),
  );
  d.append(dl);
  if (c.x_in_y)
    d.append(
      text(
        "p",
        "Every file content in “" + a.name + "” exists in “" + b.name + "”.",
        "coverage",
      ),
    );
  if (c.y_in_x)
    d.append(
      text(
        "p",
        "Every file content in “" + b.name + "” exists in “" + a.name + "”.",
        "coverage",
      ),
    );
  if (!c.overlap) d.append(button("Open this comparison", () => openCell(hit)));
}
function setAxis(axis, item) {
  state[axis] = item.token;
  state[axis + "o"] = item.kind === "other" ? item.offset : 0;
}
function navigate(action) {
  history.push({ ...state });
  action();
  load();
}
function openCell(hit) {
  if (!hit || hit.axis) return;
  navigate(() => {
    setAxis("x", view.x.items[hit.a]);
    setAxis("y", view.y.items[hit.b]);
  });
}
function list() {
  if (!view) return;
  const axis = $("list-axis").value,
    items = view[axis].items,
    target = $("item-list");
  target.replaceChildren();
  if (!items.length) {
    target.append(
      text("p", "No regular-file bytes in this scope.", "empty-note"),
    );
    return;
  }
  items.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "item-row";
    const name = text(
      "div",
      (item.kind === "folder" ? "▱ " : item.kind === "other" ? "⋯ " : "· ") +
        item.name,
      "item-name",
    );
    name.append(
      text(
        "small",
        count(item.files) +
          " files" +
          (item.blocked
            ? " · " + count(item.blocked) + " skipped / unreadable"
            : ""),
      ),
    );
    row.append(name, text("span", bytes(item.weight)));
    const coverage = document.createElement("div");
    const counterparts = (
      axis === "x"
        ? view.cells[index].map((c, i) => [c, view.y.items[i]])
        : view.cells.map((row, i) => [row[index], view.x.items[i]])
    ).filter(([c]) => (axis === "x" ? c.x_in_single : c.y_in_single));
    if (counterparts.length) {
      const pill = text("span", "One counterpart covers all", "pill keeper");
      pill.title = counterparts.map(([, i]) => i.path || i.name).join("\n");
      coverage.append(pill);
    } else if (item.kind === "folder" && item.covered_elsewhere)
      coverage.append(text("span", "All contents elsewhere", "pill"));
    const bar = document.createElement("div");
    bar.className = "mini-bar";
    const fill = document.createElement("i");
    fill.style.width =
      (view[axis].total ? (100 * item.weight) / view[axis].total : 0) + "%";
    bar.append(fill);
    coverage.append(bar);
    row.append(
      coverage,
      button("Open ↗", () => navigate(() => setAxis(axis, item))),
    );
    target.append(row);
  });
  if (view[axis].zero_size_items)
    target.append(
      text(
        "p",
        count(view[axis].zero_size_items) +
          " zero-size items have zero-width bands; they remain accessible in this list and coverage includes them.",
        "empty-note",
      ),
    );
}
async function load() {
  const id = ++request;
  $("loading").replaceChildren(
    document.createElement("span"),
    document.createTextNode("Comparing file contents…"),
  );
  $("loading").classList.remove("hidden");
  $("back").disabled = !history.length;
  $("details").replaceChildren();
  $("selection").textContent = "Explore the overlap.";
  $("hint").textContent =
    "Move over a cell to compare two items. Click a band to open that axis, or choose an item below.";
  if (coveredMetric !== state.metric) {
    $("covered-list").replaceChildren();
    $("covered-load").textContent = "Find covered folders ↗";
    $("covered-load").onclick = () => coveredFolders();
  }
  try {
    const response = await fetch("/api/view?" + new URLSearchParams(state));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    if (id !== request) return;
    view = data;
    hover = null;
    $("metric").value = state.metric;
    $("xpath").textContent =
      "X · " +
      (view.x.scope.path || "Root") +
      (state.xo ? " / Other items" : "");
    $("ypath").textContent =
      "Y · " +
      (view.y.scope.path || "Root") +
      (state.yo ? " / Other items" : "");
    window.history.replaceState(null, "", "/?" + new URLSearchParams(state));
    fit();
    list();
    $("loading").classList.add("hidden");
  } catch (e) {
    if (id === request)
      $("loading").textContent = "Could not load this comparison: " + e.message;
  }
}
let coveredOffset = 0;
let coveredMetric = "allocated";
async function coveredFolders(reset = true) {
  coveredMetric = state.metric;
  const target = $("covered-list"),
    control = $("covered-load");
  if (reset) {
    coveredOffset = 0;
    target.replaceChildren();
  }
  control.disabled = true;
  control.textContent = "Finding covered folders…";
  try {
    const response = await fetch(
        "/api/covered?" +
          new URLSearchParams({ offset: coveredOffset, metric: state.metric }),
      ),
      data = await response.json();
    if (!response.ok) throw new Error(data.error);
    for (const item of data.items) {
      const row = document.createElement("div");
      row.className = "item-row";
      const name = text("div", item.path, "item-name");
      name.append(text("small", count(item.files) + " files"));
      const status = text("div", "All contents elsewhere", "pill");
      const find = button("Find keeper", async () => {
        find.disabled = true;
        find.textContent = "Checking…";
        try {
          const response = await fetch(
              "/api/keepers?folder=" + encodeURIComponent(item.token),
            ),
            info = await response.json();
          if (!response.ok) throw new Error(info.error);
          status.replaceChildren();
          status.className = "";
          if (info.distributed_only) {
            status.append(
              text("span", "Copies spread across locations", "pill"),
            );
            find.textContent = "Explore ↗";
            find.onclick = () => {
              navigate(() => {
                setAxis("x", item);
                state.y = "d:1";
                state.yo = 0;
              });
              $("map-wrap").scrollIntoView({
                behavior: "smooth",
                block: "center",
              });
            };
          } else {
            status.append(
              text(
                "span",
                "One keeper: " + info.keepers[0].path,
                "pill keeper",
              ),
            );
            status.title = info.keepers.map((i) => i.path).join("\n");
            find.textContent = "Compare ↗";
            find.onclick = () => {
              navigate(() => {
                setAxis("x", item);
                setAxis("y", info.keepers[0]);
              });
              $("map-wrap").scrollIntoView({
                behavior: "smooth",
                block: "center",
              });
            };
          }
        } catch (e) {
          status.textContent = e.message;
          find.textContent = "Retry";
        } finally {
          find.disabled = false;
        }
      });
      row.append(name, text("span", bytes(item[state.metric])), status, find);
      target.append(row);
    }
    coveredOffset += data.items.length;
    control.textContent =
      coveredOffset < data.total
        ? "Show more (" + count(data.total - coveredOffset) + ")"
        : "Refresh candidates";
    control.onclick = () => coveredFolders(coveredOffset >= data.total);
    if (!data.total)
      target.append(
        text(
          "p",
          "No fully covered folders met the scan coverage checks.",
          "empty-note",
        ),
      );
  } catch (e) {
    target.append(text("p", e.message, "empty-note"));
    control.textContent = "Retry";
  } finally {
    control.disabled = false;
  }
}
canvas.addEventListener("pointermove", (event) => {
  if (dragging) {
    panX = dragging.panX + event.clientX - dragging.x;
    panY = dragging.panY + event.clientY - dragging.y;
    draw();
    return;
  }
  const hit = locate(event);
  if (JSON.stringify(hit) !== JSON.stringify(hover)) {
    hover = hit;
    inspect(hit);
    draw();
  }
});
canvas.addEventListener("pointerdown", (event) => {
  dragging = {
    x: event.clientX,
    y: event.clientY,
    panX,
    panY,
    hit: locate(event),
  };
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener("pointerup", (event) => {
  if (!dragging) return;
  const click =
      Math.hypot(event.clientX - dragging.x, event.clientY - dragging.y) < 4,
    hit = dragging.hit;
  dragging = null;
  if (click && hit && hit.axis)
    navigate(() => setAxis(hit.axis, view[hit.axis].items[hit.index]));
});
canvas.addEventListener("pointercancel", () => {
  dragging = null;
});
canvas.addEventListener("dblclick", (event) => openCell(locate(event)));
canvas.addEventListener(
  "wheel",
  (event) => {
    if (!layout) return;
    event.preventDefault();
    const r = canvas.getBoundingClientRect(),
      cx = event.clientX - r.left - layout.band,
      cy = event.clientY - r.top - layout.band,
      next = Math.max(1, Math.min(64, zoom * Math.exp(-event.deltaY * 0.002)));
    zoomAt(next, cx, cy);
  },
  { passive: false },
);
canvas.addEventListener("keydown", (event) => {
  if (event.key === "Escape" || event.key === "0") fit();
  if (event.key === "+" || event.key === "=") zoomAt(zoom * 1.5);
  if (event.key === "-") zoomAt(zoom / 1.5);
  if (event.key === "Backspace" && history.length) {
    event.preventDefault();
    Object.assign(state, history.pop());
    load();
  }
});
$("fit").onclick = fit;
$("zoom-in").onclick = () => zoomAt(zoom * 1.5);
$("zoom-out").onclick = () => zoomAt(zoom / 1.5);
$("reset").onclick = () =>
  navigate(() => Object.assign(state, { x: "d:1", y: "d:1", xo: 0, yo: 0 }));
$("swap").onclick = () =>
  navigate(() => {
    [state.x, state.y] = [state.y, state.x];
    [state.xo, state.yo] = [state.yo, state.xo];
  });
$("back").onclick = () => {
  if (history.length) {
    Object.assign(state, history.pop());
    $("metric").value = state.metric;
    load();
  }
};
$("metric").onchange = () =>
  navigate(() => {
    state.metric = $("metric").value;
    state.xo = state.yo = 0;
  });
$("list-axis").onchange = list;
$("covered-load").onclick = () => coveredFolders();
new ResizeObserver(draw).observe(canvas);
fetch("/api/manifest")
  .then((r) => r.json())
  .then((data) => {
    manifest = data;
    $("allocated").textContent = bytes(data.allocated_bytes);
    $("logical").textContent = bytes(data.logical_bytes);
    $("files").textContent = count(data.files);
    $("skipped").textContent = count(data.blocked_entries);
    $("snapshot").textContent =
      "Inventory · " +
      new Date(data.inventory_time_unix * 1000).toLocaleString() +
      "\n" +
      count(data.directories) +
      " folders";
    $("source").textContent =
      "Source: " +
      data.source_root +
      " · Scan epoch " +
      data.scan_epoch +
      " · " +
      data.coverage_scope +
      " " +
      data.size_scope;
  })
  .catch((e) => {
    $("snapshot").textContent = "Could not read report metadata: " + e.message;
  });
load();
