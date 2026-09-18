"""
Dashboard Controls — Interactive JavaScript overlays for Plotly dashboards.

Generates injectable JS/CSS for:
    1. Box-select → floating stats card (avg/min/max/p50/p95)
    2. Zoom/pan → persistent stats bar at top showing visible range metrics
    3. Control panel — sidebar with panel toggles + normalized overlay mode

This module is dashboard-agnostic: it works with any Plotly multi-subplot figure.
"""


def get_stats_css() -> str:
    """CSS for floating stats card and persistent stats bar."""
    return """
<style>
/* ═══ Selection Stats Card ═══ */
#sel-stats-card {
    position: fixed;
    top: 80px;
    right: 20px;
    background: rgba(255, 255, 255, 0.97);
    border: 1px solid #ccc;
    border-radius: 8px;
    padding: 12px 16px;
    min-width: 280px;
    max-width: 400px;
    max-height: 60vh;
    overflow-y: auto;
    box-shadow: 0 4px 16px rgba(0,0,0,0.15);
    z-index: 10000;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 12px;
    display: none;
    transition: opacity 0.2s;
}
#sel-stats-card.visible { display: block; opacity: 1; }
#sel-stats-card .card-title {
    font-size: 13px;
    font-weight: 600;
    color: #333;
    margin-bottom: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
#sel-stats-card .card-title .close-btn {
    cursor: pointer;
    color: #888;
    font-size: 16px;
    line-height: 1;
    padding: 2px 6px;
    border-radius: 4px;
}
#sel-stats-card .card-title .close-btn:hover { background: #eee; color: #333; }
#sel-stats-card table { width: 100%; border-collapse: collapse; }
#sel-stats-card th {
    text-align: left;
    padding: 3px 6px;
    color: #666;
    font-weight: 500;
    border-bottom: 1px solid #eee;
}
#sel-stats-card td {
    padding: 3px 6px;
    text-align: right;
    font-variant-numeric: tabular-nums;
}
#sel-stats-card td:first-child { text-align: left; font-weight: 500; }
#sel-stats-card tr:hover { background: #f5f8ff; }
#sel-stats-card .metric-name { color: #333; }
#sel-stats-card .range-info {
    font-size: 11px;
    color: #888;
    margin-bottom: 6px;
    padding-bottom: 4px;
    border-bottom: 1px solid #eee;
}

/* ═══ Zoom-Range Stats Bar ═══ */
#zoom-stats-bar {
    position: sticky;
    top: 0;
    z-index: 9999;
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    color: #e0e0e0;
    padding: 8px 16px;
    display: none;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}
#zoom-stats-bar.visible { display: flex; }
#zoom-stats-bar .stat-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: rgba(255,255,255,0.08);
    border-radius: 4px;
    padding: 3px 8px;
}
#zoom-stats-bar .stat-chip .label { color: #aaa; font-size: 11px; }
#zoom-stats-bar .stat-chip .value { font-weight: 600; font-variant-numeric: tabular-nums; }
#zoom-stats-bar .zoom-range {
    font-size: 11px;
    color: #88b4e7;
    margin-right: auto;
}

/* ═══ Control Panel ═══ */
#ctrl-panel {
    position: fixed;
    top: 70px;
    left: 10px;
    background: rgba(255, 255, 255, 0.97);
    border: 1px solid #ddd;
    border-radius: 8px;
    padding: 10px 14px;
    min-width: 200px;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 4px 16px rgba(0,0,0,0.12);
    z-index: 10000;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 12px;
    display: none;
}
#ctrl-panel.visible { display: block; }
#ctrl-panel h4 {
    margin: 0 0 8px 0;
    font-size: 13px;
    color: #333;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
#ctrl-panel .toggle-btn {
    cursor: pointer;
    font-size: 11px;
    color: #0066cc;
    text-decoration: underline;
}
#ctrl-panel .section { margin-bottom: 10px; }
#ctrl-panel .section-title { font-weight: 600; font-size: 11px; color: #666; margin-bottom: 4px; }
#ctrl-panel label {
    display: block;
    padding: 2px 0;
    cursor: pointer;
}
#ctrl-panel label:hover { color: #0066cc; }
#ctrl-panel input[type="checkbox"] { margin-right: 6px; }
#ctrl-panel .norm-toggle {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid #eee;
}
#ctrl-panel .panel-row {
    display: flex;
    align-items: center;
    padding: 2px 0;
}
#ctrl-panel .panel-row label { flex: 1; padding: 0; }
#ctrl-panel .collapse-btn {
    cursor: pointer;
    font-size: 10px;
    color: #666;
    background: #f0f0f0;
    border: 1px solid #ddd;
    border-radius: 3px;
    padding: 1px 5px;
    margin-left: 4px;
    line-height: 1.2;
    user-select: none;
}
#ctrl-panel .collapse-btn:hover { background: #e0e0e0; color: #333; }
#ctrl-panel .collapse-btn.collapsed { color: #cc6600; background: #fff3e0; border-color: #ffcc80; }
#ctrl-btn {
    position: fixed;
    top: 70px;
    left: 10px;
    z-index: 9999;
    background: #1a1a2e;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
    font-size: 13px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}
#ctrl-btn:hover { background: #2d2d4a; }
</style>
"""


