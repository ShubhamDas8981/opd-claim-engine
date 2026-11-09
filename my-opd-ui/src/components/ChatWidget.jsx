import { useState, useRef, useEffect } from "react";

/* =====================================================================
   OPD Wallet Chat Widget
   NEW (search for "NEW"):
   - Login with a User ID + Email ID (saved in the browser) + Log out button
   - Every API call uses the logged-in user's ID instead of "USER123"
   - Chat history is cached per user in localStorage for 1 hour
   - "Talk to a human" popup form -> HubSpot ticket (HANDOFF)
   - NEW (EMAIL): login asks for an email too, it is shown in the header
     and pre-filled in the "Talk to a human" form
   CHANGED (search for "CHANGED"):
   - FLAGGED claims are shown to the user as APPROVED
   - Status pill no longer wraps ("APPROVE / D")
   ===================================================================== */

/* the database stores rupees, so no paise conversion */
const money = (rupees) =>
  "₹" + Number(rupees || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const API_BASE = "http://localhost:8000";

/* ---------- NEW: login + per-user history cache ---------- */
const USER_KEY = "opd_user_id";
const EMAIL_KEY = "opd_user_email"; // NEW (EMAIL)
const HISTORY_TTL_MS = 60 * 60 * 1000; // 1 hour
const histKey = (uid) => `opd_chat_${uid}`;

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/; // NEW (EMAIL)

const WELCOME = {
  role: "bot",
  type: "text",
  text: "Hi, I'm your OPD benefits assistant. Ask about your balance, upload a bill to claim, or ask what's covered.",
};

const getSavedUser = () => {
  try { return localStorage.getItem(USER_KEY) || null; } catch { return null; }
};

/* NEW (EMAIL) */
const getSavedEmail = () => {
  try { return localStorage.getItem(EMAIL_KEY) || ""; } catch { return ""; }
};

/* Returns the saved messages, or null if there are none or they are older than 1 hour */
const loadHistory = (uid) => {
  try {
    const raw = localStorage.getItem(histKey(uid));
    if (!raw) return null;
    const { savedAt, messages } = JSON.parse(raw);
    if (!Array.isArray(messages) || Date.now() - savedAt > HISTORY_TTL_MS) {
      localStorage.removeItem(histKey(uid));
      return null;
    }
    return messages;
  } catch { return null; }
};

/* The 1 hour counts from the last message (savedAt is refreshed on every save) */
const saveHistory = (uid, messages) => {
  try { localStorage.setItem(histKey(uid), JSON.stringify({ savedAt: Date.now(), messages })); } catch { /* storage full or blocked */ }
};

/* ---------- turn any backend payload into a clean card ---------- */
const tryParse = (v) => {
  if (typeof v !== "string") return v;
  try { return JSON.parse(v); } catch { return null; }
};

function normalizeExtraction(d = {}, fileName = "", claimId = null) {
  const fromExtracted = tryParse(d.extracted);
  const extractedObj = fromExtracted && typeof fromExtracted === "object" ? fromExtracted : {};
  const amountLooksLikeJson = typeof d.amount === "string" && d.amount.trim().startsWith("{");
  const fromAmount = amountLooksLikeJson ? (tryParse(d.amount) || {}) : {};
  const src = { ...fromAmount, ...extractedObj };

  const pick = (...keys) => {
    for (const k of keys) {
      const v = src[k] ?? d[k];
      if (v !== undefined && v !== null && v !== "" && v !== "Unknown" && typeof v !== "object") return v;
    }
    return null;
  };

  const provider = pick("provider", "provider_name");
  const date = pick("date", "bill_date");
  const rawAmount = amountLooksLikeJson ? src.amount : pick("amount", "total_amount", "total");
  const num = rawAmount == null ? null : String(rawAmount).match(/\d[\d,]*(?:\.\d+)?/);
  const amount = num ? Number(num[0].replace(/,/g, "")) : null;
  const conf = src.confidence || d.confidence || {};

  const field = (key, value, confKey) => {
    const c = typeof conf[confKey] === "number" ? conf[confKey] : null;
    return { key, value: value ?? "Not found", conf: c, low: value == null || (c !== null && c < 0.8) };
  };

  // AUTO_APPROVE or HUMAN_REVIEW, the same value the admin panel shows as "Review Type".
  // If the backend does not say AUTO_APPROVE clearly, we treat it as HUMAN_REVIEW (the safe default).
  const reviewRaw = [src.score, src.decision, src.review_type, src.confidentiality_status,
                     d.score, d.decision, d.review_type, d.confidentiality_status]
    .map((v) => String(v ?? "").toUpperCase()).join(" ");
  const reviewType = reviewRaw.includes("AUTO_APPROVE") && !reviewRaw.includes("HUMAN_REVIEW")
    ? "AUTO_APPROVE" : "HUMAN_REVIEW";

  return {
    type: "extraction",
    fileName,
    claimId,
    reviewType,
    blurry: d.score === "HUMAN_REVIEW" || d.decision === "HUMAN_REVIEW",
    fields: [
      field("Provider", provider, "provider"),
      field("Total",
        amount == null || Number.isNaN(amount) ? null
          : "₹" + amount.toLocaleString("en-IN", { minimumFractionDigits: amount % 1 ? 2 : 0 }),
        "amount"),
      field("Date", date, "date"),
    ],
  };
}

/* ---------- 1. API LAYER (NEW: every call takes the userId) ---------- */
const api = {
  // NEW (EMAIL): the backend saves the email in the users table and checks it on later logins
  async login(userId, email) {
    try {
      const res = await fetch(`${API_BASE}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, email }),
      });
      const s = await res.json().catch(() => ({}));
      if (res.ok && s.status === "ok") return { ok: true, userId: s.user_id, email: s.email };
      return { ok: false, message: s.message || "Login failed. Please try again." };
    } catch {
      return { ok: false, message: "Could not reach the server. Please try again." };
    }
  },

  async sendMessage(userId, text) {
    const formData = new FormData();
    formData.append("user_id", userId);
    formData.append("message", text);

    const res = await fetch(`${API_BASE}/chat`, { method: "POST", body: formData });
    const data = await res.json();

    const response = data.response;
    if (typeof response === "string") return [{ type: "text", text: response }];

    if (response.current_balance !== undefined) {
      const left = Number(response.current_balance) || 0;
      const total = Number(response.total_limit) || left;
      return [{
        type: "balance",
        total,
        used: Math.max(total - left, 0),
        left,
        note: response.note || "Live account data",
      }];
    }
    if (response.status === "submitted") return [{ type: "text", text: response.message }];
    // NEW: the user asked for a human, so we show a card that opens the form
    if (response.status === "handoff") return [{ type: "handoff", text: response.message }];
    if (response.answer) return [{ type: "policy", text: response.answer, source: "Company Policy DB" }];
    if (response.status === "escalated") return [{ type: "text", text: response.message }];
    if (response.claims_history) return [{
      type: "history",
      claims: response.claims_history,
      text: response.message || "Your recent claims history",
    }];

    return [{ type: "text", text: JSON.stringify(response) }];
  },

  async uploadBill(userId, file) {
    const formData = new FormData();
    formData.append("user_id", userId);
    formData.append("message", "I am uploading a medical bill");
    formData.append("file", file);

    const res = await fetch(`${API_BASE}/chat`, { method: "POST", body: formData });
    const data = await res.json();
    const claimId = data.claim_id;
    // NEW (GUARDRAILS): the server refused the file (wrong type, too big, too many requests)
    if (!claimId && typeof data.response === "string") return [{ type: "text", text: data.response }];
    if (!claimId) throw new Error("Failed to get claim ID");

    for (let attempt = 0; attempt < 45; attempt++) {
      // user_id lets the server confirm this claim belongs to the logged-in user
      const statusRes = await fetch(`${API_BASE}/claims/${claimId}/status?user_id=${encodeURIComponent(userId)}`);
      const s = await statusRes.json();
      if (s.status === "failed") {
        return [{ type: "text", text: s.error || "We could not read this bill. Please try again." }];
      }
      if (s.status === "low_balance") {
        return [{ type: "lowbalance", amount: s.data.amount, balance: s.data.balance, claimId }];
      }
      if (s.status === "completed") return [normalizeExtraction(s.data ?? s, file.name, claimId)];
      await wait(2000);
    }
    throw new Error("Timed out waiting for the bill to be processed");
  },

  async checkPolicy({ reviewType, claimId }) {
    return [{ type: "confirm", text: "This claim is within your limit. Submit?", action: "submit", reviewType, claimId }];
  },

  // The claim only reaches the admin / balance when this is called
  async submitClaim(userId, { claimId }) {
    const res = await fetch(`${API_BASE}/claims/${claimId}/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId }),
    });
    const s = await res.json();

    if (s.status === "approved") return [{
      type: "result", status: "approved", title: "Claim Approved",
      text: "Amount will be credited to your Bank Account Soon",
      balance: s.balance, claimId,
    }];
    if (s.status === "pending") return [{
      type: "result", status: "pending", title: "Under Process",
      text: "Claim is under Human Review.", claimId,
    }];
    if (s.status === "low_balance") return [{ type: "lowbalance", amount: s.amount, balance: s.balance, claimId }];
    if (s.status === "already_submitted") return [{ type: "text", text: "This claim was already submitted." }];
    return [{ type: "text", text: s.message || "We could not submit this claim. Please try again." }];
  },

  // Cancel before submitting: the claim never reaches the admin
  async cancelClaim(userId, claimId) {
    const res = await fetch(`${API_BASE}/claims/${claimId}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId }),
    });
    const s = await res.json();
    if (s.status === "cancelled") return [{ type: "text", text: "Cancelled. Nothing was submitted." }];
    return [{ type: "text", text: s.message || "This claim could not be cancelled." }];
  },

  // Change date / amount / provider by typing in the chat
  async editClaim(userId, claimId, message, fileName) {
    const res = await fetch(`${API_BASE}/claims/${claimId}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, message }),
    });
    const s = await res.json();

    if (s.status === "updated") return {
      applied: true,
      messages: [
        { type: "text", text: s.message },
        normalizeExtraction(s.data, fileName, claimId),
      ],
    };
    if (s.status === "low_balance") return {
      applied: true,
      messages: [{ type: "lowbalance", amount: s.data.amount, balance: s.data.balance, claimId }],
    };
    // nothing changed (or an error): stay in edit mode so the user can try again
    return { applied: false, messages: [{ type: "text", text: s.message || "Could not update this claim." }] };
  },

  // NEW (HANDOFF): form + last hour of chat + claim ids -> backend builds the HubSpot ticket
  async submitHandoff(userId, form, history, claimIds) {
    const res = await fetch(`${API_BASE}/support/handoff`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, ...form, history, claim_ids: claimIds }),
    });
    const s = await res.json().catch(() => ({}));
    if (res.ok && s.status === "ok") return { ok: true, ticketId: s.ticket_id };
    return { ok: false, message: s.message || "Could not reach support. Please try again." };
  },
};

