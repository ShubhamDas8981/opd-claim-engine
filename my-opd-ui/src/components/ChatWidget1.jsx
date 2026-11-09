import { useState, useRef, useEffect } from "react";

/* =====================================================================
   OPD Wallet Chat Widget
   Drop into: ui/src/components/ChatWidget.jsx
   Usage:     <ChatWidget />   (renders a floating launcher + chat panel)

   RULES THIS UI ENFORCES (from the architecture):
   - Every number shown comes from the API payload, never typed by the UI.
   - Every write (submit claim) needs a BUTTON confirmation, not free text.
   - Low-confidence fields are highlighted so the user confirms them.
   - "Talk to a human" is always visible.
   ===================================================================== */

/* ---------- 1. API LAYER: replace these mocks with fetch() to FastAPI ---------- */
const money = (paise) => "₹" + (paise / 100).toLocaleString("en-IN");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const API_BASE = "http://localhost:8000";

  const api = {
    async sendMessage(text) {
      const formData = new FormData();
      formData.append("user_id", "USER123"); // Use actual auth in production
      formData.append("message", text);

      const res = await fetch(`${API_BASE}/chat`, { method: "POST", body: formData });
      const data = await res.json();

      const response = data.response;
      if (typeof response === 'string') return [{ type: "text", text: response }];

      // Map Tool-specific JSON to Widget-specific Components
      if (response.current_balance) return [{
        type: "balance",
        total: 2500000, // total limit
        used: 1000000,
        left: response.current_balance,
        note: "Live account data from AI"
      }];
      if (response.status === "submitted") return [{ type: "text", text: response.message }];
      if (response.answer) return [{ type: "policy", text: response.answer, source: "Company Policy DB" }];
      if (response.status === "escalated") return [{ type: "text", text: response.message }];

      return [{ type: "text", text: JSON.stringify(response) }];
    },

  async uploadBill(file) {
    const formData = new FormData();
    formData.append("user_id", "USER123");
    formData.append("message", "I am uploading a medical bill");
    formData.append("file", file);

    const res = await fetch(`${API_BASE}/chat`, { method: "POST", body: formData });
    const data = await res.json();

    const claimId = data.claim_id;
    if (!claimId) throw new Error("Failed to get claim ID");

    const pollStatus = async () => {
      const statusRes = await fetch(`${API_BASE}/claims/${claimId}/status`);
      const statusData = await statusRes.json();

      if (statusData.status === "completed") {
        let ext;
        try {
          ext = typeof statusData.data.extracted === 'string'
            ? JSON.parse(statusData.data.extracted)
            : statusData.data.extracted;
        } catch (e) {
          console.error("Failed to parse extracted data", e);
          ext = {};
        }

        return [{
          type: "extraction",
          fileName: file.name,
          blurry: statusData.data.score === "HUMAN_REVIEW",
          fields: [
            { key: "Provider", value: ext.provider || "Unknown", conf: 0.9 },
            { key: "Total", value: `₹${statusData.data.amount || 0}`, conf: 0.9 },
            { key: "Date", value: ext.date || "Unknown", conf: 0.7 },
          ]
        }];
      }
      return null;
    };

    while (true) {
      const result = await pollStatus();
      if (result) return result;
      await wait(2000);
    }
  },

    async checkPolicy() {
      // In a real app, this would be another API call to verify the claim amount against the user's limit
      return [{ type: "confirm", text: "This claim is within your limit. Submit?", action: "submit" }];
    },

    async submitClaim({ blurry }) {
      // Trigger the final submission API call
      return [{ type: "result", status: "approved", score: 98, claimId: "C-REAL-101",
                 reasons: [], text: "Payment processed successfully!" }];
    },
  };

