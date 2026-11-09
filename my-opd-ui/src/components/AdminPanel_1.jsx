import React, { useCallback, useEffect, useState } from "react";

const API_BASE = (import.meta.env?.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const REFRESH_MS = 5000;

async function readJson(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  }
  return data;
}

function normalizeClaims(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.claims)) return payload.claims;
  if (Array.isArray(payload.data)) return payload.data;
  if (Array.isArray(payload.data?.claims)) return payload.data.claims;
  return [];
}

const AdminPanel = () => {
  const [claims, setClaims] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");

  const fetchClaims = useCallback(async (showLoader = false) => {
    if (showLoader) setLoading(true);
    try {
      const payload = await readJson(await fetch(`${API_BASE}/admin/claims`, {
        headers: { Accept: "application/json" },
      }));
      setClaims(normalizeClaims(payload));
      setError("");
    } catch (err) {
      setError(`Could not load claims: ${err.message}`);
    } finally {
      if (showLoader) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchClaims(true);
    const timer = setInterval(() => fetchClaims(false), REFRESH_MS);
    return () => clearInterval(timer);
  }, [fetchClaims]);

  const handleAction = async (claimId, status) => {
    if (busyId !== null) return;
    setBusyId(claimId);
    setError("");

    try {
      // Keep the existing endpoint contract used by your backend.
      await readJson(await fetch(`${API_BASE}/admin/claims/${encodeURIComponent(claimId)}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ status }),
      }));

      // Do not optimistically mutate the row. Re-read the authoritative DB-backed list.
      await fetchClaims(false);
    } catch (err) {
      setError(`Could not ${status.toLowerCase()} claim #${claimId}: ${err.message}`);
    } finally {
      setBusyId(null);
    }
  };

  if (loading) return <div style={styles.container}>Loading claims…</div>;

  return (
    <div style={styles.container}>
      <div style={styles.top}>
        <h1 style={styles.header}>OPD Claims Admin Panel</h1>
        <button style={styles.refresh} onClick={() => fetchClaims(true)}>Refresh</button>
      </div>

      {error && <div role="alert" style={styles.error}>{error}</div>}
      <div style={styles.meta}>Auto-refreshes every 5 seconds · {claims.length} claim(s)</div>

      <div style={styles.tableWrapper}>
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>Claim ID</th>
              <th style={styles.th}>User ID</th>
              <th style={styles.th}>Amount</th>
              <th style={styles.th}>Extraction review</th>
              <th style={styles.th}>Claim status</th>
              <th style={styles.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {claims.length === 0 ? (
              <tr><td style={styles.empty} colSpan={6}>No claims found.</td></tr>
            ) : claims.map((claim) => {
              const status = String(claim.status ?? claim.claim_status ?? "PENDING").toUpperCase();
              const amount = Number(claim.amount ?? claim.bill_amount ?? 0);
              const confidence = Number(claim.confidence_score ?? claim.score ?? 0);
              const isBusy = busyId === claim.id;

              return (
                <tr key={claim.id}>
                  <td style={styles.td}>{claim.id}</td>
                  <td style={styles.td}>{claim.user_id}</td>
                  <td style={styles.td}>{new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(amount)}</td>
                  <td style={styles.td}>
                    <span style={{ ...styles.pill, background: confidence >= 80 ? "#dff3e8" : "#fff4df" }}>
                      {confidence || "N/A"}{confidence ? "%" : ""}
                    </span>
                  </td>
                  <td style={styles.td}>
                    <span style={styles.statusPill(status)}>{status}</span>
                  </td>
                  <td style={styles.td}>
                    <button
                      style={{ ...styles.btn, background: "#18794e" }}
                      onClick={() => handleAction(claim.id, "APPROVED")}
                      disabled={isBusy || status !== "PENDING"}
                    >
                      {isBusy ? "Saving…" : "Approve"}
                    </button>
                    <button
                      style={{ ...styles.btn, background: "#d32f2f", marginLeft: 8 }}
                      onClick={() => handleAction(claim.id, "REJECTED")}
                      disabled={isBusy || status !== "PENDING"}
                    >
                      Reject
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const styles = {
  container: { padding: 32, fontFamily: "system-ui,sans-serif", background: "#f5f7fa", minHeight: "100vh", color: "#14263f" },
  top: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 },
  header: { margin: "0 0 20px", color: "#14263f" },
  refresh: { border: 0, borderRadius: 7, padding: "8px 13px", background: "#14263f", color: "#fff", cursor: "pointer" },
  meta: { color: "#64748b", fontSize: 12, marginBottom: 10 },
  error: { background: "#ffebee", color: "#b42318", border: "1px solid #f3b7bc", borderRadius: 8, padding: 10, marginBottom: 12 },
  tableWrapper: { background: "#fff", borderRadius: 12, boxShadow: "0 4px 20px rgba(0,0,0,.08)", overflowX: "auto" },
  table: { width: "100%", borderCollapse: "collapse", textAlign: "left" },
  th: { background: "#14263f", color: "#fff", padding: 14, fontSize: 13, whiteSpace: "nowrap" },
  td: { padding: 14, borderBottom: "1px solid #eee", fontSize: 13 },
  empty: { padding: 24, textAlign: "center", color: "#64748b" },
  btn: { border: 0, color: "#fff", padding: "7px 11px", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 600 },
  pill: { padding: "4px 8px", borderRadius: 99, fontSize: 12, fontWeight: 700 },
  statusPill: (status) => ({
    display: "inline-block", padding: "4px 8px", borderRadius: 99, fontSize: 12, fontWeight: 700,
    background: status === "APPROVED" ? "#dff3e8" : status === "REJECTED" ? "#ffebee" : "#fff4df",
    color: status === "APPROVED" ? "#18794e" : status === "REJECTED" ? "#d32f2f" : "#b86e00",
  }),
};

export default AdminPanel;