/* ---------- 2. STYLES ---------- */
const css = `
@import url('https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap');
.ow{--ink:#14263f;--ink2:#4b5b73;--line:#d9e0ea;--bg:#f5f7fa;--card:#fff;--teal:#0f8f7e;--teal-d:#0b6f62;--amber:#b86e00;--amber-bg:#fff4df;--ok:#18794e;
 font-family:'Atkinson Hyperlegible',system-ui,sans-serif;color:var(--ink);font-size:15px;line-height:1.45}
.ow *{box-sizing:border-box}
.ow-launch{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;background:var(--ink);color:#fff;font-size:24px;cursor:pointer;box-shadow:0 6px 20px rgba(20,38,63,.3)}
.ow-launch:focus-visible,.ow button:focus-visible,.ow input:focus-visible,.ow textarea:focus-visible{outline:3px solid #7fd6c9;outline-offset:2px}
.ow-panel{position:fixed;right:20px;bottom:90px;width:380px;max-width:calc(100vw - 24px);height:600px;max-height:calc(100vh - 110px);background:var(--bg);border:1px solid var(--line);border-radius:14px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 16px 48px rgba(20,38,63,.22)}
.ow-head{background:var(--ink);color:#fff;padding:14px 16px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.ow-head b{font-size:16px}.ow-head small{display:block;color:#aebbd0;font-size:12px;overflow-wrap:anywhere}
.ow-head-btns{display:flex;gap:6px;flex-shrink:0}
.ow-human{background:transparent;color:#fff;border:1px solid #5d7089;border-radius:8px;padding:6px 10px;font:inherit;font-size:13px;cursor:pointer}
.ow-body{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.ow-msg{max-width:88%;padding:10px 12px;border-radius:12px;background:var(--card);border:1px solid var(--line);white-space:pre-wrap}
.ow-msg.me{align-self:flex-end;background:var(--ink);color:#fff;border-color:var(--ink)}
.ow-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;align-self:stretch}
.ow-big{font-size:28px;font-weight:700;letter-spacing:-.5px}
.ow-bar{height:8px;background:#e6ebf2;border-radius:4px;margin:8px 0;overflow:hidden}
.ow-bar i{display:block;height:100%;background:var(--teal)}
.ow-muted{color:var(--ink2);font-size:13px}
.ow-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;margin-top:6px;background:var(--bg)}
.ow-row.warn{background:var(--amber-bg);border:1px solid #f0cf9a}
/* CHANGED: left side wraps long IDs, right side never shrinks so the pill stays on one line */
.ow-row .k{color:var(--ink2);font-size:13px;min-width:0;overflow-wrap:anywhere}
.ow-row .v{font-weight:700;text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0;white-space:nowrap}
.ow-flag{color:var(--amber);font-size:12px;margin-top:2px}
.ow-actions{display:flex;gap:8px;margin-top:10px}
.ow-btn{flex:1;border-radius:8px;padding:9px 12px;font:inherit;font-weight:700;cursor:pointer;border:1px solid var(--teal);background:var(--teal);color:#fff}
.ow-btn:hover{background:var(--teal-d)}
.ow-btn.alt{background:#fff;color:var(--teal-d)}
.ow-btn:disabled{opacity:.45;cursor:default}
.ow-pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:700;white-space:nowrap}
.ow-pill.approved{background:#dff3e8;color:var(--ok)}.ow-pill.review{background:var(--amber-bg);color:var(--amber)}
.ow-pill.pending{background:#fff1b8;color:#7a5600}
.ow-pill.rejected{background:#fde2e2;color:#a11d1d}
.ow-pill.neutral{background:#e6ebf2;color:var(--ink2)}
.ow-cite{margin-top:6px;font-size:12px;color:var(--ink2);border-left:3px solid var(--teal);padding-left:8px}
.ow-chips{display:flex;gap:6px;flex-wrap:wrap;padding:0 14px 8px}
.ow-chip{border:1px solid var(--line);background:#fff;border-radius:99px;padding:5px 11px;font:inherit;font-size:13px;cursor:pointer;color:var(--ink)}
.ow-foot{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line);background:#fff}
.ow-foot input[type=text]{flex:1;border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit}
.ow-ic{border:1px solid var(--line);background:#fff;border-radius:8px;padding:0 11px;font-size:18px;cursor:pointer}
.ow-send{border:0;background:var(--ink);color:#fff;border-radius:8px;padding:0 14px;font:inherit;font-weight:700;cursor:pointer}
.ow-login{flex:1;display:flex;flex-direction:column;justify-content:center;gap:12px;padding:28px 24px}
.ow-login h2{margin:0;font-size:20px}
.ow-login input{border:1px solid var(--line);border-radius:8px;padding:11px 12px;font:inherit;background:#fff}
.ow-login .ow-btn{flex:none}
.ow-err{color:#a11d1d;font-size:13px}
/* NEW (HANDOFF): popup form */
.ow-modal{position:absolute;inset:0;background:rgba(20,38,63,.55);display:flex;align-items:center;justify-content:center;padding:12px;z-index:5}
.ow-modal-card{background:#fff;border-radius:12px;padding:16px;width:100%;max-height:100%;overflow-y:auto;display:flex;flex-direction:column;gap:10px}
.ow-modal-card h2{margin:0;font-size:18px}
.ow-modal-card label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--ink2);font-weight:700}
.ow-modal-card input,.ow-modal-card textarea{border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit;color:var(--ink);resize:vertical;background:#fff}
.ow-modal-card .ow-err{font-weight:400}
.ow-dots{display:inline-flex;gap:4px}.ow-dots i{width:6px;height:6px;border-radius:50%;background:var(--ink2);animation:owb 1s infinite}
.ow-dots i:nth-child(2){animation-delay:.15s}.ow-dots i:nth-child(3){animation-delay:.3s}
@keyframes owb{0%,80%,100%{opacity:.25}40%{opacity:1}}
@media (prefers-reduced-motion:reduce){.ow-dots i{animation:none}}
`;

