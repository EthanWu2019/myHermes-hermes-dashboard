"use client";

import { useEffect, useState } from "react";
import { Line } from "react-chartjs-2";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler);

type Service = {
  label: string;
  host: string;
  url: string;
  local: string;
  critical: boolean;
  icon: string;
  note: string;
  status: "up" | "down" | "unknown";
  http_code: number | null;
  latency_ms: number | null;
  last_checked: string | null;
  error: string | null;
};

type Snapshot = {
  hermes: {
    version: string;
    commits_behind: number | null;
    install_method: string | null;
    started_at: string | null;
    uptime_seconds: number | null;
    configured_model: string | null;
    configured_provider: string | null;
    last_call_model: string | null;
    last_call_provider: string | null;
    summary: {
      total_sessions?: number;
      total_messages?: number;
      total_tool_calls?: number;
      input_tokens?: number;
      output_tokens?: number;
      cache_read_tokens?: number;
      cache_write_tokens?: number;
      reasoning_tokens?: number;
      total_tokens?: number;
      estimated_cost_usd?: number;
    };
    mimo_used: number | null;
    mimo_total: number | null;
    timestamp: string | null;
  };
  services: Service[];
  last_full_refresh: string | null;
};

type Rollup = {
  date_local: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  total_sessions: number;
  total_messages: number;
  total_tool_calls: number;
  by_model: Record<string, any>;
  rollup_complete?: number;
};

const fmtInt = (n: number | null | undefined) => {
  if (n === null || n === undefined) return "—";
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
};

const fmtUptime = (sec: number | null) => {
  if (sec === null) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
};

const fmtMimoPct = (used: number | null, total: number | null) => {
  if (used === null || total === null) return "—";
  return ((used / total) * 100).toFixed(3) + "%";
};