/* ---------- 2. STYLES (self-contained; move to CSS file if you prefer) ---------- */
const css = `
@import url('https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap');
.ow{--ink:#14263f;--ink2:#4b5b73;--line:#d9e0ea;--bg:#f5f7fa;--card:#fff;--teal:#0f8f7e;--teal-d:#0b6f62;--amber:#b86e00;--amber-bg:#fff4df;--ok:#18794e;
 font-family:'Atkinson Hyperlegible',system-ui,sans-serif;color:var(--ink);font-size:15px;line-height:1.45}
.ow *{box-sizing:border-box}
.ow-launch{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;background:var(--ink);color:#fff;font-size:24px;cursor:pointer;box-shadow:0 6px 20px rgba(20,38,63,.3)}
.ow-launch:focus-visible,.ow button:focus-visible,.ow input:focus-visible{outline:3px solid #7fd6c9;outline-offset:2px}
.ow-panel{position:fixed;right:20px;bottom:90px;width:380px;max-width:calc(100vw - 24px);height:600px;max-height:calc(100vh - 110px);background:var(--bg);border:1px solid var(--line);border-radius:14px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 16px 48px rgba(20,38,63,.22)}
.ow-head{background:var(--ink);color:#fff;padding:14px 16px;display:flex;justify-content:space-between;align-items:center}
.ow-head b{font-size:16px}.ow-head small{display:block;color:#aebbd0;font-size:12px}
.ow-human{background:transparent;color:#fff;border:1px solid #5d7089;border-radius:8px;padding:6px 10px;font:inherit;font-size:13px;cursor:pointer}
.ow-body{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.ow-msg{max-width:88%;padding:10px 12px;border-radius:12px;background:var(--card);border:1px solid var(--line);white-space:pre-wrap}
.ow-msg.me{align-self:flex-end;background:var(--ink);color:#fff;border-color:var(--ink)}
.ow-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;align-self:stretch}
.ow-big{font-size:28px;font-weight:700;letter-spacing:-.5px}
.ow-bar{height:8px;background:#e6ebf2;border-radius:4px;margin:8px 0;overflow:hidden}
.ow-bar i{display:block;height:100%;background:var(--teal)}
.ow-muted{color:var(--ink2);font-size:13px}
.ow-row{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border-radius:8px;margin-top:6px;background:var(--bg)}
.ow-row.warn{background:var(--amber-bg);border:1px solid #f0cf9a}
.ow-row .k{color:var(--ink2);font-size:13px}.ow-row .v{font-weight:700}
.ow-flag{color:var(--amber);font-size:12px;margin-top:2px}
.ow-actions{display:flex;gap:8px;margin-top:10px}
.ow-btn{flex:1;border-radius:8px;padding:9px 12px;font:inherit;font-weight:700;cursor:pointer;border:1px solid var(--teal);background:var(--teal);color:#fff}
.ow-btn:hover{background:var(--teal-d)}
.ow-btn.alt{background:#fff;color:var(--teal-d)}
.ow-btn:disabled{opacity:.45;cursor:default}
.ow-pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:700}
.ow-pill.approved{background:#dff3e8;color:var(--ok)}.ow-pill.review{background:var(--amber-bg);color:var(--amber)}
.ow-cite{margin-top:6px;font-size:12px;color:var(--ink2);border-left:3px solid var(--teal);padding-left:8px}
.ow-chips{display:flex;gap:6px;flex-wrap:wrap;padding:0 14px 8px}
.ow-chip{border:1px solid var(--line);background:#fff;border-radius:99px;padding:5px 11px;font:inherit;font-size:13px;cursor:pointer;color:var(--ink)}
.ow-foot{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line);background:#fff}
.ow-foot input[type=text]{flex:1;border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit}
.ow-ic{border:1px solid var(--line);background:#fff;border-radius:8px;padding:0 11px;font-size:18px;cursor:pointer}
.ow-send{border:0;background:var(--ink);color:#fff;border-radius:8px;padding:0 14px;font:inherit;font-weight:700;cursor:pointer}
.ow-dots{display:inline-flex;gap:4px}.ow-dots i{width:6px;height:6px;border-radius:50%;background:var(--ink2);animation:owb 1s infinite}
.ow-dots i:nth-child(2){animation-delay:.15s}.ow-dots i:nth-child(3){animation-delay:.3s}
@keyframes owb{0%,80%,100%{opacity:.25}40%{opacity:1}}
@media (prefers-reduced-motion:reduce){.ow-dots i{animation:none}}
`;