/* ---------- 3. MESSAGE RENDERERS ---------- */
function BalanceCard({ m }) {
  const pct = m.total ? Math.min(100, Math.max(0, Math.round((m.used / m.total) * 100))) : 0;
  return (
    <div className="ow-card">
      <div className="ow-muted">OPD balance left</div>
      <div className="ow-big">{money(m.left)}</div>
      <div className="ow-bar"><i style={{ width: pct + "%" }} /></div>
      <div className="ow-muted">{money(m.used)} used of {money(m.total)}</div>
      <div className="ow-muted">{m.note}</div>
    </div>
  );
}

/* CHANGED: FLAGGED is an admin-side label, so the user sees it as APPROVED */
const displayStatus = (status) => {
  const v = String(status ?? "").toUpperCase();
  if (v === "FLAGGED") return "APPROVED";
  if (v === "LOW_BALANCE") return "LOW BALANCE";
  return status;
};

const statusKind = (status) => {
  const v = String(displayStatus(status) ?? "").toLowerCase();
  if (/reject|declin|denied|disapprov|low balance|failed/.test(v)) return "rejected";
  if (/approv|paid|complete/.test(v)) return "approved";
  if (/pend|review|process|hold|submit/.test(v)) return "pending";
  return "neutral";
};

/* NEW (HANDOFF): plain-text version of any message, so cards can go into the ticket */
const toPlain = (m) => {
  if (m.role === "me") return m.text;
  switch (m.type) {
    case "balance": return `Balance card: ${money(m.left)} left, ${money(m.used)} used of ${money(m.total)}`;
    case "history": return "Claims history: " + (m.claims || []).map((c) => `${c.claim_id} Rs ${c.amount} ${displayStatus(c.status)}`).join("; ");
    case "extraction": return "Bill read: " + (m.fields || []).map((f) => `${f.key}=${f.value}`).join(", ");
    case "result": return `${m.title}: ${m.text} (claim ${m.claimId})`;
    case "lowbalance": return `Not enough balance: bill ${money(m.amount)}, balance ${money(m.balance)}`;
    default: return m.text || "";
  }
};