def get_controls_html(panel_names: list[str]) -> str:
    """Generate HTML elements for stats card, stats bar, and control panel.

    Args:
        panel_names: List of subplot panel names for the control panel checkboxes.
    """
    panel_rows = "\n".join(
        f'        <div class="panel-row">'
        f'<label><input type="checkbox" checked data-panel="{i+1}" '
        f'onchange="togglePanel({i+1}, this.checked)">{name}</label>'
        f'<span class="collapse-btn" id="collapse-btn-{i+1}" '
        f'onclick="toggleCollapse({i+1})" title="Collapse/Expand panel">'
        f'\u25BC</span></div>'
        for i, name in enumerate(panel_names)
    )

    return f"""
<!-- ═══ Zoom Stats Bar ═══ -->
<div id="zoom-stats-bar">
    <span class="zoom-range" id="zoom-range-text">Viewing: full range</span>
</div>

<!-- ═══ Selection Stats Card ═══ -->
<div id="sel-stats-card">
    <div class="card-title">
        <span>📊 Selection Statistics</span>
        <span class="close-btn" onclick="hideSelStats()">✕</span>
    </div>
    <div class="range-info" id="sel-range-info"></div>
    <table id="sel-stats-table">
        <thead><tr><th>Metric</th><th>Avg</th><th>Min</th><th>Max</th><th>P50</th><th>P95</th></tr></thead>
        <tbody></tbody>
    </table>
</div>

<!-- ═══ Control Panel ═══ -->
<button id="ctrl-btn" onclick="toggleCtrlPanel()">⚙ Panels</button>
<div id="ctrl-panel">
    <h4>Panel Controls <span class="toggle-btn" onclick="toggleCtrlPanel()">close</span></h4>
    <div class="section">
        <div class="section-title">Visible Panels <span style="font-size:10px;color:#888;font-weight:normal;">(▼ = collapse)</span></div>
{panel_rows}
    </div>
    <div class="section norm-toggle">
        <div class="section-title">Overlay Mode</div>
        <label><input type="checkbox" id="norm-checkbox" onchange="toggleNormalized(this.checked)">
            Normalized (0-100%) overlay</label>
        <div style="font-size:10px;color:#888;margin-top:2px;">Real values preserved in hover</div>
    </div>
</div>
"""


