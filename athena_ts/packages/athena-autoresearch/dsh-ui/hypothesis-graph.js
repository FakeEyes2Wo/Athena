/**
 * Dependency-free SVG hypothesis-graph renderer for the Athena DSH web UI.
 *
 * Layout is a small layered (Sugiyama-inspired) pass over the lineage DAG:
 * depth comes from parent→child edges, ordering inside a layer is refined by
 * barycenter sweeps to reduce crossings. Supersedes edges are drawn as dashed
 * curves above the lineage layer and do not participate in the layout.
 *
 * The renderer only needs the JSON produced by /api/hypothesis_graph, so it
 * stays decoupled from the runtime (mirrors ``src/athena/gui/graph.py``).
 */
(() => {
  const NS = "http://www.w3.org/2000/svg"
  const NODE_W = 210
  const NODE_H = 62
  const GAP_X = 90
  const GAP_Y = 40
  const PAD = 30

  // Status palette mirrors athena-gui/src/styles.css hypothesis status tokens.
  const STATUS_COLORS = {
    PROPOSED: { border: "#3B82F6", bg: "#EFF6FF", text: "#1E40AF" },
    SUPPORTED: { border: "#22C55E", bg: "#DCFCE7", text: "#166534" },
    REFUTED: { border: "#EF4444", bg: "#FEE2E2", text: "#991B1B" },
    REJECTED: { border: "#9CA3AF", bg: "#F3F4F6", text: "#374151" },
    INCONCLUSIVE: { border: "#F59E0B", bg: "#FFFBEB", text: "#B45309" },
    QUEUED: { border: "#06B6D4", bg: "#CFFAFE", text: "#155E75" },
    RUNNING: { border: "#3B82F6", bg: "#EFF6FF", text: "#1E40AF" },
    SELECTED: { border: "#3B82F6", bg: "#EFF6FF", text: "#1E40AF" },
    PROMOTED_TO_PAPER: { border: "#22C55E", bg: "#DCFCE7", text: "#166534" },
    NEGATIVE_RESULT: { border: "#EF4444", bg: "#FEE2E2", text: "#991B1B" },
    SUPERSEDED: { border: "#9CA3AF", bg: "#F3F4F6", text: "#374151" },
    ARCHIVED: { border: "#9CA3AF", bg: "#F3F4F6", text: "#374151" },
  }

  function statusColor(status) {
    return STATUS_COLORS[status] ?? STATUS_COLORS.ARCHIVED
  }

  function truncate(text, max = 32) {
    if (!text) return "(untitled)"
    const clean = String(text).replace(/\s+/g, " ").trim()
    return clean.length > max ? `${clean.slice(0, max)}…` : clean
  }

  function layout(nodes, edges) {
    const ids = new Set(nodes.map((node) => node.id))
    const lineageEdges = edges.filter(
      (edge) => edge.kind === "lineage" && ids.has(edge.source) && ids.has(edge.target),
    )
    const children = new Map(nodes.map((node) => [node.id, []]))
    const parents = new Map(nodes.map((node) => [node.id, []]))
    for (const edge of lineageEdges) {
      children.get(edge.source).push(edge.target)
      parents.get(edge.target).push(edge.source)
    }

    const depth = new Map()
    const roots = nodes.filter((node) => (parents.get(node.id) ?? []).length === 0)
    const queue = roots.map((node) => ({ id: node.id, depth: 0 }))
    for (const item of queue) {
      if ((depth.get(item.id) ?? 0) < item.depth) depth.set(item.id, item.depth)
      for (const child of children.get(item.id) ?? []) {
        const nextDepth = item.depth + 1
        if ((depth.get(child) ?? -1) < nextDepth) {
          depth.set(child, nextDepth)
          queue.push({ id: child, depth: nextDepth })
        }
      }
    }
    let maxDepth = 0
    for (const node of nodes) {
      const d = depth.get(node.id) ?? maxDepth + 1
      depth.set(node.id, d)
      maxDepth = Math.max(maxDepth, d)
    }
    // Isolated nodes (no lineage at all) form one extra layer at the end.
    const isolated = nodes.filter(
      (node) =>
        (children.get(node.id) ?? []).length === 0 &&
        (parents.get(node.id) ?? []).length === 0,
    )
    if (isolated.length > 0 && nodes.length !== isolated.length) {
      for (const node of isolated) depth.set(node.id, maxDepth + 1)
      maxDepth += 1
    }

    const layers = new Map()
    for (const node of nodes) {
      const d = depth.get(node.id)
      if (!layers.has(d)) layers.set(d, [])
      layers.get(d).push(node)
    }
    const orderedLayers = [...layers.entries()].sort((a, b) => a[0] - b[0])
    for (const [, layer] of orderedLayers) {
      layer.sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.id.localeCompare(b.id))
    }

    // Barycenter sweeps: order each layer by the mean position of connected
    // parents/children, one forward and one backward pass.
    for (let pass = 0; pass < 4; pass++) {
      for (let i = 1; i < orderedLayers.length; i++) {
        const layer = orderedLayers[i][1]
        const prev = new Map(
          orderedLayers[i - 1][1].map((node, index) => [node.id, index]),
        )
        const score = (node) => {
          const ps = parents.get(node.id) ?? []
          if (ps.length === 0) return prev.size / 2
          return ps.reduce((sum, p) => sum + (prev.get(p) ?? prev.size / 2), 0) / ps.length
        }
        layer.sort((a, b) => score(a) - score(b) || a.id.localeCompare(b.id))
      }
      for (let i = orderedLayers.length - 2; i >= 0; i--) {
        const layer = orderedLayers[i][1]
        const next = new Map(
          orderedLayers[i + 1][1].map((node, index) => [node.id, index]),
        )
        const score = (node) => {
          const cs = children.get(node.id) ?? []
          if (cs.length === 0) return next.size / 2
          return cs.reduce((sum, c) => sum + (next.get(c) ?? next.size / 2), 0) / cs.length
        }
        layer.sort((a, b) => score(a) - score(b) || a.id.localeCompare(b.id))
      }
    }

    const positions = new Map()
    for (const [layerIndex, layer] of orderedLayers) {
      layer.forEach((node, orderIndex) => {
        positions.set(node.id, {
          x: PAD + layerIndex * (NODE_W + GAP_X),
          y: PAD + orderIndex * (NODE_H + GAP_Y),
        })
      })
    }
    const height = Math.max(
      1,
      PAD * 2 +
        Math.max(...[...positions.values()].map((p) => p.y), 0) +
        NODE_H,
    )
    const width = Math.max(
      1,
      PAD * 2 +
        Math.max(...[...positions.values()].map((p) => p.x), 0) +
        NODE_W,
    )
    return { positions, width, height }
  }

  function render(container, data) {
    const { positions, width, height } = layout(data.nodes, data.edges)
    const svg = document.createElementNS(NS, "svg")
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`)
    svg.setAttribute("width", "100%")
    svg.setAttribute("height", "100%")
    svg.setAttribute("role", "img")
    svg.setAttribute("aria-label", "假设图")

    const defs = document.createElementNS(NS, "defs")
    const marker = document.createElementNS(NS, "marker")
    marker.setAttribute("id", "hyp-graph-arrow")
    marker.setAttribute("viewBox", "0 0 10 10")
    marker.setAttribute("refX", "9")
    marker.setAttribute("refY", "5")
    marker.setAttribute("markerWidth", "7")
    marker.setAttribute("markerHeight", "7")
    marker.setAttribute("orient", "auto-start-reverse")
    const markerPath = document.createElementNS(NS, "path")
    markerPath.setAttribute("d", "M 0 0 L 10 5 L 0 10 z")
    markerPath.setAttribute("fill", "#9AA3B2")
    marker.appendChild(markerPath)
    defs.appendChild(marker)
    svg.appendChild(defs)

    for (const edge of data.edges) {
      const source = positions.get(edge.source)
      const target = positions.get(edge.target)
      if (!source || !target) continue
      const path = document.createElementNS(NS, "path")
      const supersedes = edge.kind === "supersedes"
      const x1 = source.x + NODE_W
      const y1 = source.y + NODE_H * 0.25
      const x2 = target.x
      const y2 = target.y + NODE_H * 0.25
      const dx = Math.max(24, Math.abs(x2 - x1) * 0.45)
      const d = supersedes
        ? `M ${x1} ${y1} C ${x1 + dx} ${y1 - 26}, ${x2 - dx} ${y2 - 26}, ${x2} ${y2}`
        : `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
      path.setAttribute("d", d)
      path.setAttribute("fill", "none")
      path.setAttribute("stroke", supersedes ? "#DC2626" : "#9AA3B2")
      path.setAttribute("stroke-width", supersedes ? "1.4" : "1.6")
      path.setAttribute("stroke-dasharray", supersedes ? "6 5" : "")
      path.setAttribute("marker-end", "url(#hyp-graph-arrow)")
      svg.appendChild(path)
    }

    for (const node of data.nodes) {
      const pos = positions.get(node.id)
      if (!pos) continue
      const colors = statusColor(node.status)
      const group = document.createElementNS(NS, "g")
      group.setAttribute("transform", `translate(${pos.x} ${pos.y})`)
      group.setAttribute("tabindex", "0")

      const rect = document.createElementNS(NS, "rect")
      rect.setAttribute("width", String(NODE_W))
      rect.setAttribute("height", String(NODE_H))
      rect.setAttribute("rx", "10")
      rect.setAttribute("fill", colors.bg)
      rect.setAttribute("stroke", colors.border)
      rect.setAttribute("stroke-width", "1.4")
      group.appendChild(rect)

      const title = document.createElementNS(NS, "text")
      title.setAttribute("x", "12")
      title.setAttribute("y", "22")
      title.setAttribute("fill", "#0F172A")
      title.setAttribute("font-size", "12")
      title.setAttribute("font-weight", "600")
      title.textContent = truncate(node.statement)
      group.appendChild(title)

      const meta = document.createElementNS(NS, "text")
      meta.setAttribute("x", "12")
      meta.setAttribute("y", "40")
      meta.setAttribute("fill", "#5B6472")
      meta.setAttribute("font-size", "11")
      meta.textContent =
        node.primary !== null && node.primary !== undefined
          ? `primary ${Number(node.primary).toFixed(4)}`
          : node.experiment_id
            ? `experiment ${node.experiment_id}`
            : "no experiment"
      group.appendChild(meta)

      const badge = document.createElementNS(NS, "text")
      badge.setAttribute("x", "12")
      badge.setAttribute("y", "55")
      badge.setAttribute("fill", colors.text)
      badge.setAttribute("font-size", "10")
      badge.textContent = `${node.status}${node.sota ? " ★ SOTA" : ""}`
      group.appendChild(badge)

      const tip = document.createElementNS(NS, "title")
      tip.textContent = [
        node.id,
        node.statement,
        `${node.status}${node.sota ? " · SOTA" : ""}`,
        node.primary !== null && node.primary !== undefined
          ? `primary: ${Number(node.primary).toFixed(4)}`
          : "no experiment",
        node.sources.length ? `sources: ${node.sources.join(", ")}` : "",
      ]
        .filter(Boolean)
        .join("\n")
      group.appendChild(tip)

      svg.appendChild(group)
    }

    container.replaceChildren(svg)
    installPanZoom(container, svg, width, height)
  }

  function installPanZoom(container, svg, width, height) {
    let viewBox = { x: 0, y: 0, w: width, h: height }
    let drag = null
    const clamp = (value, min, max) => Math.min(max, Math.max(min, value))

    const apply = () => {
      svg.setAttribute("viewBox", `${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`)
    }

    container.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault()
        const factor = event.deltaY < 0 ? 0.9 : 1.1
        const cx = viewBox.x + (event.offsetX / container.clientWidth) * viewBox.w
        const cy = viewBox.y + (event.offsetY / container.clientHeight) * viewBox.h
        const w = clamp(viewBox.w * factor, 80, width * 2)
        const h = clamp(viewBox.h * factor, 80, height * 2)
        viewBox = {
          x: clamp(cx - (cx - viewBox.x) * (w / viewBox.w), 0, width - w),
          y: clamp(cy - (cy - viewBox.y) * (h / viewBox.h), 0, height - h),
          w,
          h,
        }
        apply()
      },
      { passive: false },
    )

    container.addEventListener("pointerdown", (event) => {
      drag = { x: event.clientX, y: event.clientY, viewBox: { ...viewBox } }
      container.setPointerCapture(event.pointerId)
    })
    container.addEventListener("pointermove", (event) => {
      if (!drag) return
      const scale = viewBox.w / container.clientWidth
      const dx = (event.clientX - drag.x) * scale
      const dy = (event.clientY - drag.y) * scale
      viewBox = {
        x: clamp(drag.viewBox.x - dx, 0, width - viewBox.w),
        y: clamp(drag.viewBox.y - dy, 0, height - viewBox.h),
        w: viewBox.w,
        h: viewBox.h,
      }
      apply()
    })
    container.addEventListener("pointerup", () => {
      drag = null
    })
    container.addEventListener("dblclick", () => {
      viewBox = { x: 0, y: 0, w: width, h: height }
      apply()
    })
  }

  async function refresh(container, lastFingerprint) {
    try {
      const response = await fetch("/api/hypothesis_graph")
      const data = await response.json()
      const fingerprint = JSON.stringify(data)
      if (fingerprint !== lastFingerprint.value) {
        lastFingerprint.value = fingerprint
        render(container, data)
        const summary = document.getElementById("graph-summary")
        if (summary) {
          summary.textContent =
            data.nodes.length === 0
              ? "暂无假设图数据。启动 AutoResearch 后，这里会展示谱系与取代关系。"
              : `${data.nodes.length} 个假设 · ${data.edges.filter((e) => e.kind === "lineage").length} 条谱系 · ${data.edges.filter((e) => e.kind === "supersedes").length} 条取代`
        }
      }
    } catch {
      /* keep the previous graph on transient network errors */
    }
  }

  window.mountHypothesisGraph = (container) => {
    if (!container) return
    const lastFingerprint = { value: "" }
    refresh(container, lastFingerprint)
    setInterval(() => refresh(container, lastFingerprint), 3000)
  }
})()