function HistoryCard({ m }) {
  return (
    <div className="ow-card">
      <b>{m.text}</b>
      {m.claims.length === 0 ? (
        <div className="ow-muted" style={{ marginTop: 8 }}>No claims found.</div>
      ) : (
        m.claims.map((c, i) => (
          <div key={i} className="ow-row">
            <div className="k">{String(c.date ?? "").split(" ")[0]} · ID: {c.claim_id}</div>
            <div className="v">
              <span>₹{c.amount}</span>
              <span className={"ow-pill " + statusKind(c.status)}>{displayStatus(c.status)}</span>
            </div>
          </div>
        ))
      )}
    </div>
  );
}

function ExtractionCard({ m, onAction }) {
  return (
    <div className="ow-card">
      <b>Check what we read from your bill</b>
      <div className="ow-muted">{m.fileName}</div>
      {m.fields.map((f) => {
        const low = f.low;
        return (
          <div key={f.key} className={"ow-row" + (low ? " warn" : "")}>
            <div>
              <div className="k">{f.key}</div>
              {low && <div className="ow-flag">Not sure about this one. Please check.</div>}
            </div>
            <div className="v">{f.value}</div>
          </div>
        );
      })}
      <div className="ow-actions">
        <button className="ow-btn" disabled={m.used} onClick={() => onAction(m, "fields_ok")}>Looks right</button>
        <button className="ow-btn alt" disabled={m.used} onClick={() => onAction(m, "fields_edit")}>Edit</button>
      </div>
    </div>
  );
}

