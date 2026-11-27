import QtQuick
import QtQuick.Controls
import QtMultimedia
import QtQuick.Layouts

ApplicationWindow {
    id: root
    title: "greenpulse — Power Market"
    width: 1920
    height: 1080
    visible: true
    color: "#141a18"

    property string selectedZone: "ALL"
    property real hoverTimestampMs: -1

    property real nowMs: Date.now()
    Timer {
        interval: 30000; running: true; repeat: true
        onTriggered: root.nowMs = Date.now()
    }

    // Chart/map window: the modelled delivery day when a model result exists
    // (FORECAST = tomorrow), otherwise the observed ENTSO-E day.
    function modelDayStartMs() {
        const rows = backend.metadata.modelledPriceSeriesB || [];
        const src = rows.length > 0 ? rows : (backend.metadata.modelledPriceSeriesA || []);
        return src.length > 0 ? Date.parse(src[0].delivery_day_start_utc) : NaN;
    }
    function modelDayEndMs() {
        const rows = backend.metadata.modelledPriceSeriesB || [];
        const src = rows.length > 0 ? rows : (backend.metadata.modelledPriceSeriesA || []);
        return src.length > 0 ? Date.parse(src[0].delivery_day_end_utc) : NaN;
    }
    function chartStartMs() {
        const m = modelDayStartMs();
        return Number.isFinite(m) ? m : Date.parse(backend.metadata.observedPriceDayStartUtc);
    }
    function chartEndMs() {
        const m = modelDayEndMs();
        return Number.isFinite(m) ? m : Date.parse(backend.metadata.observedPriceDayEndUtc);
    }

    // Modelled record for one area at the current MTU (Approach B, A fills gaps).
    // Before the day starts the first MTU is used, after it ends the last one.
    function modelledAtNow(areaId) {
        const byTs = {};
        const series = [backend.metadata.modelledPriceSeriesA || [], backend.metadata.modelledPriceSeriesB || []];
        for (const rows of series)
            for (const rec of rows)
                if (rec.area === areaId) byTs[rec.timestamp_ms] = rec;
        const keys = Object.keys(byTs).map(Number).sort(function(a, b) { return a - b; });
        if (keys.length === 0) return null;
        let pick = keys[0];
        for (const k of keys) { if (k <= nowMs) pick = k; else break; }
        return byTs[pick];
    }

    function displayedPrice(areaId) {
        if (backend.metadata.dataSourceMode === "MODELLED") {
            const rec = modelledAtNow(areaId);
            if (rec) return Number(rec.price_eur_mwh);
        }
        return backend.metadata["price" + areaId];
    }
    function displayedAverage() {
        let sum = 0, n = 0;
        for (const a of ["NO1", "NO2", "NO3", "NO4", "NO5"]) {
            if (!hasDisplayedPriceFor(a)) continue;
            sum += displayedPrice(a); n++;
        }
        return n > 0 ? sum / n : 0;
    }

    // Latest Statnett physical flow per directed pair, then net per corridor.
    // Direction convention assumed: Value is the flow Out -> In (see CHANGES.md).
    function physicalCorridors() {
        const latest = {};
        for (const r of (backend.metadata.physicalFlows || [])) {
            const inA = String(r.InAreaElspotId || r.in_area || "");
            const outA = String(r.OutAreaElspotId || r.out_area || "");
            const v = Number(r.Value !== undefined ? r.Value : r.value_mw);
            const ts = Date.parse(r.MeasureDate || r.timestamp_utc);
            if (!inA || !outA || !Number.isFinite(v)) continue;
            const key = outA + ">" + inA;
            if (!latest[key] || ts >= latest[key].ts) latest[key] = { out: outA, inn: inA, v: v, ts: ts };
        }
        const pairs = {};
        let newest = 0;
        for (const k in latest) {
            const e = latest[k];
            const a = e.out < e.inn ? e.out : e.inn;
            const b = e.out < e.inn ? e.inn : e.out;
            const id = a + "|" + b;
            if (!pairs[id]) pairs[id] = { a: a, b: b, net: 0 };
            pairs[id].net += (e.out === a) ? e.v : -e.v;   // positive = a -> b
            if (Number.isFinite(e.ts)) newest = Math.max(newest, e.ts);
        }
        return { pairs: pairs, newestMs: newest };
    }

    function selectChartZone(areaId) {
        selectedZone = areaId;
        priceCanvas.requestPaint();
    }

    function formatChartTime(timestampMs) {
        const d = new Date(timestampMs);
        const hh = String(d.getHours()).padStart(2, "0");
        const mm = String(d.getMinutes()).padStart(2, "0");
        return hh + ":" + mm;
    }

    function hasDisplayedPriceFor(areaId) {
        return backend.metadata.dataSourceMode === "MODELLED"
            ? backend.metadata.hasModelledPrices
            : backend.metadata["hasObservedPrice" + areaId];
    }

    function pointAtTime(points, targetMs) {
        if (!points || points.length === 0)
            return null;

        // Forutsetter at punktene er sortert etter timestamp_ms.
        // Finn siste datapunkt som har startet før eller ved markørtiden.
        let low = 0;
        let high = points.length;

        while (low < high) {
            const mid = Math.floor((low + high) / 2);

            if (Number(points[mid].timestamp_ms) <= targetMs)
                low = mid + 1;
            else
                high = mid;
        }

        const index = low - 1;

        if (index < 0)
            return null;

        const point = points[index];

        // Finn medianen av tidsavstanden mellom datapunktene.
        // Dette hindrer at vi viser en gammel pris over lange datagap.
        const intervals = [];

        for (let i = 1; i < points.length; i++) {
            const dt = Number(points[i].timestamp_ms)
                    - Number(points[i - 1].timestamp_ms);

            if (dt > 0)
                intervals.push(dt);
        }

        if (intervals.length > 0) {
            intervals.sort(function(a, b) { return a - b; });

            const nominalInterval =
                intervals[Math.floor(intervals.length / 2)];

            if (targetMs - Number(point.timestamp_ms)
                > nominalInterval * 1.05) {
                return null;
            }
        } else if (targetMs - Number(point.timestamp_ms) > 15 * 60 * 1000) {
            return null;
        }

        return point;
    }

    // Video Output (hvis video eller animert bakgrunn kjøres)
    VideoOutput {
        id: videoOutput
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
        Component.onCompleted: backend.bindVideoSink(videoSink)
    }
    
    // TOP BAR: Logo, Frekvens, Tidsur og Modusvelgere
    Rectangle {
        id: topBar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 74
        color: "#f2161f1c"
        border.color: "#28342e"
        z: 10

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 24
            anchors.rightMargin: 24
            spacing: 14

            Row {
                spacing: 10
                Text {
                    text: "greenpulse"
                    color: "#f8f7f3"
                    font.pixelSize: 22
                    font.bold: true
                    font.family: "Plus Jakarta Sans"
                }
                Text {
                    text: "·"
                    color: "#b98e67"
                    font.pixelSize: 22
                    font.bold: true
                }
                Text {
                    text: "Market analysis"
                    color: "#8da99c"
                    font.pixelSize: 13
                    anchors.verticalCenter: parent.verticalCenter
                    font.family: "Manrope"
                }
            }

            Item { Layout.fillWidth: true }

            // Calculation mode is independent of the OBSERVED/MODELLED chart selector.
            Row {
                spacing: 4
                Repeater {
                    model: ["REPLAY", "FORECAST"]
                    delegate: Rectangle {
                        required property string modelData
                        width: 82
                        height: 36
                        radius: 6
                        color: backend.metadata.dayAheadMode === modelData ? "#70583e" : "#1f2724"
                        border.color: backend.metadata.dayAheadMode === modelData ? "#d4a373" : "#36443e"
                        Text {
                            anchors.centerIn: parent
                            text: parent.modelData
                            color: backend.metadata.dayAheadMode === parent.modelData ? "#ffffff" : "#8da99c"
                            font.pixelSize: 10
                            font.bold: true
                            font.family: "Inter"
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: backend.metadata.setDayAheadMode(parent.modelData)
                        }
                    }
                }
            }

            // OBSERVED VS MODELLED: chooses which price series the dashboard emphasizes.
            Row {
                spacing: 6
                Rectangle {
                    width: 100
                    height: 36
                    radius: 6
                    color: backend.metadata.dataSourceMode === "OBSERVED" ? "#304d42" : "#1f2724"
                    border.color: backend.metadata.dataSourceMode === "OBSERVED" ? "#526d60" : "#36443e"
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { backend.metadata.setDataSourceMode("OBSERVED") }
                    }
                    Text {
                        anchors.centerIn: parent
                        text: "OBSERVERT"
                        color: backend.metadata.dataSourceMode === "OBSERVED" ? "#ffffff" : "#8da99c"
                        font.pixelSize: 10
                        font.bold: true
                        font.family: "Inter"
                    }
                }

                Rectangle {
                    width: 100
                    height: 36
                    radius: 6
                    color: backend.metadata.dataSourceMode === "MODELLED" ? "#284b63" : "#1f2724"
                    border.color: backend.metadata.dataSourceMode === "MODELLED" ? "#3c6e71" : "#36443e"
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { backend.metadata.setDataSourceMode("MODELLED") }
                    }
                    Text {
                        anchors.centerIn: parent
                        text: "MODELLERT"
                        color: backend.metadata.dataSourceMode === "MODELLED" ? "#ffffff" : "#8da99c"
                        font.pixelSize: 10
                        font.bold: true
                        font.family: "Inter"
                    }
                }
            }

            // SCENARIO-VELGER KNAPPER
            Row {
                spacing: 6
                component ScenButton: Rectangle {
                    property string scId: ""
                    property string label: ""
                    width: 110
                    height: 36
                    radius: 6
                    color: backend.metadata.activeScenario === scId ? "#b98e67" : "#1f2724"
                    border.color: backend.metadata.activeScenario === scId ? "#d4a373" : "#36443e"
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { backend.metadata.setScenario(scId) } 
                    }
                    Text {
                        anchors.centerIn: parent
                        text: label
                        color: backend.metadata.activeScenario === scId ? "#141a18" : "#8da99c"
                        font.pixelSize: 10
                        font.bold: true
                        font.family: "Inter"
                    }
                }

                ScenButton { scId: "normal_base"; label: "NORMAL DRIFT" }
                ScenButton { scId: "cold_snap_winter"; label: "KALDFRONT" }
                ScenButton { scId: "dry_year_low_inflow"; label: "TØRRÅR" }
                ScenButton { scId: "grid_congestion_no1_no2"; label: "FLASKEHALS" }
            }

            // FREKVENS-INDIKATOR
            Rectangle {
                width: 135
                height: 36
                radius: 6
                color: !backend.metadata.hasFrequency ? "#1f2724" : Math.abs(backend.metadata.frequencyHz - 50.0) < 0.05 ? "#1f332a" : "#401c18"
                border.color: !backend.metadata.hasFrequency ? "#36443e" : Math.abs(backend.metadata.frequencyHz - 50.0) < 0.05 ? "#526d60" : "#a55850"

                Row {
                    anchors.centerIn: parent
                    spacing: 6
                    Text {
                        text: "FREQ:"
                        color: "#8da99c"
                        font.pixelSize: 10
                        font.bold: true
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Text {
                        text: backend.metadata.hasFrequency ? backend.metadata.frequencyHz.toFixed(3) + " Hz" : "IKKE TILGJENGELIG"
                        color: "#ffffff"
                        font.pixelSize: backend.metadata.hasFrequency ? 14 : 9
                        font.bold: true
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
            }

            // OSLO TID
            Rectangle {
                width: 125
                height: 36
                radius: 6
                color: "#1f2724"
                border.color: "#36443e"

                Row {
                    anchors.centerIn: parent
                    spacing: 6
                    Text {
                        text: "OSLO:"
                        color: "#8da99c"
                        font.pixelSize: 10
                        font.bold: true
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Text {
                        text: backend.clockString
                        color: "#f8f7f3"
                        font.pixelSize: 14
                        font.bold: true
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
            }
        }
    }

    // SENTER: Skjematisk Nettopologi og Modellavvik-forklaring
    Rectangle {
        id: centerCanvasArea
        anchors.top: topBar.bottom
        anchors.bottom: chartAndStatusArea.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: 18
        color: "transparent"

        RowLayout {
            anchors.fill: parent
            spacing: 20

            // 1. Skjematisk Norgeskart med NO1–NO5 noder og flyt
            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 640
                radius: 12
                color: "#d918201d"
                border.color: "#2a3630"

                Canvas {
                    id: topologyCanvas
                    anchors.fill: parent
                    anchors.margins: 14

                    // Repaint on any data change and on the 30 s "current MTU" tick.
                    Connections { target: backend.metadata; function onChanged() { topologyCanvas.requestPaint(); } }
                    Connections { target: root; function onNowMsChanged() { topologyCanvas.requestPaint(); } }
                    onWidthChanged: requestPaint()
                    onHeightChanged: requestPaint()

                    onPaint: {
                        const ctx = getContext("2d");
                        ctx.clearRect(0, 0, width, height);
                        ctx.textBaseline = "alphabetic";

                        // Title strip
                        ctx.textAlign = "left";
                        ctx.fillStyle = "#e4efe9";
                        ctx.font = "bold 12px Inter, sans-serif";
                        ctx.fillText("NORDISK KRAFTFLYT · NO1–NO5", 4, 14);

                        const nodes = {
                            "NO4": { x: width * 0.62, y: height * 0.17 },
                            "NO3": { x: width * 0.50, y: height * 0.38 },
                            "NO5": { x: width * 0.25, y: height * 0.60 },
                            "NO1": { x: width * 0.60, y: height * 0.62 },
                            "NO2": { x: width * 0.42, y: height * 0.80 }
                        };
                        const R = 30;

                        function arrowHead(x, y, ang, size, color) {
                            ctx.save();
                            ctx.translate(x, y); ctx.rotate(ang);
                            ctx.beginPath();
                            ctx.moveTo(size, 0); ctx.lineTo(-size * 0.7, size * 0.65); ctx.lineTo(-size * 0.7, -size * 0.65);
                            ctx.closePath(); ctx.fillStyle = color; ctx.fill();
                            ctx.restore();
                        }
                        function flowStyle(mw) {
                            const m = Math.abs(mw);
                            return { w: Math.min(7, 1.5 + m / 450), c: m < 1 ? "#3d4e44" : "#5f9c85" };
                        }
                        function tag(cx, cy, text, color) {
                            ctx.font = "bold 10px Inter, sans-serif";
                            const w = ctx.measureText(text).width + 12;
                            ctx.fillStyle = "#161f1b"; ctx.fillRect(cx - w / 2, cy - 9, w, 18);
                            ctx.strokeStyle = "#35463d"; ctx.lineWidth = 1; ctx.strokeRect(cx - w / 2, cy - 9, w, 18);
                            ctx.fillStyle = color; ctx.textAlign = "center";
                            ctx.fillText(text, cx, cy + 4);
                        }

                        const corridors = root.physicalCorridors();
                        const haveFlows = Object.keys(corridors.pairs).length > 0;

                        // Faint grid-skeleton so the map keeps its shape before data arrives.
                        const skeleton = [["NO4","NO3"],["NO3","NO1"],["NO3","NO5"],["NO1","NO5"],["NO1","NO2"],["NO2","NO5"]];
                        ctx.setLineDash([3, 6]); ctx.strokeStyle = "#26332c"; ctx.lineWidth = 1.2;
                        for (const e of skeleton) {
                            ctx.beginPath(); ctx.moveTo(nodes[e[0]].x, nodes[e[0]].y); ctx.lineTo(nodes[e[1]].x, nodes[e[1]].y); ctx.stroke();
                        }
                        ctx.setLineDash([]);

                        // Foreign stubs (anchors by neighbouring country; unknown countries are skipped).
                        const foreignAnchor = {
                            "SE": { x: 0.95, y: 0.30 }, "FI": { x: 0.95, y: 0.08 }, "RU": { x: 0.96, y: 0.03 },
                            "DK": { x: 0.12, y: 0.90 }, "NL": { x: 0.05, y: 0.76 }, "DE": { x: 0.30, y: 0.93 },
                            "GB": { x: 0.04, y: 0.50 }, "LT": { x: 0.92, y: 0.92 }, "PL": { x: 0.80, y: 0.95 }
                        };
                        const foreignIndex = {};
                        const domestic = [];
                        for (const id in corridors.pairs) {
                            const e = corridors.pairs[id];
                            const aNO = e.a.indexOf("NO") === 0 && nodes[e.a];
                            const bNO = e.b.indexOf("NO") === 0 && nodes[e.b];
                            if (aNO && bNO) { domestic.push(e); continue; }
                            if (!aNO && !bNO) continue;
                            const noId = aNO ? e.a : e.b;
                            const fId = aNO ? e.b : e.a;
                            const anchor = foreignAnchor[fId.substring(0, 2)];
                            if (!anchor) continue;
                            const exportMw = aNO ? e.net : -e.net;        // positive = leaving Norway
                            const k = fId.substring(0, 2);
                            foreignIndex[k] = (foreignIndex[k] || 0) + 1;
                            const ax = width * anchor.x, ay = height * anchor.y + (foreignIndex[k] - 1) * 24;
                            const n = nodes[noId];
                            const ang = Math.atan2(ay - n.y, ax - n.x);
                            const sx = n.x + Math.cos(ang) * R, sy = n.y + Math.sin(ang) * R;
                            const st = flowStyle(exportMw);
                            ctx.strokeStyle = st.c; ctx.lineWidth = Math.max(1.2, st.w * 0.7); ctx.setLineDash([2, 4]);
                            ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ax, ay); ctx.stroke(); ctx.setLineDash([]);
                            const mx = (sx + ax) / 2, my = (sy + ay) / 2;
                            const dir = exportMw >= 0 ? ang : ang + Math.PI;
                            const tA = exportMw >= 0 ? 0.80 : 0.22;
                            arrowHead(sx + (ax - sx) * tA, sy + (ay - sy) * tA, dir, 6, st.c);
                            ctx.fillStyle = "#8da99c"; ctx.font = "bold 10px Inter, sans-serif";
                            ctx.textAlign = ax > width * 0.5 ? "right" : "left";
                            ctx.fillText(fId, ax > width * 0.5 ? ax : ax, ay - 6);
                            tag(mx, my, Math.abs(exportMw).toFixed(0) + " MW", "#e4efe9");
                        }

                        // Domestic corridors from Statnett physical flow.
                        for (const e of domestic) {
                            const p1 = nodes[e.a], p2 = nodes[e.b];
                            const ang = Math.atan2(p2.y - p1.y, p2.x - p1.x);
                            const st = flowStyle(e.net);
                            ctx.strokeStyle = st.c; ctx.lineWidth = st.w; ctx.setLineDash([]);
                            ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y); ctx.stroke();
                            const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
                            const dir = e.net >= 0 ? ang : ang + Math.PI;
                            const tA = e.net >= 0 ? 0.66 : 0.34;
                            arrowHead(p1.x + (p2.x - p1.x) * tA, p1.y + (p2.y - p1.y) * tA, dir, 7, st.c);
                            tag(mx, my, Math.abs(e.net).toFixed(0) + " MW", "#e4efe9");
                        }

                        // Nodes: displayed price (current MTU in MODELLED mode) + modelled net position.
                        const avg = root.displayedAverage();
                        const modelled = backend.metadata.dataSourceMode === "MODELLED";
                        const hvdc = [];
                        let mtuMs = NaN;
                        for (const k in nodes) {
                            const n = nodes[k];
                            const has = root.hasDisplayedPriceFor(k);
                            const price = has ? root.displayedPrice(k) : NaN;
                            const rec = root.modelledAtNow(k);
                            const np = rec && rec.net_position_mw !== null && rec.net_position_mw !== undefined
                                ? Number(rec.net_position_mw) : NaN;
                            if (rec) mtuMs = Number(rec.timestamp_ms);

                            const hot = has && avg > 0 && price > avg * 1.12;
                            const cold = has && avg > 0 && price < avg * 0.88;
                            ctx.beginPath(); ctx.arc(n.x, n.y, R, 0, Math.PI * 2);
                            ctx.fillStyle = hot ? "#3a2420" : cold ? "#1c3328" : "#1e2622"; ctx.fill();
                            ctx.lineWidth = 2.5;
                            ctx.strokeStyle = hot ? "#c0705f" : cold ? "#5f9c85" : "#526d60"; ctx.stroke();

                            ctx.textAlign = "center";
                            ctx.fillStyle = "#8da99c"; ctx.font = "bold 10px Inter, sans-serif";
                            ctx.fillText(k, n.x, n.y - 6);
                            ctx.fillStyle = "#ffffff"; ctx.font = "bold 13px 'Plus Jakarta Sans', sans-serif";
                            ctx.fillText(has ? price.toFixed(1) + " €" : "—", n.x, n.y + 11);
                            if (Number.isFinite(np) && modelled) {
                                ctx.font = "bold 10px Inter, sans-serif";
                                ctx.fillStyle = np >= 0 ? "#7fbf9f" : "#d4a373";
                                ctx.fillText((np >= 0 ? "▲ eksport " : "▼ import ") + Math.abs(np).toFixed(0) + " MW", n.x, n.y + R + 14);
                            }
                            const ic = rec ? rec.interconnectors : null;
                            if (ic) for (const z in ic) hvdc.push({ zone: z, v: ic[z] });
                        }

                        // Modelled HVDC / allocation-constraint panel (bottom-left).
                        if (modelled && hvdc.length > 0) {
                            hvdc.sort(function(x, y) { return x.zone < y.zone ? -1 : 1; });
                            const rowH = 15, boxW = 230, boxH = 22 + hvdc.length * rowH;
                            const bx = 0, by = 24;
                            ctx.fillStyle = "#141c18"; ctx.fillRect(bx, by, boxW, boxH);
                            ctx.strokeStyle = "#2a3630"; ctx.strokeRect(bx, by, boxW, boxH);
                            ctx.textAlign = "left"; ctx.fillStyle = "#8da99c"; ctx.font = "bold 9px Inter, sans-serif";
                            ctx.fillText("MODELLERTE UTLANDSFORBINDELSER", bx + 8, by + 13);
                            for (let i = 0; i < hvdc.length; i++) {
                                const h = hvdc[i], v = h.v;
                                const binding = v.binding_cnec !== null && v.binding_cnec !== undefined;
                                ctx.fillStyle = binding ? "#e88f86" : "#c7d8cf"; ctx.font = "10px Inter, sans-serif";
                                const sp = (binding && v.shadow_price_eur_per_mw !== null && v.shadow_price_eur_per_mw !== undefined)
                                    ? "  λ " + Number(v.shadow_price_eur_per_mw).toFixed(1) : "";
                                ctx.fillText((binding ? "● " : "○ ") + h.zone + "  " + Number(v.net_position_mw).toFixed(0) + " MW" + sp,
                                             bx + 8, by + 28 + i * rowH);
                            }
                        }

                        // Footer: data provenance
                        ctx.textAlign = "left"; ctx.fillStyle = "#6f8a7d"; ctx.font = "9px Inter, sans-serif";
                        const fmt = root.formatChartTime;
                        const flowTxt = haveFlows && corridors.newestMs > 0
                            ? "Linjer: Statnett fysisk flyt, siste måling " + fmt(corridors.newestMs)
                            : "Linjer: venter på Statnett fysisk flyt";
                        const mtuTxt = modelled && Number.isFinite(mtuMs)
                            ? " · Noder: modell, MTU " + fmt(mtuMs) : " · Noder: observert ENTSO-E";
                        ctx.fillText(flowTxt + mtuTxt, 0, height - 4);
                        ctx.textAlign = "right";
                        ctx.fillText("Tykkelse ∝ MW · pil = flytretning · ● = bindende JAO-grense", width, height - 4);
                    }
                }
            }

            // 2. Forklaringspanel: Hvorfor modellene viser forskjeller (Modell vs Observert)
            Rectangle {
                Layout.fillHeight: true
                Layout.fillWidth: true
                radius: 12
                color: "#d918201d"
                border.color: "#2a3630"
                clip: true

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 14

                    Row {
                        spacing: 8
                        Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            color: "#b98e67"
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                            text: "ENTSO-E DAY-AHEAD-PRIS VS. MODELLERT MARGINALKOSTNAD · EUR/MWh"
                            color: "#8da99c"
                            font.pixelSize: 10
                            font.bold: true
                            font.family: "Inter"
                        }
                    }

                    // Sammenligningstabell Observert vs Modellert
                    GridLayout {
                        columns: 6
                        columnSpacing: 14
                        rowSpacing: 8

                        Text { text: "OMRÅDE"; color: "#8da99c"; font.pixelSize: 9; font.bold: true; font.family: "Inter" }
                        Text { text: "NO1 (ØST)"; color: "#ffffff"; font.pixelSize: 10; font.bold: true }
                        Text { text: "NO2 (SØR)"; color: "#ffffff"; font.pixelSize: 10; font.bold: true }
                        Text { text: "NO3 (MIDT)"; color: "#ffffff"; font.pixelSize: 10; font.bold: true }
                        Text { text: "NO4 (NORD)"; color: "#ffffff"; font.pixelSize: 10; font.bold: true }
                        Text { text: "NO5 (VEST)"; color: "#ffffff"; font.pixelSize: 10; font.bold: true }

                        Text { text: "OBSERVERTE (ENTSO-E)"; color: "#8da99c"; font.pixelSize: 9; font.bold: true; font.family: "Inter" }
                        Text { text: backend.metadata.hasObservedPriceNO1 ? backend.metadata.obsPriceNO1.toFixed(1) + " €" : "—"; color: "#f8f7f3"; font.pixelSize: 13; font.bold: true }
                        Text { text: backend.metadata.hasObservedPriceNO2 ? backend.metadata.obsPriceNO2.toFixed(1) + " €" : "—"; color: "#f8f7f3"; font.pixelSize: 13; font.bold: true }
                        Text { text: backend.metadata.hasObservedPriceNO3 ? backend.metadata.obsPriceNO3.toFixed(1) + " €" : "—"; color: "#f8f7f3"; font.pixelSize: 13; font.bold: true }
                        Text { text: backend.metadata.hasObservedPriceNO4 ? backend.metadata.obsPriceNO4.toFixed(1) + " €" : "—"; color: "#f8f7f3"; font.pixelSize: 13; font.bold: true }
                        Text { text: backend.metadata.hasObservedPriceNO5 ? backend.metadata.obsPriceNO5.toFixed(1) + " €" : "—"; color: "#f8f7f3"; font.pixelSize: 13; font.bold: true }

                        Text { text: "MODELLERT MARGINALKOSTNAD (CVXPY)"; color: "#8da99c"; font.pixelSize: 9; font.bold: true; font.family: "Inter" }
                        Text { text: backend.metadata.hasModelledPrices ? backend.metadata.modPriceNO1.toFixed(1) + " €" : "—"; color: "#8da99c"; font.pixelSize: 13; font.bold: true }
                        Text { text: backend.metadata.hasModelledPrices ? backend.metadata.modPriceNO2.toFixed(1) + " €" : "—"; color: "#8da99c"; font.pixelSize: 13; font.bold: true }
                        Text { text: backend.metadata.hasModelledPrices ? backend.metadata.modPriceNO3.toFixed(1) + " €" : "—"; color: "#8da99c"; font.pixelSize: 13; font.bold: true }
                        Text { text: backend.metadata.hasModelledPrices ? backend.metadata.modPriceNO4.toFixed(1) + " €" : "—"; color: "#8da99c"; font.pixelSize: 13; font.bold: true }
                        Text { text: backend.metadata.hasModelledPrices ? backend.metadata.modPriceNO5.toFixed(1) + " €" : "—"; color: "#8da99c"; font.pixelSize: 13; font.bold: true }

                        Text { text: "OBS − MODEL"; color: "#8da99c"; font.pixelSize: 9; font.bold: true; font.family: "Inter" }
                        Text { text: backend.metadata.hasObservedPriceNO1 && backend.metadata.hasModelledPrices ? (backend.metadata.obsPriceNO1 - backend.metadata.modPriceNO1).toFixed(1) + " €" : "—"; color: "#b98e67"; font.pixelSize: 11; font.bold: true }
                        Text { text: backend.metadata.hasObservedPriceNO2 && backend.metadata.hasModelledPrices ? (backend.metadata.obsPriceNO2 - backend.metadata.modPriceNO2).toFixed(1) + " €" : "—"; color: "#e88f86"; font.pixelSize: 11; font.bold: true }
                        Text { text: backend.metadata.hasObservedPriceNO3 && backend.metadata.hasModelledPrices ? (backend.metadata.obsPriceNO3 - backend.metadata.modPriceNO3).toFixed(1) + " €" : "—"; color: "#8da99c"; font.pixelSize: 11; font.bold: true }
                        Text { text: backend.metadata.hasObservedPriceNO4 && backend.metadata.hasModelledPrices ? (backend.metadata.obsPriceNO4 - backend.metadata.modPriceNO4).toFixed(1) + " €" : "—"; color: "#8da99c"; font.pixelSize: 11; font.bold: true }
                        Text { text: backend.metadata.hasObservedPriceNO5 && backend.metadata.hasModelledPrices ? (backend.metadata.obsPriceNO5 - backend.metadata.modPriceNO5).toFixed(1) + " €" : "—"; color: "#8da99c"; font.pixelSize: 11; font.bold: true }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        height: 1
                        color: "#2a3630"
                    }

                    // Forklarende tekst
                    Text {
                        Layout.fillWidth: true
                        text: backend.metadata.modelBasisExplanation
                        color: "#d0dbd5"
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                        lineHeight: 1.4
                        font.family: "Manrope"
                    }

                    Text {
                        Layout.fillWidth: true
                        text: backend.metadata.hasObservedPrices && backend.metadata.hasModelledPrices
                            ? "Observert ENTSO-E-pris og modellert marginalkostnad. "
                            : "Faktisk prisserie eller modellresultat mangler"
                        color: "#8da99c"
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        lineHeight: 1.5
                        font.family: "Manrope"
                    }

                    Item { Layout.fillHeight: true }
                }
            }
        }
    }

    // NEDRE DEL: Passende Døgnprofilgraf (NO1-NO5)
    Item {
        id: chartAndStatusArea
        anchors.bottom: bottomBar.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 220
        anchors.margins: 18

        Rectangle {
            anchors.fill: parent
            radius: 12
            color: "#f2161f1c"
            border.color: "#2a3630"

            RowLayout {
                anchors.fill: parent
                anchors.margins: 14
                spacing: 16

                // Venstre del: Døgnkurve med Y-akse og sonelinjer
                Item {
                    Layout.fillHeight: true
                    Layout.fillWidth: true

                    RowLayout {
                        id: chartHeader
                        anchors.top: parent.top
                        anchors.left: parent.left
                        anchors.right: parent.right
                        height: 24

                        Text {
                            text: backend.metadata.modelledDeliveryDayLocal.length > 0
                                ? "DAY-AHEAD PRICE · " + backend.metadata.modelledDeliveryDayLocal + " · " + backend.metadata.dayAheadMode
                                : "DAY-AHEAD PRICE · ENTSO-E A44"
                            color: "#8da99c"
                            font.pixelSize: 10
                            font.bold: true
                            font.family: "Inter"
                        }
                        Item { Layout.fillWidth: true }
                        // Sonefilterknapper
                        Row {
                            spacing: 4
                            component ZoneBtn: Rectangle {
                                property string zId: ""
                                width: 38
                                height: 20
                                radius: 4
                                color: selectedZone === zId ? "#304d42" : "#1f2724"
                                border.color: selectedZone === zId ? "#526d60" : "#36443e"
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: { root.selectChartZone(zId); }
                                }
                                Text {
                                    anchors.centerIn: parent
                                    text: zId
                                    color: selectedZone === zId ? "#ffffff" : "#8da99c"
                                    font.pixelSize: 8
                                    font.bold: true
                                    font.family: "Inter"
                                }
                            }
                            ZoneBtn { zId: "ALL" }
                            ZoneBtn { zId: "NO1" }
                            ZoneBtn { zId: "NO2" }
                            ZoneBtn { zId: "NO3" }
                            ZoneBtn { zId: "NO4" }
                            ZoneBtn { zId: "NO5" }
                        }
                    }

                    Canvas {
                        id: priceCanvas
                        anchors.top: chartHeader.bottom
                        anchors.bottom: parent.bottom
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.topMargin: 6

                        onPaint: {
                            const ctx = getContext("2d");
                            ctx.clearRect(0, 0, width, height);

                            // Keep these margins in sync with the MouseArea below.
                            const left = 48;
                            const right = 18;
                            const top = 28;
                            const bottom = 25;
                            const plotWidth = Math.max(1, width - left - right);
                            const plotHeight = Math.max(1, height - top - bottom);

                            const startMs = root.chartStartMs();
                            const endMs = root.chartEndMs();
                            const durationMs = endMs - startMs;

                            if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || durationMs <= 0) {
                                ctx.fillStyle = "#8da99c";
                                ctx.font = "11px Manrope, sans-serif";
                                ctx.textAlign = "center";
                                ctx.textBaseline = "middle";
                                ctx.fillText("Waiting for a valid ENTSO-E delivery window…", width / 2, height / 2);
                                return;
                            }

                            const areaIds = ["NO1", "NO2", "NO3", "NO4", "NO5"];
                            const colors = {
                                NO1: "#526d60",
                                NO2: "#b98e67",
                                NO3: "#4f7a6f",
                                NO4: "#6a7b82",
                                NO5: "#a67c52"
                            };

                            const observedByArea = {
                                NO1: [], NO2: [], NO3: [], NO4: [], NO5: []
                            };

                            // Distribute and validate the observed A44 observations.
                            const series = backend.metadata.observedPriceSeries || [];
                            for (let i = 0; i < series.length; i++) {
                                const point = series[i];
                                const areaId = String(point.area);
                                const timestamp = Number(point.timestamp_ms);
                                const price = Number(point.price_eur_mwh);

                                if (observedByArea[areaId]
                                    && Number.isFinite(timestamp)
                                    && Number.isFinite(price)) {
                                    observedByArea[areaId].push({
                                        area: areaId,
                                        timestamp_ms: timestamp,
                                        price_eur_mwh: price
                                    });
                                }
                            }
                            for (const areaId of areaIds) {
                                const raw = observedByArea[areaId];
                                raw.sort(function(a, b) {
                                    return a.timestamp_ms - b.timestamp_ms;
                                });

                                const unique = [];
                                for (let i = 0; i < raw.length; i++) {
                                    const point = raw[i];
                                    if (unique.length > 0
                                        && unique[unique.length - 1].timestamp_ms === point.timestamp_ms) {
                                        unique[unique.length - 1] = point;
                                    } else {
                                        unique.push(point);
                                    }
                                }
                                observedByArea[areaId] = unique;
                            }
                            function nominalIntervalMs(points) {
                                if (!points || points.length < 2)
                                    return 15 * 60 * 1000;

                                const intervals = [];
                                for (let i = 1; i < points.length; i++) {
                                    const dt = points[i].timestamp_ms - points[i - 1].timestamp_ms;
                                    if (dt > 0 && dt <= 6 * 60 * 60 * 1000)
                                        intervals.push(dt);
                                }

                                if (intervals.length === 0)
                                    return 15 * 60 * 1000;

                                intervals.sort(function(a, b) { return a - b; });
                                return intervals[Math.floor(intervals.length / 2)];
                            }

                            function pricesByArea(records) {
                                const result = { NO1: [], NO2: [], NO3: [], NO4: [], NO5: [] };
                                for (let i = 0; i < records.length; i++) {
                                    const point = records[i];
                                    const areaId = String(point.area);
                                    const timestamp = Number(point.timestamp_ms);
                                    const price = Number(point.price_eur_mwh);
                                    if (result[areaId] && Number.isFinite(timestamp) && Number.isFinite(price))
                                        result[areaId].push({ timestamp_ms: timestamp, price_eur_mwh: price });
                                }
                                for (const areaId of areaIds)
                                    result[areaId].sort(function(a, b) { return a.timestamp_ms - b.timestamp_ms; });
                                return result;
                            }
                            const modeledByAreaA = pricesByArea(backend.metadata.modelledPriceSeriesA || []);
                            const modeledByAreaB = pricesByArea(backend.metadata.modelledPriceSeriesB || []);

                            const visibleAreas = selectedZone === "ALL" ? areaIds : [selectedZone];
                            let minPrice = Infinity;
                            let maxPrice = -Infinity;

                            for (const areaId of visibleAreas) {
                                const points = observedByArea[areaId] || [];
                                for (let i = 0; i < points.length; i++) {
                                    minPrice = Math.min(minPrice, points[i].price_eur_mwh);
                                    maxPrice = Math.max(maxPrice, points[i].price_eur_mwh);
                                }

                                for (const point of (modeledByAreaA[areaId] || []).concat(modeledByAreaB[areaId] || [])) {
                                    if (point.timestamp_ms >= startMs && point.timestamp_ms < endMs) {
                                        minPrice = Math.min(minPrice, point.price_eur_mwh);
                                        maxPrice = Math.max(maxPrice, point.price_eur_mwh);
                                    }
                                }
                            }

                            if (!Number.isFinite(minPrice) || !Number.isFinite(maxPrice)) {
                                minPrice = 0;
                                maxPrice = 100;
                            }

                            if (maxPrice - minPrice < 1) {
                                const center = (maxPrice + minPrice) / 2;
                                minPrice = center - 10;
                                maxPrice = center + 10;
                            }

                            const pricePadding = Math.max(5, (maxPrice - minPrice) * 0.08);
                            minPrice -= pricePadding;
                            maxPrice += pricePadding;
                            const roughStep = (maxPrice - minPrice) / 5;
                            const magnitude = Math.pow(10, Math.floor(Math.log10(Math.max(1, roughStep))));
                            const normalizedStep = roughStep / magnitude;
                            const niceStep = normalizedStep <= 1 ? 1
                                : normalizedStep <= 2 ? 2
                                : normalizedStep <= 5 ? 5 : 10;
                            const tickStep = niceStep * magnitude;
                            minPrice = Math.floor(minPrice / tickStep) * tickStep;
                            maxPrice = Math.ceil(maxPrice / tickStep) * tickStep;

                            function xForMs(ms) {
                                return left + ((ms - startMs) / durationMs) * plotWidth;
                            }

                            function yFor(price) {
                                return top + plotHeight - ((price - minPrice) / (maxPrice - minPrice)) * plotHeight;
                            }

                            ctx.font = "8px Inter, sans-serif";
                            ctx.textAlign = "right";
                            ctx.textBaseline = "middle";
                            ctx.setLineDash([2, 4]);
                            for (let price = minPrice; price <= maxPrice + tickStep * 0.001; price += tickStep) {
                                const y = yFor(price);
                                ctx.strokeStyle = "#1e2924";
                                ctx.lineWidth = 1;
                                ctx.beginPath();
                                ctx.moveTo(left, y);
                                ctx.lineTo(left + plotWidth, y);
                                ctx.stroke();
                                ctx.fillStyle = "#829087";
                                ctx.fillText(price.toFixed(0), left - 7, y);
                            }
                            ctx.setLineDash([]);

                            ctx.font = "8px Inter, sans-serif";
                            ctx.textBaseline = "middle";
                            ctx.textAlign = "left";
                            ctx.fillStyle = "#829087";
                            ctx.fillText("EUR/MWh", left, 11);

                            ctx.strokeStyle = "#8daa98";
                            ctx.lineWidth = 2;
                            ctx.beginPath();
                            ctx.moveTo(left + 65, 11);
                            ctx.lineTo(left + 81, 11);
                            ctx.stroke();
                            ctx.fillStyle = "#8daa98";
                            ctx.fillText("ENTSO-E A44", left + 86, 11);

                            // Model profile styles in the legend.
                            const legendX = left + 180;
                            ctx.strokeStyle = "#d4a373";
                            ctx.lineWidth = 1.8;
                            ctx.setLineDash([6, 3]);
                            ctx.beginPath();
                            ctx.moveTo(legendX - 7, 11);
                            ctx.lineTo(legendX + 7, 11);
                            ctx.stroke();
                            ctx.setLineDash([]);
                            ctx.fillStyle = "#d4a373";
                            ctx.fillText("B profile", legendX + 10, 11);
                            ctx.strokeStyle = "#a9b5b0";
                            ctx.setLineDash([2, 4]);
                            ctx.beginPath();
                            ctx.moveTo(legendX + 70, 11);
                            ctx.lineTo(legendX + 84, 11);
                            ctx.stroke();
                            ctx.setLineDash([]);
                            ctx.fillStyle = "#a9b5b0";
                            ctx.fillText("A fallback", legendX + 89, 11);
                            ctx.textAlign = "center";
                            ctx.textBaseline = "alphabetic";
                            ctx.font = "8px Inter, sans-serif";
                            for (let i = 0; i <= 6; i++) {
                                const timestamp = startMs + durationMs * (i / 6);
                                const x = xForMs(timestamp);
                                ctx.strokeStyle = "#1b2420";
                                ctx.lineWidth = 1;
                                ctx.beginPath();
                                ctx.moveTo(x, top);
                                ctx.lineTo(x, top + plotHeight);
                                ctx.stroke();
                                ctx.fillStyle = "#829087";
                                ctx.fillText(root.formatChartTime(timestamp), x, top + plotHeight + 15);
                            }

                            for (const areaId of visibleAreas) {
                                const points = observedByArea[areaId] || [];
                                if (points.length === 0)
                                    continue;

                                const intervalMs = nominalIntervalMs(points);
                                let previousEndMs = null;
                                let previousY = null;

                                ctx.strokeStyle = colors[areaId];
                                ctx.lineWidth = 2.1;
                                ctx.lineJoin = "miter";
                                ctx.lineCap = "butt";
                                ctx.setLineDash([]);
                                ctx.beginPath();

                                for (let i = 0; i < points.length; i++) {
                                    const point = points[i];
                                    const pointMs = point.timestamp_ms;
                                    const nextPointMs = i + 1 < points.length
                                        ? points[i + 1].timestamp_ms
                                        : pointMs + intervalMs;

                                    const segmentStartMs = Math.max(startMs, pointMs);
                                    const segmentEndMs = Math.min(
                                        endMs,
                                        pointMs + intervalMs,
                                        nextPointMs
                                    );

                                    if (segmentEndMs <= segmentStartMs) {
                                        previousEndMs = null;
                                        previousY = null;
                                        continue;
                                    }

                                    const x0 = xForMs(segmentStartMs);
                                    const x1 = xForMs(segmentEndMs);
                                    const y = yFor(point.price_eur_mwh);

                                    if (previousEndMs !== null
                                        && Math.abs(segmentStartMs - previousEndMs) < 1) {
                                        ctx.lineTo(x0, previousY);
                                        ctx.lineTo(x0, y);
                                    } else { ctx.moveTo(x0, y); }

                                    ctx.lineTo(x1, y);
                                    previousEndMs = segmentEndMs;
                                    previousY = y;

                                    if (i + 1 < points.length
                                        && points[i + 1].timestamp_ms - pointMs > intervalMs * 1.05) {
                                        previousEndMs = null;
                                        previousY = null;
                                    }
                                }
                                ctx.stroke();
                            }
                            function drawModeledSeries(points, dashPattern, strokeColor) {
                                ctx.strokeStyle = strokeColor;
                                ctx.lineWidth = 1.8;
                                ctx.lineJoin = "round";
                                ctx.lineCap = "round";
                                ctx.setLineDash(dashPattern);
                                ctx.beginPath();
                                let started = false;
                                let previousTimestamp = null;
                                for (const point of points || []) {
                                    const timestamp = point.timestamp_ms;
                                    if (timestamp < startMs || timestamp >= endMs)
                                        continue;
                                    // A missing MTU breaks the path; do not draw through gaps.
                                    if (previousTimestamp !== null
                                        && timestamp - previousTimestamp > 15 * 60 * 1000 * 1.05)
                                        started = false;
                                    const x = xForMs(timestamp);
                                    const y = yFor(point.price_eur_mwh);
                                    if (!started) {
                                        ctx.moveTo(x, y);
                                        started = true;
                                    } else {
                                        ctx.lineTo(x, y);
                                    }
                                    previousTimestamp = timestamp;
                                }
                                ctx.stroke();
                                ctx.setLineDash([]);
                            }
                            for (const areaId of visibleAreas) {
                                drawModeledSeries(modeledByAreaA[areaId], [2, 4], colors[areaId]);
                                drawModeledSeries(modeledByAreaB[areaId], [6, 3], colors[areaId]);
                            }

                            if (root.hoverTimestampMs >= startMs && root.hoverTimestampMs < endMs) {
                                const hoverX = xForMs(root.hoverTimestampMs);
                                const hoverRows = [];

                                ctx.strokeStyle = "#d4a373";
                                ctx.lineWidth = 1.2;
                                ctx.setLineDash([4, 3]);
                                ctx.beginPath();
                                ctx.moveTo(hoverX, top);
                                ctx.lineTo(hoverX, top + plotHeight);
                                ctx.stroke();
                                ctx.setLineDash([]);

                                for (const areaId of visibleAreas) {
                                    const point = root.pointAtTime(
                                        observedByArea[areaId],
                                        root.hoverTimestampMs
                                    );
                                    const observedPrice = point ? Number(point.price_eur_mwh) : NaN;
                                    const hasObserved = Number.isFinite(observedPrice);
                                    const modelPointB = root.pointAtTime(
                                        modeledByAreaB[areaId] || [], root.hoverTimestampMs
                                    );
                                    const modelPointA = root.pointAtTime(
                                        modeledByAreaA[areaId] || [], root.hoverTimestampMs
                                    );
                                    const modelPoint = modelPointB || modelPointA;
                                    const modelPrice = modelPoint ? Number(modelPoint.price_eur_mwh) : NaN;
                                    const hasModel = Number.isFinite(modelPrice);

                                    if (hasObserved) {
                                        const y = yFor(observedPrice);
                                        ctx.fillStyle = colors[areaId];
                                        ctx.strokeStyle = "#f8f7f3";
                                        ctx.lineWidth = 1.3;
                                        ctx.beginPath();
                                        ctx.arc(hoverX, y, 4, 0, Math.PI * 2);
                                        ctx.fill();
                                        ctx.stroke();
                                    }

                                    hoverRows.push({
                                        area: areaId,
                                        observed: observedPrice,
                                        hasObserved: hasObserved,
                                        modelled: modelPrice,
                                        hasModelled: hasModel
                                    });
                                }

                                const boxWidth = 204;
                                const rowHeight = 14;
                                const boxHeight = 42 + hoverRows.length * rowHeight;
                                let boxX = hoverX + 10;
                                if (boxX + boxWidth > left + plotWidth)
                                    boxX = hoverX - boxWidth - 10;
                                boxX = Math.max(left, Math.min(boxX, left + plotWidth - boxWidth));
                                const boxY = top + 3;

                                ctx.fillStyle = "#1e2622";
                                ctx.fillRect(boxX, boxY, boxWidth, boxHeight);
                                ctx.strokeStyle = "#d4a373";
                                ctx.lineWidth = 1;
                                ctx.strokeRect(boxX, boxY, boxWidth, boxHeight);

                                ctx.font = "bold 10px Inter, sans-serif";
                                ctx.textAlign = "left";
                                ctx.textBaseline = "middle";
                                ctx.fillStyle = "#d4a373";
                                ctx.fillText(root.formatChartTime(root.hoverTimestampMs), boxX + 8, boxY + 11);

                                ctx.font = "8px Inter, sans-serif";
                                ctx.fillStyle = "#829087";
                                ctx.textAlign = "left";
                                ctx.fillText("AREA", boxX + 8, boxY + 30);
                                ctx.textAlign = "right";
                                ctx.fillText("A44", boxX + 112, boxY + 30);
                                ctx.fillText("B / A", boxX + boxWidth - 8, boxY + 30);

                                ctx.strokeStyle = "#384941";
                                ctx.beginPath();
                                ctx.moveTo(boxX + 7, boxY + 36);
                                ctx.lineTo(boxX + boxWidth - 7, boxY + 36);
                                ctx.stroke();

                                hoverRows.forEach(function(row, index) {
                                    const y = boxY + 45 + index * rowHeight;
                                    ctx.font = "bold 9px Inter, sans-serif";
                                    ctx.textAlign = "left";
                                    ctx.fillStyle = colors[row.area];
                                    ctx.fillText(row.area, boxX + 8, y);

                                    ctx.textAlign = "right";
                                    ctx.fillStyle = row.hasObserved ? "#f8f7f3" : "#667870";
                                    ctx.fillText(
                                        row.hasObserved ? row.observed.toFixed(1) : "—",
                                        boxX + 112,
                                        y
                                    );
                                    ctx.fillStyle = row.hasModelled ? "#d4a373" : "#667870";
                                    ctx.fillText(
                                        row.hasModelled ? row.modelled.toFixed(1) : "—",
                                        boxX + boxWidth - 8,
                                        y
                                    );
                                });
                            }
                        }

                        MouseArea {
                            anchors.fill: parent
                            hoverEnabled: true
                            acceptedButtons: Qt.NoButton

                            onPositionChanged: function(mouse) {
                                const left = 48;
                                const right = 18;
                                const top = 28;
                                const bottom = 25;
                                const plotWidth = Math.max(1, width - left - right);

                                const startMs = root.chartStartMs();
                                const endMs = root.chartEndMs();
                                const durationMs = endMs - startMs;

                                if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || durationMs <= 0
                                    || mouse.x < left || mouse.x > left + plotWidth
                                    || mouse.y < top || mouse.y > height - bottom) {
                                    root.hoverTimestampMs = -1;
                                    priceCanvas.requestPaint();
                                    return;
                                }

                                const ratio = (mouse.x - left) / plotWidth;
                                root.hoverTimestampMs = startMs + ratio * durationMs;
                                priceCanvas.requestPaint();
                            }

                            onExited: {
                                root.hoverTimestampMs = -1;
                                priceCanvas.requestPaint();
                            }
                        }

                        Connections {
                            target: backend.metadata
                            function onChanged() {
                                priceCanvas.requestPaint();
                            }
                        }
                    }
                }
            }
        }
    }

    // BOTTOM BAR: NO1 - NO5 Price & Flow Bar med Systemstatus
    Rectangle {
        id: bottomBar
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: 118
        color: "#f2161f1c"
        border.color: "#28342e"
        z: 10

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 22
            anchors.rightMargin: 22
            spacing: 14

            component PricePill: Rectangle {
                property string area: ""
                property real price: 0.0
                property bool available: false
                property real avgPrice: root.displayedAverage()
                width: 130
                height: 70
                radius: 8
                color: !available ? "#202a25" : (price > avgPrice * 1.12) ? "#3f201d" : (price < avgPrice * 0.88) ? "#1b3327" : "#202a25"
                border.color: !available ? "#384941" : (price > avgPrice * 1.12) ? "#a55850" : (price < avgPrice * 0.88) ? "#526d60" : "#384941"

                Column {
                    anchors.centerIn: parent
                    spacing: 2
                    Text {
                        text: area
                        color: "#8da99c"
                        font.pixelSize: 9
                        font.bold: true
                        font.family: "Inter"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Text {
                        text: available ? price.toFixed(1) + " €" : "—"
                        color: "#ffffff"
                        font.pixelSize: 18
                        font.bold: true
                        font.family: "Plus Jakarta Sans"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Text {
                        text: !available || avgPrice === 0 ? "— vs snitt" : ((price - avgPrice) / avgPrice * 100 > 0 ? "+" : "") + ((price - avgPrice) / avgPrice * 100).toFixed(0) + "% vs snitt"
                        color: (price > avgPrice * 1.12) ? "#e88f86" : (price < avgPrice * 0.88) ? "#8da99c" : "#8da99c"
                        font.pixelSize: 8
                        font.family: "Inter"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }
            }

            PricePill { area: "NO1 (Øst)"; price: root.displayedPrice("NO1"); available: root.hasDisplayedPriceFor("NO1") }
            PricePill { area: "NO2 (Sør)"; price: root.displayedPrice("NO2"); available: root.hasDisplayedPriceFor("NO2") }
            PricePill { area: "NO3 (Midt)"; price: root.displayedPrice("NO3"); available: root.hasDisplayedPriceFor("NO3") }
            PricePill { area: "NO4 (Nord)"; price: root.displayedPrice("NO4"); available: root.hasDisplayedPriceFor("NO4") }
            PricePill { area: "NO5 (Vest)"; price: root.displayedPrice("NO5"); available: root.hasDisplayedPriceFor("NO5") }

            Rectangle {
                width: 1
                height: 52
                color: "#2a3630"
            }

            // SYSTEMSTATUS & AKTIVE BINDENDE GRENSER
            Column {
                Layout.fillWidth: true
                spacing: 3
                Text {
                    text: "SYSTEMSTATUS & JAO CNEC-GRENSER (IKKE ENKELTLINJEFLOW):"
                    color: "#8da99c"
                    font.pixelSize: 9
                    font.bold: true
                    font.family: "Inter"
                }
                Text {
                    text: backend.metadata.bindingConstraint
                    color: backend.metadata.bindingConstraint.indexOf("Normal") !== -1 ? "#8da99c" : "#e88f86"
                    font.pixelSize: 12
                    font.bold: true
                    font.family: "Manrope"
                    elide: Text.ElideNone
                    wrapMode: Text.WordWrap
                    maximumLineCount: 3
                    width: parent.width
                }
            }

            // NETTO EKSPORT
            Rectangle {
                width: 140
                height: 70
                radius: 8
                color: "#202a25"
                border.color: "#384941"

                Column {
                    anchors.centerIn: parent
                    spacing: 2
                    Text {
                        text: "STATNETT FYSISKE FLYTER"
                        color: "#8da99c"
                        font.pixelSize: 9
                        font.bold: true
                        font.family: "Inter"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Text {
                        text: backend.metadata.hasPhysicalFlows ? backend.metadata.physicalFlowCount + " observasjoner" : "—"
                        color: "#b98e67"
                        font.pixelSize: 16
                        font.bold: true
                        font.family: "Plus Jakarta Sans"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Text {
                        text: "XML GetFlow-feed"
                        color: "#8da99c"
                        font.pixelSize: 8
                        font.family: "Inter"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }
            }
        }
    } 
}