export default function Dashboard() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [rollups, setRollups] = useState<Rollup[]>([]);
  const [todayLocal, setTodayLocal] = useState<string>("");
  const [now, setNow] = useState<Date>(new Date());
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";

  useEffect(() => {
    const fetchAll = async () => {
      try {
        const [s, r] = await Promise.all([
          fetch(`${apiUrl}/api/snapshot`).then((r) => r.json()),
          fetch(`${apiUrl}/api/rollups`).then((r) => r.json()),
        ]);
        setSnap(s);
        setRollups(r.rollups || []);
        setTodayLocal(r.today_local || "");
      } catch (e) {
        console.error(e);
      }
    };
    fetchAll();
    const iv1 = setInterval(fetchAll, 30000);
    const iv2 = setInterval(() => setNow(new Date()), 1000);
    return () => {
      clearInterval(iv1);
      clearInterval(iv2);
    };
  }, [apiUrl]);

  if (!snap) {
    return (
      <div className="container">
        <div className="header">
          <h1>Hermes Dashboard</h1>
          <span className="subtitle">loading…</span>
        </div>
      </div>
    );
  }

  const h = snap.hermes;
  const upCount = snap.services.filter((s) => s.status === "up").length;
  const downCount = snap.services.filter((s) => s.status === "down").length;
  const criticalDown = snap.services.filter((s) => s.critical && s.status === "down");

  // Today's row (last in array, marked live) — fallback if today missing
  const todayRow = rollups.find((r) => r.date_local === todayLocal) || rollups[rollups.length - 1];
  const todayIn = todayRow?.input_tokens ?? 0;
  const todayOut = todayRow?.output_tokens ?? 0;
  const todaySessions = todayRow?.total_sessions ?? 0;

  const chartData = {
    labels: rollups.map((r) => r.date_local),
    datasets: [
      {
        label: "Input tokens",
        data: rollups.map((r) => r.input_tokens),
        borderColor: "#facc15",
        backgroundColor: "rgba(250, 204, 21, 0.08)",
        fill: true,
        tension: 0.25,
        pointRadius: 2,
        pointBackgroundColor: "#facc15",
      },
      {
        label: "Output tokens",
        data: rollups.map((r) => r.output_tokens),
        borderColor: "#22c55e",
        backgroundColor: "transparent",
        tension: 0.25,
        pointRadius: 2,
        pointBackgroundColor: "#22c55e",
      },
      {
        label: "Cache read",
        data: rollups.map((r) => r.cache_read_tokens),
        borderColor: "#60a5fa",
        backgroundColor: "transparent",
        tension: 0.25,
        pointRadius: 1,
        pointBackgroundColor: "#60a5fa",
        borderDash: [4, 4],
      },
    ],
  };

  const chartOpts: any = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: "#a3a3a3", font: { size: 11 } } },
      tooltip: { mode: "index", intersect: false },
    },
    scales: {
      x: {
        ticks: { color: "#525252", font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
        grid: { color: "#1c1c1c" },
      },
      y: {
        ticks: {
          color: "#525252",
          font: { size: 11 },
          callback: (v: any) => fmtInt(Number(v)),
        },
        grid: { color: "#1c1c1c" },
      },
    },
  };

  return (
    <div className="container">
      <div className="header">
        <h1>Hermes Dashboard</h1>
        <span className="subtitle">
          {now.toLocaleString("en-US", { hour12: false })} · refresh 30s
        </span>
      </div>

      {/* Row 1: top-line KPIs */}
      <div className="grid">
        <div className="card span-3">
          <div className="card-title">Hermes Version</div>
          <div className="card-big">v{h.version}</div>
          <div className="card-sub">
            {h.commits_behind !== null && h.commits_behind > 0 ? `${h.commits_behind} commits behind` : "up to date"}
          </div>
        </div>
        <div className="card span-3">
          <div className="card-title">Uptime</div>
          <div className="uptime-display">{fmtUptime(h.uptime_seconds)}</div>
          <div className="card-sub">dashboard backend</div>
        </div>
        <div className="card span-3">
          <div className="card-title">Services</div>
          <div className="card-big">
            <span style={{ color: upCount === snap.services.length ? "var(--up)" : "var(--warn)" }}>
              {upCount}
            </span>
            <span style={{ color: "var(--text-muted)" }}>/{snap.services.length}</span>
          </div>
          <div className="card-sub">
            {downCount === 0
              ? "all green"
              : `${downCount} down${criticalDown.length ? ` · ${criticalDown.length} critical` : ""}`}
          </div>
        </div>
        <div className="card span-3">
          <div className="card-title">Default Model</div>
          <div className="card-big" style={{ fontSize: 22 }}>{h.configured_model || "—"}</div>
          <div className="card-sub">{h.configured_provider || ""}</div>
        </div>
      </div>

      {/* Row 2: today + trend */}
      <div className="grid" style={{ marginTop: 16 }}>
        <div className="card span-4">
          <div className="card-title">Today ({todayLocal})</div>
          <div className="kv"><span className="kv-key">Input</span><span className="kv-val">{fmtInt(todayIn)}</span></div>
          <div className="kv"><span className="kv-key">Output</span><span className="kv-val">{fmtInt(todayOut)}</span></div>
          <div className="kv"><span className="kv-key">in + out</span><span className="kv-val">{fmtInt(todayIn + todayOut)}</span></div>
          <div className="kv"><span className="kv-key">Sessions</span><span className="kv-val">{todaySessions}</span></div>
          <div className="kv"><span className="kv-key">Last-call model</span><span className="kv-val" style={{ fontSize: 11 }}>{h.last_call_model || "—"}</span></div>
        </div>
        <div className="card span-8">
          <div className="card-title">Daily Token Usage · {rollups.length} days · source: token_tracker.py</div>
          <div style={{ height: 280, marginTop: 8 }}>
            {rollups.length > 0 ? (
              <Line data={chartData} options={chartOpts} />
            ) : (
              <div style={{ color: "var(--text-muted)", padding: 40, textAlign: "center" }}>
                no data
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Row 3: services + cumulative + MiMo */}
      <div className="grid" style={{ marginTop: 16 }}>
        <div className="card span-4">
          <div className="card-title">Services Health</div>
          {snap.services.map((s) => (
            <div key={s.host} className="service-row">
              <span className={"dot " + s.status} />
              <span className={"service-name " + (s.critical ? "critical" : "")}>{s.label}</span>
              <span className="service-host">{s.host}</span>
              <span className="service-latency">
                {s.latency_ms !== null ? `${s.latency_ms}ms` : "—"}
              </span>
              <span className="service-status" style={{ color: s.status === "up" ? "var(--up)" : "var(--down)" }}>
                {s.status === "up" ? (s.http_code || "OK") : "DOWN"}
              </span>
            </div>
          ))}
        </div>
        <div className="card span-4">
          <div className="card-title">Lifetime Stats</div>
          <div className="kv"><span className="kv-key">Sessions</span><span className="kv-val">{fmtInt(h.summary.total_sessions)}</span></div>
          <div className="kv"><span className="kv-key">Messages</span><span className="kv-val">{fmtInt(h.summary.total_messages)}</span></div>
          <div className="kv"><span className="kv-key">Tool calls</span><span className="kv-val">{fmtInt(h.summary.total_tool_calls)}</span></div>
          <div className="kv"><span className="kv-key">Total tokens</span><span className="kv-val">{fmtInt(h.summary.total_tokens)}</span></div>
          <div className="kv"><span className="kv-key">Install method</span><span className="kv-val">{h.install_method || "—"}</span></div>
        </div>
        <div className="card span-4">
          <div className="card-title">MiMo Quota</div>
          <div className="kv"><span className="kv-key">Used</span><span className="kv-val">{fmtInt(h.mimo_used)}</span></div>
          <div className="kv"><span className="kv-key">Total</span><span className="kv-val">{fmtInt(h.mimo_total)}</span></div>
          <div className="kv"><span className="kv-key">% used</span><span className="kv-val">{fmtMimoPct(h.mimo_used, h.mimo_total)}</span></div>
          <div className="kv"><span className="kv-key">Last call provider</span><span className="kv-val" style={{ fontSize: 11 }}>{h.last_call_provider || "—"}</span></div>
        </div>
      </div>

      {/* Row 4: daily rollup table (last 30 days, newest first) */}
      <div className="grid" style={{ marginTop: 16 }}>
        <div className="card span-12">
          <div className="card-title">Daily Token Rollups (last 30 days · live = today)</div>
          <table className="rollup-table">
            <thead>
              <tr>
                <th>Date (CDT)</th>
                <th className="num">Input</th>
                <th className="num">Output</th>
                <th className="num">Cache read</th>
                <th className="num">Sessions</th>
                <th className="num">Msgs</th>
                <th className="num">Tools</th>
                <th>Models</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rollups.length === 0 ? (
                <tr>
                  <td colSpan={9} style={{ textAlign: "center", color: "var(--text-muted)", padding: 20 }}>
                    no rollups
                  </td>
                </tr>
              ) : (
                rollups.slice(-30).reverse().map((r) => (
                  <tr key={r.date_local} className={r.rollup_complete ? "" : "incomplete"}>
                    <td>{r.date_local}</td>
                    <td className="num">{fmtInt(r.input_tokens)}</td>
                    <td className="num">{fmtInt(r.output_tokens)}</td>
                    <td className="num">{fmtInt(r.cache_read_tokens)}</td>
                    <td className="num">{r.total_sessions}</td>
                    <td className="num">{r.total_messages}</td>
                    <td className="num">{r.total_tool_calls}</td>
                    <td>{Object.keys(r.by_model || {}).join(", ") || "—"}</td>
                    <td>{r.rollup_complete ? "sealed" : "live"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="footer">
        <span>Backend: {apiUrl || "(same origin)"}</span>
        <span>
          Source: token_tracker.py cron (23:55 daily) · {snap.last_full_refresh ? new Date(snap.last_full_refresh).toLocaleString("en-US", { hour12: false }) : "—"}
        </span>
      </div>
    </div>
  );
}