function ConfirmCard({ m, onAction }) {
  return (
    <div className="ow-card">
      <div>{m.text}</div>
      <div className="ow-actions">
        <button className="ow-btn" disabled={m.used} onClick={() => onAction(m, "submit_ok")}>Submit claim</button>
        <button className="ow-btn alt" disabled={m.used} onClick={() => onAction(m, "cancel")}>Cancel</button>
      </div>
    </div>
  );
}

/* approved = green pill, pending = yellow pill */
function ResultCard({ m }) {
  return (
    <div className="ow-card">
      <span className={"ow-pill " + m.status}>{m.title}</span>
      <div style={{ marginTop: 8 }}>{m.text}</div>
      {m.balance != null && (
        <div className="ow-muted" style={{ marginTop: 6 }}>Remaining balance: {money(m.balance)}</div>
      )}
      <div className="ow-muted" style={{ marginTop: 6 }}>Claim ID: {m.claimId}</div>
    </div>
  );
}

/* shown when the bill is more than the user's balance; the claim is not processed */
function LowBalanceCard({ m }) {
  return (
    <div className="ow-card">
      <span className="ow-pill rejected">Not enough balance</span>
      <div style={{ marginTop: 8 }}>
        This bill is more than your available balance, so we could not process this claim.
      </div>
      <div className="ow-row warn">
        <div className="k">Available balance</div>
        <div className="v">{money(m.balance)}</div>
      </div>
      <div className="ow-row">
        <div className="k">Bill amount</div>
        <div className="v">{money(m.amount)}</div>
      </div>
      <div className="ow-muted" style={{ marginTop: 6 }}>Claim ID: {m.claimId}</div>
    </div>
  );
}

/* NEW (HANDOFF): shown when the user types "talk to a human"; the button opens the form */
function HandoffCard({ m, onAction }) {
  return (
    <div className="ow-card">
      <div>{m.text}</div>
      <div className="ow-actions">
        <button className="ow-btn" onClick={() => onAction(m, "open_handoff")}>Contact support</button>
      </div>
    </div>
  );
}

