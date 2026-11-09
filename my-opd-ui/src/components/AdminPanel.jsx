import React, { useState, useEffect, useRef } from "react";

const API_BASE = "http://localhost:8000";

/* NEW (FILTER): options shown in the filter popup */
const REVIEW_TYPES = [
  { value: "AUTO_APPROVE", label: "Auto approve" },
  { value: "HUMAN_REVIEW", label: "Human review" },
];
const STATUSES = ["PENDING", "APPROVED", "REJECTED", "FLAGGED"];

/* the same value the table shows in the "Review Type" column */
const reviewTypeOf = (c) => c.score || c.confidentiality_status || "Unknown";

const AdminPanel = () => {
  const [claims, setClaims] = useState([]);
  const [loading, setLoading] = useState(true);        // first load only
  const [refreshing, setRefreshing] = useState(false); // spinner on the refresh button
  const [lastUpdated, setLastUpdated] = useState(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [isLoggedIn, setIsLoggedIn] = useState(!!localStorage.getItem("admin_token"));
  const [authData, setAuthData] = useState({ username: "", password: "" });
  const [authError, setAuthError] = useState("");

  /* NEW (FILTER): empty list = "show all" for that group */
  const [filterOpen, setFilterOpen] = useState(false);
  const [reviewFilter, setReviewFilter] = useState([]);
  const [statusFilter, setStatusFilter] = useState([]);
  const filterRef = useRef(null);

  const activeFilters = reviewFilter.length + statusFilter.length;

  const toggle = (list, setList, value) =>
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

  const clearFilters = () => {
    setReviewFilter([]);
    setStatusFilter([]);
  };

  /* close the popup when clicking outside it or pressing Escape */
  useEffect(() => {
    if (!filterOpen) return;
    const onDown = (e) => {
      if (filterRef.current && !filterRef.current.contains(e.target)) setFilterOpen(false);
    };
    const onKey = (e) => e.key === "Escape" && setFilterOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [filterOpen]);

  /* silent = true  -> refresh button: keeps the table, search text and login as they are */
  const fetchClaims = async (silent = false) => {
    if (silent) setRefreshing(true);
    else setLoading(true);
    try {
      const token = localStorage.getItem("admin_token");
      const res = await fetch(`${API_BASE}/admin/claims`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      // Only a 401 (token rejected) logs the admin out. Any other error keeps the session.
      if (res.status === 401) {
        setIsLoggedIn(false);
        localStorage.removeItem("admin_token");
        return;
      }
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      const data = await res.json();
      setClaims(Array.isArray(data) ? data : []);
      setLastUpdated(new Date());
    } catch (e) {
      console.error("Failed to fetch claims", e);
      if (silent) alert("Could not refresh claims. You are still logged in, please try again.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    if (isLoggedIn) fetchClaims();
  }, [isLoggedIn]);

  const handleLogin = async (e) => {
    e.preventDefault();
    setAuthError("");
    try {
      const res = await fetch(`${API_BASE}/admin/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(authData),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Login failed");

      localStorage.setItem("admin_token", data.access_token);
      setIsLoggedIn(true);
    } catch (e) {
      setAuthError(e.message);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem("admin_token");
    setClaims([]);
    clearFilters();
    setFilterOpen(false);
    setIsLoggedIn(false);
  };

  const handleAction = async (claimId, status) => {
    try {
      const token = localStorage.getItem("admin_token");
      const res = await fetch(`${API_BASE}/admin/claims/${claimId}/action`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ status }),
      });

      if (!res.ok) {
        const errorData = await res.json();
        throw new Error(errorData.message || "Action failed");
      }

      fetchClaims(true); // refresh list without the full-page loader
    } catch (e) {
      alert("Action failed: " + e.message);
    }
  };

  const handleViewBill = (filePath) => {
    if (!filePath) {
      alert("No bill file available for this claim.");
      return;
    }
    const filename = filePath.split("/").pop();
    window.open(`${API_BASE}/uploads/${filename}`, "_blank");
  };

  if (!isLoggedIn) {
    return (
      <div style={styles.authContainer}>
        <form onSubmit={handleLogin} style={styles.authForm}>
          <h2 style={styles.authHeader}>Admin Login</h2>
          {authError && <div style={styles.errorMsg}>{authError}</div>}
          <div style={styles.inputGroup}>
            <label>Username</label>
            <input
              type="text"
              style={styles.authInput}
              value={authData.username}
              onChange={(e) => setAuthData({ ...authData, username: e.target.value })}
              required
            />
          </div>
          <div style={styles.inputGroup}>
            <label>Password</label>
            <input
              type="password"
              style={styles.authInput}
              value={authData.password}
              onChange={(e) => setAuthData({ ...authData, password: e.target.value })}
              required
            />
          </div>
          <button type="submit" style={styles.loginBtn}>Sign In</button>
        </form>
      </div>
    );
  }

  if (loading) return <div style={styles.container}>Loading claims...</div>;

  /* search + review type filter + status filter, all combined */
  const visibleClaims = claims.filter((c) => {
    const matchesSearch =
      String(c.id).toLowerCase().includes(searchTerm.toLowerCase()) ||
      String(c.user_id).toLowerCase().includes(searchTerm.toLowerCase());
    const matchesReview = reviewFilter.length === 0 || reviewFilter.includes(reviewTypeOf(c));
    const matchesStatus = statusFilter.length === 0 || statusFilter.includes(c.status);
    return matchesSearch && matchesReview && matchesStatus;
  });

  return (
    <div style={styles.container}>
      <style>{`@keyframes adminSpin{to{transform:rotate(360deg)}}`}</style>

      <div style={styles.headerContainer}>
        <div>
          <h1 style={styles.header}>OPD Claims Admin Panel</h1>
          <div style={styles.updated}>
            {lastUpdated ? `Last updated ${lastUpdated.toLocaleTimeString("en-IN")}` : ""}
          </div>
        </div>

        <div style={styles.controls}>
          <input
            type="text"
            placeholder="Search by Claim ID or User ID..."
            style={styles.searchInput}
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
          />

          {/* NEW (FILTER): filter icon on the right side of the search bar */}
          <div style={styles.filterWrap} ref={filterRef}>
            <button
              onClick={() => setFilterOpen((o) => !o)}
              title="Filter claims"
              aria-label="Filter claims"
              aria-expanded={filterOpen}
              aria-haspopup="dialog"
              style={{
                ...styles.filterBtn,
                backgroundColor: activeFilters ? "#14263f" : "#fff",
                color: activeFilters ? "#fff" : "#14263f",
              }}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
              </svg>
              {activeFilters > 0 && <span style={styles.filterBadge}>{activeFilters}</span>}
            </button>

            {filterOpen && (
              <div style={styles.filterPopup} role="dialog" aria-label="Filter claims">
                <div style={styles.filterGroup}>
                  <div style={styles.filterTitle}>Review Type</div>
                  {REVIEW_TYPES.map((o) => (
                    <label key={o.value} style={styles.filterOption}>
                      <input
                        type="checkbox"
                        checked={reviewFilter.includes(o.value)}
                        onChange={() => toggle(reviewFilter, setReviewFilter, o.value)}
                      />
                      {o.label}
                    </label>
                  ))}
                </div>

                <div style={styles.filterGroup}>
                  <div style={styles.filterTitle}>Status</div>
                  {STATUSES.map((s) => (
                    <label key={s} style={styles.filterOption}>
                      <input
                        type="checkbox"
                        checked={statusFilter.includes(s)}
                        onChange={() => toggle(statusFilter, setStatusFilter, s)}
                      />
                      {s.charAt(0) + s.slice(1).toLowerCase()}
                    </label>
                  ))}
                </div>

                <div style={styles.filterFooter}>
                  <span style={styles.filterCount}>
                    {visibleClaims.length} of {claims.length} claims
                  </span>
                  <button
                    onClick={clearFilters}
                    disabled={!activeFilters}
                    style={{ ...styles.clearBtn, opacity: activeFilters ? 1 : 0.4 }}
                  >
                    Clear all
                  </button>
                </div>
              </div>
            )}
          </div>

          <button
            onClick={() => fetchClaims(true)}
            disabled={refreshing}
            title="Refresh claims"
            aria-label="Refresh claims"
            style={styles.refreshBtn}
          >
            <span
              style={{
                display: "inline-block",
                animation: refreshing ? "adminSpin 0.8s linear infinite" : "none",
              }}
            >
              ⟳
            </span>
          </button>
          <button onClick={handleLogout} style={{ ...styles.btn, backgroundColor: "#666" }}>
            Logout
          </button>
        </div>
      </div>

      <div style={styles.tableWrapper}>
        <table style={styles.table}>
          <thead>
            <tr style={{ backgroundColor: "#14263f", color: "#fff" }}>
              <th style={styles.th}>Claim ID</th>
              <th style={styles.th}>User ID</th>
              <th style={styles.th}>Amount</th>
              <th style={styles.th}>Review Type</th>
              <th style={styles.th}>Score</th>
              <th style={styles.th}>Status</th>
              <th style={styles.th}>Reason</th>
              <th style={styles.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visibleClaims.length === 0 && (
              <tr>
                <td style={styles.td} colSpan={8}>
                  No claims found.
                  {activeFilters > 0 && (
                    <>
                      {" "}
                      <button onClick={clearFilters} style={styles.linkBtn}>Clear filters</button>
                    </>
                  )}
                </td>
              </tr>
            )}
            {visibleClaims.map((c) => {
              const isProcessed = c.status === "APPROVED" || c.status === "REJECTED";
              const isAutoApproved =
                c.score === "AUTO_APPROVE" || c.confidentiality_status === "AUTO_APPROVE";

              return (
                <tr key={c.id} style={styles.tr}>
                  <td style={styles.td}>{c.id}</td>
                  <td style={styles.td}>{c.user_id}</td>
                  <td style={styles.td}>₹{c.amount}</td>
                  <td style={styles.td}>
                    <span
                      style={{
                        ...styles.pill,
                        backgroundColor: isAutoApproved ? "#dff3e8" : "#fff4df",
                      }}
                    >
                      {c.score || c.confidentiality_status || "Unknown"}
                    </span>
                  </td>
                  <td style={styles.td}>
                    <span
                      style={{
                        ...styles.pill,
                        backgroundColor:
                          c.confidence_value >= 0.8 || c.score >= 0.8 ? "#dff3e8" : "#fff4df",
                      }}
                    >
                      {((c.confidence_value || c.score || 0) * 100).toFixed(1)}%
                    </span>
                  </td>
                  <td style={styles.td}>
                    <span style={styles.statusPill(c.status)}>{c.status}</span>
                  </td>
                  <td style={styles.td}>
                    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                      <span
                        style={{
                          color: isAutoApproved ? "#18794e" : "#d32f2f",
                          fontSize: "12px",
                          fontWeight: "500",
                          display: "block",
                          whiteSpace: "normal",
                          lineHeight: "1.4",
                          maxWidth: "300px",
                        }}
                      >
                        {c.review_reason ||
                          (isAutoApproved
                            ? "Auto-approved based on high confidence score"
                            : "No reason provided")}
                      </span>
                      <button
                        onClick={() => handleViewBill(c.file_path)}
                        style={{
                          background: "none",
                          border: "none",
                          cursor: "pointer",
                          fontSize: "16px",
                          padding: "0",
                          marginLeft: "5px",
                        }}
                        title="View Bill"
                      >
                        👁️
                      </button>
                    </div>
                  </td>
                  <td style={styles.td}>
                    {isAutoApproved ? (
                      <button
                        style={{ ...styles.btn, backgroundColor: "#d32f2f" }}
                        onClick={() => handleAction(c.id, "FLAGGED")}
                      >
                        🚩 Flag
                      </button>
                    ) : (
                      <>
                        <button
                          style={{
                            ...styles.btn,
                            backgroundColor: "#18794e",
                            filter: isProcessed ? "blur(1px)" : "none",
                            opacity: isProcessed ? 0.6 : 1,
                          }}
                          onClick={() => handleAction(c.id, "APPROVED")}
                          disabled={c.status === "APPROVED"}
                        >
                          Approve
                        </button>
                        <button
                          style={{
                            ...styles.btn,
                            backgroundColor: "#d32f2f",
                            marginLeft: 8,
                            filter: isProcessed ? "blur(1px)" : "none",
                            opacity: isProcessed ? 0.6 : 1,
                          }}
                          onClick={() => handleAction(c.id, "REJECTED")}
                          disabled={c.status === "REJECTED"}
                        >
                          Reject
                        </button>
                      </>
                    )}
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
  container: {
    padding: "40px",
    fontFamily: "system-ui, sans-serif",
    backgroundColor: "#f5f7fa",
    minHeight: "100vh",
  },
  authContainer: {
    display: "flex",
    justifyContent: "center",
    alignItems: "center",
    height: "100vh",
    backgroundColor: "#f5f7fa",
    fontFamily: "system-ui, sans-serif",
  },
  authForm: {
    backgroundColor: "#fff",
    padding: "40px",
    borderRadius: "12px",
    boxShadow: "0 4px 20px rgba(0,0,0,0.1)",
    width: "400px",
    display: "flex",
    flexDirection: "column",
    gap: "20px",
  },
  authHeader: { textAlign: "center", color: "#14263f", marginBottom: "10px" },
  inputGroup: { display: "flex", flexDirection: "column", gap: "8px", textAlign: "left" },
  authInput: { padding: "12px", borderRadius: "6px", border: "1px solid #ddd", fontSize: "14px" },
  loginBtn: {
    padding: "12px",
    borderRadius: "6px",
    border: "none",
    backgroundColor: "#14263f",
    color: "#fff",
    fontSize: "16px",
    fontWeight: "600",
    cursor: "pointer",
  },
  errorMsg: { color: "#d32f2f", fontSize: "14px", textAlign: "center", marginBottom: "10px" },
  headerContainer: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    flexWrap: "wrap",
    marginBottom: "30px",
    gap: "20px",
  },
  header: { color: "#14263f", margin: 0, fontSize: "24px" },
  updated: { color: "#4b5b73", fontSize: "12px", marginTop: "4px", minHeight: "14px" },
  controls: { display: "flex", alignItems: "center", gap: "12px" },
  searchInput: {
    padding: "10px 15px",
    borderRadius: "8px",
    border: "1px solid #ccc",
    fontSize: "14px",
    width: "300px",
    outline: "none",
    boxShadow: "0 2px 4px rgba(0,0,0,0.05)",
  },

  /* NEW (FILTER) */
  filterWrap: { position: "relative" },
  filterBtn: {
    position: "relative",
    width: "40px",
    height: "40px",
    borderRadius: "8px",
    border: "1px solid #ccc",
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    boxShadow: "0 2px 4px rgba(0,0,0,0.05)",
  },
  filterBadge: {
    position: "absolute",
    top: "-6px",
    right: "-6px",
    minWidth: "18px",
    height: "18px",
    padding: "0 5px",
    borderRadius: "99px",
    backgroundColor: "#0f8f7e",
    color: "#fff",
    fontSize: "11px",
    fontWeight: "700",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
  filterPopup: {
    position: "absolute",
    top: "48px",
    right: 0,
    width: "240px",
    backgroundColor: "#fff",
    border: "1px solid #d9e0ea",
    borderRadius: "10px",
    boxShadow: "0 8px 28px rgba(20,38,63,0.18)",
    padding: "14px",
    zIndex: 20,
    display: "flex",
    flexDirection: "column",
    gap: "14px",
  },
  filterGroup: { display: "flex", flexDirection: "column", gap: "8px" },
  filterTitle: { fontSize: "12px", fontWeight: "700", color: "#4b5b73", textTransform: "uppercase", letterSpacing: "0.5px" },
  filterOption: { display: "flex", alignItems: "center", gap: "8px", fontSize: "14px", color: "#14263f", cursor: "pointer" },
  filterFooter: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    borderTop: "1px solid #eee",
    paddingTop: "10px",
  },
  filterCount: { fontSize: "12px", color: "#4b5b73" },
  clearBtn: {
    border: "none",
    background: "none",
    color: "#0b6f62",
    fontWeight: "700",
    fontSize: "13px",
    cursor: "pointer",
    padding: 0,
  },
  linkBtn: {
    border: "none",
    background: "none",
    color: "#0b6f62",
    fontWeight: "700",
    fontSize: "14px",
    cursor: "pointer",
    padding: 0,
    textDecoration: "underline",
  },

  refreshBtn: {
    width: "40px",
    height: "40px",
    borderRadius: "50%",
    border: "none",
    backgroundColor: "#0f8f7e",
    color: "#fff",
    fontSize: "22px",
    lineHeight: 1,
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    boxShadow: "0 2px 6px rgba(0,0,0,0.2)",
  },
  tableWrapper: {
    backgroundColor: "#fff",
    borderRadius: "12px",
    boxShadow: "0 4px 20px rgba(0,0,0,0.08)",
    overflow: "hidden",
  },
  table: { width: "100%", borderCollapse: "collapse", textAlign: "left" },
  th: {
    backgroundColor: "#14263f",
    color: "#fff",
    padding: "15px",
    fontSize: "14px",
    fontWeight: "600",
    whiteSpace: "nowrap",
  },
  td: { padding: "15px", borderBottom: "1px solid #eee", fontSize: "14px", verticalAlign: "top" },
  tr: { transition: "background 0.2s" },
  btn: {
    border: "none",
    color: "#fff",
    padding: "6px 12px",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: "600",
  },
  pill: { padding: "4px 8px", borderRadius: "99px", fontSize: "12px", fontWeight: "700" },
  statusPill: (status) => ({
    padding: "4px 8px",
    borderRadius: "99px",
    fontSize: "12px",
    fontWeight: "700",
    backgroundColor:
      status === "APPROVED" ? "#dff3e8" : status === "REJECTED" ? "#ffebee" : "#fff4df",
    color: status === "APPROVED" ? "#18794e" : status === "REJECTED" ? "#d32f2f" : "#b86e00",
  }),
};

export default AdminPanel;