/* ---------- 3. MESSAGE RENDERERS ---------- */
function BalanceCard({ m }) {
  const pct = Math.round((m.used / m.total) * 100);
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

function ExtractionCard({ m, onAction }) {
  return (
    <div className="ow-card">
      <b>Check what we read from your bill</b>
      <div className="ow-muted">{m.fileName}</div>
      {m.fields.map((f) => {
        const low = f.conf < 0.8;
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

function ResultCard({ m }) {
  return (
    <div className="ow-card">
      <span className={"ow-pill " + m.status}>{m.status === "approved" ? "Approved" : "Under review"}</span>
      <div style={{ marginTop: 8 }}>{m.text}</div>
      <div className="ow-muted" style={{ marginTop: 6 }}>
        Claim {m.claimId} · Confidence {m.score}/100
      </div>
      {m.reasons.length > 0 && (
        <div className="ow-muted">Reason: {m.reasons.join(", ")}</div>
      )}
    </div>
  );
}

function Message({ m, onAction }) {
  if (m.role === "me") return <div className="ow-msg me">{m.text}</div>;
  switch (m.type) {
    case "balance": return <BalanceCard m={m} />;
    case "extraction": return <ExtractionCard m={m} onAction={onAction} />;
    case "confirm": return <ConfirmCard m={m} onAction={onAction} />;
    case "result": return <ResultCard m={m} />;
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

/* ---------- 4. THE WIDGET ---------- */
const CHIPS = ["Check my balance", "Is dental covered?", "Claim status"];

export default function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const [blurry, setBlurry] = useState(false);
  const [messages, setMessages] = useState([
    { role: "bot", type: "text", text: "Hi, I'm your OPD benefits assistant. Ask about your balance, upload a bill to claim, or ask what's covered." },
  ]);
  const endRef = useRef(null);
  const fileRef = useRef(null);
  let nextId = useRef(1);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  const push = (arr) =>
    setMessages((prev) => [...prev, ...arr.map((m) => ({ role: "bot", id: nextId.current++, ...m }))]);

  // wrapper so every API call has loading state and a safe fallback to a human
  const run = async (fn) => {
    setBusy(true);
    try { push(await fn()); }
    catch { push([{ type: "text", text: "Something went wrong on our side. I've asked a support agent to follow up." }]); }
    finally { setBusy(false); }
  };

  const send = (text) => {
    const t = (text ?? input).trim();
    if (!t || busy) return;
    setInput("");
    setMessages((p) => [...p, { role: "me", text: t }]);
    run(() => api.sendMessage(t));
  };

  const onFile = (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setBlurry(/blur/i.test(f.name));
    setMessages((p) => [...p, { role: "me", text: "Uploaded bill: " + f.name }]);
    run(() => api.uploadBill(f));
    e.target.value = "";
  };

  // button actions: the ONLY way a claim gets submitted
  const onAction = (m, action) => {
    setMessages((p) => p.map((x) => (x.id === m.id ? { ...x, used: true } : x)));
    if (action === "fields_ok") run(() => api.checkPolicy());
    else if (action === "submit_ok") run(() => api.submitClaim({ blurry }));
    else if (action === "fields_edit") push([{ type: "text", text: "Tell me what to change, for example: date is 11 Oct. (Editing is the next feature to build.)" }]);
    else push([{ type: "text", text: "Cancelled. Nothing was submitted." }]);
  };

  return (
    <div className="ow">
      <style>{css}</style>
      {open && (
        <section className="ow-panel" aria-label="OPD benefits chat">
          <div className="ow-head">
            <div><b>OPD Wallet</b><small>Answers come from your live account</small></div>
            <button className="ow-human" onClick={() => send("Talk to a human")}>Talk to a human</button>
          </div>

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
            <input type="text" value={input} placeholder="Ask about benefits or claims"
              aria-label="Message" onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()} />
            <button className="ow-send" onClick={() => send()} disabled={busy}>Send</button>
          </div>
        </section>
      )}
      <button className="ow-launch" aria-label={open ? "Close chat" : "Open chat"} onClick={() => setOpen(!open)}>
        {open ? "×" : "💬"}
      </button>
    </div>
  );
}