function Message({ m, onAction }) {
  if (m.role === "me") return <div className="ow-msg me">{m.text}</div>;
  switch (m.type) {
    case "balance": return <BalanceCard m={m} />;
    case "history": return <HistoryCard m={m} />;
    case "extraction": return <ExtractionCard m={m} onAction={onAction} />;
    case "confirm": return <ConfirmCard m={m} onAction={onAction} />;
    case "result": return <ResultCard m={m} />;
    case "lowbalance": return <LowBalanceCard m={m} />;
    case "handoff": return <HandoffCard m={m} onAction={onAction} />;
    case "policy":
      return (
        <div className="ow-msg">
          {m.text}
          <div className="ow-cite">Source: {m.source}</div>
        </div>
      );
    default: return <div className="ow-msg">{m.text}</div>;
  }
}

/* ---------- NEW: login form (NEW (EMAIL): now asks for User ID and Email ID) ---------- */
function LoginForm({ onLogin }) {
  const [userId, setUserId] = useState("");
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (busy) return;
    const id = userId.trim();
    const mail = email.trim();
    if (!id) return setError("Enter your User ID to continue.");
    if (!/^[\w.@-]{2,40}$/.test(id)) return setError("Use 2 to 40 letters, numbers, or . _ - @ only.");
    if (!mail) return setError("Enter your Email ID to continue.");
    if (!EMAIL_RE.test(mail)) return setError("Enter a valid email address.");
    setBusy(true);
    const problem = await onLogin(id, mail);   // an error message, or null when logged in
    if (problem) { setError(problem); setBusy(false); }
  };

  return (
    <form className="ow-login" onSubmit={submit} noValidate>
      <h2>Log in to OPD Wallet</h2>
      <div className="ow-muted">Enter your User ID and Email ID to see your balance, claims, and chat history.</div>
      <input
        type="text"
        value={userId}
        autoFocus
        aria-label="User ID"
        autoComplete="username"
        placeholder="User ID (for example USER123)"
        onChange={(e) => { setUserId(e.target.value); setError(""); }}
      />
      <input
        type="email"
        value={email}
        aria-label="Email ID"
        autoComplete="email"
        placeholder="Email ID (for example you@example.com)"
        onChange={(e) => { setEmail(e.target.value); setError(""); }}
      />
      {error && <div className="ow-err" role="alert">{error}</div>}
      <button type="submit" className="ow-btn" disabled={busy}>{busy ? "Logging in..." : "Log in"}</button>
    </form>
  );
}

/* ---------- NEW (HANDOFF): the "Talk to a human" popup ---------- */
function HandoffModal({ onClose, onSubmit, initialEmail = "" }) {
  const [f, setF] = useState({ name: "", mobile: "", email: initialEmail, reason: "" }); // NEW (EMAIL): pre-filled from login
  const [err, setErr] = useState({});
  const [busy, setBusy] = useState(false);
  const [serverErr, setServerErr] = useState("");

  const set = (k) => (e) => { setF({ ...f, [k]: e.target.value }); setErr({ ...err, [k]: "" }); };

  const submit = async (e) => {
    e.preventDefault();
    if (busy) return;
    const x = {};
    if (f.name.trim().length < 2) x.name = "Enter your name.";
    if (f.mobile.replace(/\D/g, "").length < 10 || !/^\+?[\d\s\-()]{10,18}$/.test(f.mobile.trim())) x.mobile = "Enter a valid mobile number.";
    if (!EMAIL_RE.test(f.email.trim())) x.email = "Enter a valid email address.";
    if (f.reason.trim().length < 5) x.reason = "Tell us briefly what you need.";
    setErr(x);
    if (Object.keys(x).length) return;

    setBusy(true); setServerErr("");
    const out = await onSubmit({ name: f.name.trim(), mobile: f.mobile.trim(), email: f.email.trim(), reason: f.reason.trim() });
    setBusy(false);
    if (!out.ok) setServerErr(out.message);
  };

  return (
    <div className="ow-modal" role="dialog" aria-modal="true" aria-label="Talk to a human">
      <form className="ow-modal-card" onSubmit={submit} noValidate>
        <h2>Talk to a human</h2>
        <div className="ow-muted">All fields are required. We will share your last hour of chat and any bill you uploaded with our team.</div>

        <label>Full name
          <input type="text" value={f.name} onChange={set("name")} autoComplete="name" autoFocus />
          {err.name && <span className="ow-err" role="alert">{err.name}</span>}
        </label>
        <label>Mobile number
          <input type="tel" value={f.mobile} onChange={set("mobile")} autoComplete="tel" placeholder="+91 98765 43210" />
          {err.mobile && <span className="ow-err" role="alert">{err.mobile}</span>}
        </label>
        <label>Email ID
          <input type="email" value={f.email} onChange={set("email")} autoComplete="email" />
          {err.email && <span className="ow-err" role="alert">{err.email}</span>}
        </label>
        <label>Reason for enquiry
          <textarea rows={3} maxLength={1000} value={f.reason} onChange={set("reason")} />
          {err.reason && <span className="ow-err" role="alert">{err.reason}</span>}
        </label>

        {serverErr && <div className="ow-err" role="alert">{serverErr}</div>}
        <div className="ow-actions">
          <button type="submit" className="ow-btn" disabled={busy}>{busy ? "Sending..." : "Send to support"}</button>
          <button type="button" className="ow-btn alt" onClick={onClose} disabled={busy}>Close</button>
        </div>
      </form>
    </div>
  );
}