def get_controls_js(n_panels: int) -> str:
    """Generate the JavaScript for interactive controls.

    Args:
        n_panels: Number of subplot rows in the figure.
    """
    return f"""
<script>
(function() {{
    'use strict';

    const N_PANELS = {n_panels};
    const PANEL_HEIGHT = 300;
    const plotDiv = document.querySelector('.plotly-graph-div') || document.querySelector('[id^="plotly-"]');
    if (!plotDiv) {{ console.warn('Dashboard controls: no Plotly div found'); return; }}

    // Map Plotly y-axis ref to panel index (1-based).
    // With secondary_y, panel N uses yaxis(2N-1) primary + yaxis(2N) secondary.
    function yRefToPanel(yRef) {{
        if (!yRef || yRef === 'y') return 1;
        const num = parseInt(yRef.replace('y', ''));
        return isNaN(num) ? 1 : Math.ceil(num / 2);
    }}

    // ═══════════════════════════════════════════════════
    // UTILITY FUNCTIONS
    // ═══════════════════════════════════════════════════
    function percentile(arr, p) {{
        if (!arr.length) return 0;
        const sorted = [...arr].sort((a, b) => a - b);
        const idx = (p / 100) * (sorted.length - 1);
        const lo = Math.floor(idx), hi = Math.ceil(idx);
        return lo === hi ? sorted[lo] : sorted[lo] * (hi - idx) + sorted[hi] * (idx - lo);
    }}

    function fmt(v, digits) {{
        if (v === null || v === undefined || isNaN(v)) return '—';
        digits = digits !== undefined ? digits : 2;
        if (Math.abs(v) >= 1000) return v.toFixed(0);
        if (Math.abs(v) >= 100) return v.toFixed(1);
        return v.toFixed(digits);
    }}

    function getVisibleTraceData(xRange) {{
        // Collect data for all visible traces within the x-range
        const traces = plotDiv.data || [];
        const results = [];
        for (let i = 0; i < traces.length; i++) {{
            const t = traces[i];
            if (!t.visible || t.visible === 'legendonly') continue;
            if (!t.x || !t.y || t.y.length === 0) continue;
            if (!t.name) continue;

            // Filter to x-range
            const xArr = t.x;
            const yArr = t.y;
            const filtered = [];
            for (let j = 0; j < xArr.length; j++) {{
                const xVal = new Date(xArr[j]).getTime();
                if (xRange && (xVal < xRange[0] || xVal > xRange[1])) continue;
                const yVal = parseFloat(yArr[j]);
                if (!isNaN(yVal)) filtered.push(yVal);
            }}
            if (filtered.length < 2) continue;

            const sum = filtered.reduce((a, b) => a + b, 0);
            results.push({{
                name: t.name,
                color: (t.line && t.line.color) || '#333',
                avg: sum / filtered.length,
                min: Math.min(...filtered),
                max: Math.max(...filtered),
                p50: percentile(filtered, 50),
                p95: percentile(filtered, 95),
                count: filtered.length,
            }});
        }}
        return results;
    }}

    // ═══════════════════════════════════════════════════
    // SELECTION STATS (Box/Lasso Select)
    // ═══════════════════════════════════════════════════
    const selCard = document.getElementById('sel-stats-card');
    const selTable = document.getElementById('sel-stats-table').querySelector('tbody');
    const selRangeInfo = document.getElementById('sel-range-info');

    window.hideSelStats = function() {{
        selCard.classList.remove('visible');
    }};

    plotDiv.on('plotly_selected', function(eventData) {{
        if (!eventData || !eventData.range) {{
            selCard.classList.remove('visible');
            return;
        }}

        const xRange = eventData.range.x;
        const x0 = new Date(xRange[0]);
        const x1 = new Date(xRange[1]);
        const durationMs = x1 - x0;
        const durationStr = durationMs < 60000
            ? (durationMs / 1000).toFixed(1) + 's'
            : (durationMs / 60000).toFixed(1) + 'min';

        selRangeInfo.textContent =
            x0.toLocaleTimeString() + ' → ' + x1.toLocaleTimeString() +
            ' (' + durationStr + ')';

        const xRangeMs = [x0.getTime(), x1.getTime()];
        const stats = getVisibleTraceData(xRangeMs);

        // Build table
        selTable.innerHTML = '';
        stats.forEach(function(s) {{
            const row = document.createElement('tr');
            row.innerHTML =
                '<td class="metric-name" style="color:' + s.color + '">' + s.name + '</td>' +
                '<td>' + fmt(s.avg) + '</td>' +
                '<td>' + fmt(s.min) + '</td>' +
                '<td>' + fmt(s.max) + '</td>' +
                '<td>' + fmt(s.p50) + '</td>' +
                '<td>' + fmt(s.p95) + '</td>';
            selTable.appendChild(row);
        }});

        selCard.classList.add('visible');
    }});

    plotDiv.on('plotly_deselect', function() {{
        selCard.classList.remove('visible');
    }});

    // ═══════════════════════════════════════════════════
    // ZOOM-RANGE STATS BAR
    // ═══════════════════════════════════════════════════
    const zoomBar = document.getElementById('zoom-stats-bar');
    const zoomRangeText = document.getElementById('zoom-range-text');
    let zoomDebounce = null;

    function updateZoomStats() {{
        const layout = plotDiv.layout || {{}};
        // Get the x-axis range (first subplot's x-axis)
        const xaxis = layout.xaxis || {{}};
        const range = xaxis.range;

        if (!range || range.length < 2) {{
            zoomBar.classList.remove('visible');
            return;
        }}

        const x0 = new Date(range[0]);
        const x1 = new Date(range[1]);

        // Check if this is effectively the full range (no zoom)
        const data = plotDiv.data || [];
        let fullMin = Infinity, fullMax = -Infinity;
        for (let i = 0; i < data.length; i++) {{
            if (data[i].x && data[i].x.length > 0) {{
                const first = new Date(data[i].x[0]).getTime();
                const last = new Date(data[i].x[data[i].x.length - 1]).getTime();
                fullMin = Math.min(fullMin, first);
                fullMax = Math.max(fullMax, last);
            }}
        }}

        // If viewing 95%+ of full range, hide the bar
        const viewSpan = x1 - x0;
        const fullSpan = fullMax - fullMin;
        if (fullSpan > 0 && viewSpan / fullSpan > 0.95) {{
            zoomBar.classList.remove('visible');
            return;
        }}

        const durationMs = x1 - x0;
        const durationStr = durationMs < 60000
            ? (durationMs / 1000).toFixed(0) + 's'
            : (durationMs / 60000).toFixed(1) + 'min';
        zoomRangeText.textContent =
            'Viewing: ' + x0.toLocaleTimeString() + ' → ' +
            x1.toLocaleTimeString() + ' (' + durationStr + ')';

        // Compute stats for key metrics in visible range
        const xRangeMs = [x0.getTime(), x1.getTime()];
        const stats = getVisibleTraceData(xRangeMs);

        // Show top 6 metrics as chips (by priority — main metrics first)
        const priorityNames = ['CPU Total %', 'DRAM Total BW', 'SoC Total (W)',
                               'NVIDIA Compute %', 'DRAM Read Latency (ns)', 'NPU'];
        const chipStats = [];
        priorityNames.forEach(function(pn) {{
            const found = stats.find(function(s) {{ return s.name.includes(pn.split(' ')[0]); }});
            if (found) chipStats.push(found);
        }});
        // Fill remaining with other traces
        stats.forEach(function(s) {{
            if (chipStats.length < 8 && !chipStats.includes(s)) chipStats.push(s);
        }});

        // Remove old chips, keep range text
        const existingChips = zoomBar.querySelectorAll('.stat-chip');
        existingChips.forEach(function(c) {{ c.remove(); }});

        chipStats.slice(0, 8).forEach(function(s) {{
            const chip = document.createElement('span');
            chip.className = 'stat-chip';
            chip.innerHTML = '<span class="label">' + s.name.substring(0, 20) + '</span>' +
                '<span class="value" style="color:' + s.color + '">avg ' + fmt(s.avg) + '</span>';
            zoomBar.appendChild(chip);
        }});

        zoomBar.classList.add('visible');
    }}

    plotDiv.on('plotly_relayout', function(eventData) {{
        if (zoomDebounce) clearTimeout(zoomDebounce);
        zoomDebounce = setTimeout(updateZoomStats, 150);
    }});

    // ═══════════════════════════════════════════════════
    // CONTROL PANEL
    // ═══════════════════════════════════════════════════
    const ctrlPanel = document.getElementById('ctrl-panel');

    window.toggleCtrlPanel = function() {{
        ctrlPanel.classList.toggle('visible');
    }};

    window.togglePanel = function(panelIdx, show) {{
        // Find traces belonging to this panel and toggle visibility
        const traces = plotDiv.data || [];
        const indices = [];
        for (let i = 0; i < traces.length; i++) {{
            if (yRefToPanel(traces[i].yaxis) === panelIdx) {{
                indices.push(i);
            }}
        }}
        if (indices.length > 0) {{
            const vis = show ? true : 'legendonly';
            Plotly.restyle(plotDiv, {{ visible: Array(indices.length).fill(vis) }}, indices);
        }}
    }};

    // ═══════════════════════════════════════════════════
    // COLLAPSE / EXPAND PANELS
    // ═══════════════════════════════════════════════════
    const collapsedSet = new Set();      // set of collapsed panel indices
    const VERT_SPACING = 0.03;           // matches make_subplots vertical_spacing
    const COLLAPSED_PX = 15;             // pixel height for a collapsed strip

    function recomputeLayout() {{
        // Calculate new figure height based on expanded/collapsed panels
        const expandedCount = N_PANELS - collapsedSet.size;
        const collapsedCount = collapsedSet.size;
        const newHeight = expandedCount * PANEL_HEIGHT + collapsedCount * COLLAPSED_PX;

        // Allocate domain fractions proportionally
        const totalGap = (N_PANELS - 1) * VERT_SPACING;
        const available = 1.0 - totalGap;

        // Collapsed panels get a tiny slice; expanded panels share the rest
        const collapsedFrac = collapsedCount > 0
            ? (COLLAPSED_PX / newHeight) * available : 0;
        const expandedTotal = available - collapsedCount * collapsedFrac;
        const expandedFrac = expandedCount > 0 ? expandedTotal / expandedCount : 0;

        // Assign domains top-to-bottom (row 1 = top in make_subplots)
        const layoutUpdate = {{ height: newHeight }};
        let top = 1.0;
        for (let p = 1; p <= N_PANELS; p++) {{
            const isCollapsed = collapsedSet.has(p);
            const frac = isCollapsed ? collapsedFrac : expandedFrac;
            const bot = Math.max(top - frac, 0);

            const primKey = p === 1 ? 'yaxis' : ('yaxis' + (2 * p - 1));
            const secKey  = 'yaxis' + (2 * p);

            layoutUpdate[primKey + '.domain'] = [bot, top];
            layoutUpdate[secKey  + '.domain'] = [bot, top];

            if (isCollapsed) {{
                layoutUpdate[primKey + '.showticklabels'] = false;
                layoutUpdate[primKey + '.title.text'] = '';
                layoutUpdate[secKey  + '.showticklabels'] = false;
                layoutUpdate[secKey  + '.title.text'] = '';
            }} else {{
                layoutUpdate[primKey + '.showticklabels'] = true;
                layoutUpdate[primKey + '.title.text'] = null;
                layoutUpdate[secKey  + '.showticklabels'] = true;
                layoutUpdate[secKey  + '.title.text'] = null;
            }}

            top = bot - VERT_SPACING;
        }}

        // Build trace visibility — single array for all traces
        const traces = plotDiv.data || [];
        const vis = [];
        const allIndices = [];
        for (let i = 0; i < traces.length; i++) {{
            const panel = yRefToPanel(traces[i].yaxis);
            vis.push(collapsedSet.has(panel) ? 'legendonly' : true);
            allIndices.push(i);
        }}

        // Single atomic Plotly.update — data + layout in one call
        Plotly.update(plotDiv, {{ visible: vis }}, layoutUpdate, allIndices);
    }}

    window.toggleCollapse = function(panelIdx) {{
        if (collapsedSet.has(panelIdx)) {{
            collapsedSet.delete(panelIdx);
        }} else {{
            collapsedSet.add(panelIdx);
        }}
        recomputeLayout();

        // Update button appearance
        const btn = document.getElementById('collapse-btn-' + panelIdx);
        if (btn) {{
            const isCol = collapsedSet.has(panelIdx);
            btn.textContent = isCol ? '\u25B6' : '\u25BC';
            btn.classList.toggle('collapsed', isCol);
            btn.title = isCol ? 'Expand panel' : 'Collapse panel';
        }}
    }};

    // ═══════════════════════════════════════════════════
    // NORMALIZED OVERLAY MODE
    // ═══════════════════════════════════════════════════
    let originalData = null;

    window.toggleNormalized = function(enabled) {{
        const traces = plotDiv.data;
        if (!traces || traces.length === 0) return;

        if (enabled) {{
            // Save original data
            originalData = traces.map(function(t) {{
                return {{ y: t.y ? [...t.y] : [], hovertemplate: t.hovertemplate }};
            }});

            // Normalize each trace to 0-100% of its own range
            for (let i = 0; i < traces.length; i++) {{
                const t = traces[i];
                if (!t.y || t.y.length === 0) continue;

                const yVals = t.y.filter(function(v) {{ return !isNaN(v) && v !== null; }});
                if (yVals.length === 0) continue;

                const yMin = Math.min(...yVals);
                const yMax = Math.max(...yVals);
                const range = yMax - yMin || 1;

                // Store real values in customdata for hover
                const normalized = [];
                const customdata = [];
                for (let j = 0; j < t.y.length; j++) {{
                    const real = t.y[j];
                    normalized.push(isNaN(real) ? null : ((real - yMin) / range) * 100);
                    customdata.push(real);
                }}

                Plotly.restyle(plotDiv, {{
                    y: [normalized],
                    customdata: [customdata.map(function(v) {{ return [v]; }})],
                    hovertemplate: [t.name + ': %{{customdata[0]:.2f}} (norm: %{{y:.0f}}%)<extra></extra>'],
                }}, [i]);
            }}

            // Set all y-axes to 0-100 (primary + secondary per panel)
            const layoutUpdate = {{}};
            for (let r = 1; r <= N_PANELS; r++) {{
                const primKey = r === 1 ? 'yaxis' : ('yaxis' + (2 * r - 1));
                const secKey  = 'yaxis' + (2 * r);
                layoutUpdate[primKey + '.range'] = [0, 100];
                layoutUpdate[primKey + '.title.text'] = 'Normalized %';
                layoutUpdate[secKey  + '.range'] = [0, 100];
                layoutUpdate[secKey  + '.title.text'] = '';
            }}
            Plotly.relayout(plotDiv, layoutUpdate);

        }} else if (originalData) {{
            // Restore original data
            for (let i = 0; i < originalData.length; i++) {{
                if (i < plotDiv.data.length) {{
                    Plotly.restyle(plotDiv, {{
                        y: [originalData[i].y],
                        hovertemplate: [originalData[i].hovertemplate || null],
                        customdata: [null],
                    }}, [i]);
                }}
            }}
            // Reset y-axes to autorange (primary + secondary per panel)
            const layoutUpdate = {{}};
            for (let r = 1; r <= N_PANELS; r++) {{
                const primKey = r === 1 ? 'yaxis' : ('yaxis' + (2 * r - 1));
                const secKey  = 'yaxis' + (2 * r);
                layoutUpdate[primKey + '.autorange'] = true;
                layoutUpdate[primKey + '.title.text'] = null;
                layoutUpdate[secKey  + '.autorange'] = true;
                layoutUpdate[secKey  + '.title.text'] = null;
            }}
            Plotly.relayout(plotDiv, layoutUpdate);
            originalData = null;
        }}
    }};

    // ═══════════════════════════════════════════════════
    // ENABLE BOX-SELECT MODE BY DEFAULT
    // ═══════════════════════════════════════════════════
    // Set dragmode to 'select' so box-select works out of the box
    // Users can switch back to zoom via the modebar
    Plotly.relayout(plotDiv, {{ dragmode: 'select' }});

    console.log('Dashboard controls initialized: selection stats, zoom stats, control panel');
}})();
</script>
"""


def inject_dashboard_controls(html_content: str, panel_names: list[str], n_panels: int) -> str:
    """Inject interactive controls into an existing Plotly HTML dashboard.

    Args:
        html_content: The full HTML string from fig.write_html()
        panel_names: List of panel/subplot names for control panel checkboxes
        n_panels: Number of subplot rows

    Returns:
        Modified HTML with controls injected.
    """
    css = get_stats_css()
    controls_html = get_controls_html(panel_names)
    controls_js = get_controls_js(n_panels)

    # Inject CSS before </head>
    html_content = html_content.replace("</head>", css + "\n</head>", 1)

    # Inject HTML controls and JS before </body>
    html_content = html_content.replace(
        "</body>",
        controls_html + "\n" + controls_js + "\n</body>",
        1,
    )

    return html_content