/* ---------- 4. THE WIDGET ---------- */
const CHIPS = ["Check my balance", "Is dental covered?", "Claim status"];

export default function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const [blurry, setBlurry] = useState(false);
  const [editing, setEditing] = useState(null);   // { claimId, cardId, fileName } while the user is changing bill details
  const [handoffOpen, setHandoffOpen] = useState(false);       // NEW (HANDOFF)
  const [userId, setUserId] = useState(getSavedUser);          // NEW
  const [userEmail, setUserEmail] = useState(getSavedEmail);   // NEW (EMAIL)
  const [messages, setMessages] = useState(() => {              // NEW: restore this user's cached chat
    const uid = getSavedUser();
    return (uid && loadHistory(uid)) || [WELCOME];
  });
  const endRef = useRef(null);
  const fileRef = useRef(null);
  const nextId = useRef(1);

  // keep ids unique after restoring saved messages
  useEffect(() => {
    const maxId = messages.reduce((mx, m) => Math.max(mx, m.id ?? 0), 0);
    if (maxId >= nextId.current) nextId.current = maxId + 1;
  }, [messages]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy, open]);

  // NEW: save this user's chat every time it changes (refreshes the 1 hour timer)
  useEffect(() => {
    if (userId) saveHistory(userId, messages);
  }, [messages, userId]);

  // NEW: login / logout (NEW (EMAIL): login now also stores the email)
  // Returns an error message (shown in the form) or null when the login worked
  const login = async (typedId, typedEmail) => {
    const out = await api.login(typedId, typedEmail);
    if (!out.ok) return out.message;
    const id = out.userId;        // the server's cleaned-up ID (upper case)
    const email = out.email;
    try {
      localStorage.setItem(USER_KEY, id);
      localStorage.setItem(EMAIL_KEY, email);
    } catch { /* storage blocked: the user stays logged in for this session only */ }
    setMessages(loadHistory(id) || [WELCOME]);   // same user within 1 hour gets the old chat back
    setUserEmail(email);
    setUserId(id);
    return null;
  };

  const logout = () => {
    try {
      localStorage.removeItem(USER_KEY);         // history stays cached for the 1 hour window
      localStorage.removeItem(EMAIL_KEY);
    } catch { /* ignore */ }
    setUserId(null);
    setUserEmail("");
    setEditing(null);
    setHandoffOpen(false);
    setMessages([WELCOME]);
    setInput("");
    setBusy(false);
  };

  // NEW: every message gets a timestamp (ts) so we can pick the last hour for the support ticket
  const push = (arr) =>
    setMessages((prev) => [...prev, ...arr.map((m) => ({ role: "bot", id: nextId.current++, ts: Date.now(), ...m }))]);

  const run = async (fn) => {
    setBusy(true);
    try { push(await fn()); }
    catch (e) {
      console.error(e);
      push([{ type: "text", text: "Something went wrong on our side. I've asked a support agent to follow up." }]);
    }
    finally { setBusy(false); }
  };

  const send = (text) => {
    const fromInput = text === undefined;   // typed by the user (chips and buttons pass their own text)
    const t = (text ?? input).trim();
    if (!t || busy || !userId) return;
    setInput("");
    setMessages((p) => [...p, { role: "me", ts: Date.now(), text: t }]);

    // While editing, what the user types is the change request for the bill
    if (editing && fromInput) {
      if (/^(cancel|stop|never ?mind|exit)$/i.test(t)) {
        setEditing(null);
        push([{ type: "text", text: "Okay, nothing was changed." }]);
        return;
      }
      const target = editing;
      run(async () => {
        const out = await api.editClaim(userId, target.claimId, t, target.fileName);
        if (out.applied) {
          setEditing(null);
          // the old card is replaced by the updated one
          setMessages((p) => p.map((x) => (x.id === target.cardId ? { ...x, used: true } : x)));
        }
        return out.messages;
      });
      return;
    }

    run(() => api.sendMessage(userId, t));
  };

  const onFile = (e) => {
    const f = e.target.files?.[0];
    if (!f || !userId) return;
    setBlurry(/blur/i.test(f.name));
    setEditing(null);
    setMessages((p) => [...p, { role: "me", ts: Date.now(), text: "Uploaded bill: " + f.name }]);
    run(() => api.uploadBill(userId, f));
    e.target.value = "";
  };

  const onAction = (m, action) => {
    if (action === "open_handoff") {          // NEW (HANDOFF)
      setHandoffOpen(true);
      return;
    }

    if (action === "fields_edit") {
      // the card stays active until a change is really applied
      setEditing({ claimId: m.claimId, cardId: m.id, fileName: m.fileName });
      push([{
        type: "text",
        text: "Tell me what to change, for example:\n• date is 11 Oct\n• amount is 750\n• provider is City Clinic\nYou can send more than one change in a message. Type cancel to stop editing.",
      }]);
      return;
    }

    setEditing(null);
    setMessages((p) => p.map((x) => (x.id === m.id ? { ...x, used: true } : x)));
    if (action === "fields_ok") run(() => api.checkPolicy({ reviewType: m.reviewType, claimId: m.claimId }));
    else if (action === "submit_ok") run(() => api.submitClaim(userId, { claimId: m.claimId }));
    else run(() => api.cancelClaim(userId, m.claimId));   // "cancel"
  };

  // NEW (HANDOFF): collect the last hour of chat + claim ids, send with the form
  const submitHandoff = async (form) => {
    const cutoff = Date.now() - HISTORY_TTL_MS;
    const recent = messages.filter((m) => (m.ts ?? 0) >= cutoff);
    const history = recent
      .map((m) => ({ role: m.role === "me" ? "user" : "bot", text: toPlain(m), ts: m.ts }))
      .filter((h) => h.text);
    const claimIds = [...new Set(recent.map((m) => m.claimId).filter(Boolean))];

    let out;
    try { out = await api.submitHandoff(userId, form, history, claimIds); }
    catch { out = { ok: false, message: "Could not reach support. Please try again." }; }

    if (out.ok) {
      setHandoffOpen(false);
      push([{
        type: "text",
        text: `Thank you. A support agent will contact you on ${form.email} or ${form.mobile}. Your ticket number is ${out.ticketId}.`,
      }]);
    }
    return out;
  };

  return (
    <div className="ow">
      <style>{css}</style>
      {open && (
        <section className="ow-panel" aria-label="OPD benefits chat">
          <div className="ow-head">
            <div>
              <b>OPD Wallet</b>
              <small>{userId ? `Signed in as ${userId}` : "Please log in"}</small>
              {userId && userEmail && <small>{userEmail}</small>}
            </div>
            {userId && (
              <div className="ow-head-btns">
                <button className="ow-human" onClick={() => setHandoffOpen(true)}>Talk to a human</button>
                <button className="ow-human" onClick={logout}>Log out</button>
              </div>
            )}
          </div>

          {!userId ? (
            <LoginForm onLogin={login} />
          ) : (
            <>
              <div className="ow-body" role="log" aria-live="polite">
                {messages.map((m, i) => <Message key={m.id ?? i} m={m} onAction={onAction} />)}
                {busy && <div className="ow-msg"><span className="ow-dots" aria-label="Assistant is typing"><i /><i /><i /></span></div>}
                <div ref={endRef} />
              </div>

              {messages.length < 3 && (
                <div className="ow-chips">
                  {CHIPS.map((c) => <button key={c} className="ow-chip" onClick={() => send(c)}>{c}</button>)}
                </div>
              )}

              <div className="ow-foot">
                <input ref={fileRef} type="file" accept="image/*,application/pdf" hidden onChange={onFile} />
                <button className="ow-ic" aria-label="Upload a bill" onClick={() => fileRef.current?.click()}>＋</button>
                <input type="text" value={input}
                  placeholder={editing ? "Type the change, e.g. amount is 750" : "Ask about benefits or claims"}
                  aria-label="Message" maxLength={1000} onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && send()} />
                <button className="ow-send" onClick={() => send()} disabled={busy}>Send</button>
              </div>
            </>
          )}

          {/* NEW (HANDOFF) */}
          {handoffOpen && userId && (
            <HandoffModal
              onClose={() => setHandoffOpen(false)}
              onSubmit={submitHandoff}
              initialEmail={userEmail}
            />
          )}
        </section>
      )}
      <button className="ow-launch" aria-label={open ? "Close chat" : "Open chat"} onClick={() => setOpen(!open)}>
        {open ? "×" : "💬"}
      </button>
    </div>
  );
}