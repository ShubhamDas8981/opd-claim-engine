import React, { useEffect, useRef, useState } from "react";

const API_BASE = (import.meta.env?.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const USER_ID = import.meta.env?.VITE_OPD_USER_ID || "USER123";
const POLL_MS = 5000;
const MAX_UPLOAD_POLLS = 120;

const money = (rupees) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(Number(rupees) || 0);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `HTTP ${response.status}`);
  }
  return payload;
}

function parseObject(value) {
  if (!value) return {};
  if (typeof value === "object" && !Array.isArray(value)) return value;
  if (typeof value !== "string") return {};

  let source = value.trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/i, "");

  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const parsed = JSON.parse(source);
      if (typeof parsed === "string") {
        source = parsed;
        continue;
      }
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }
  return {};
}

function numberAmount(value) {
  if (value && typeof value === "object") {
    value = value.amount ?? value.total ?? value.grand_total ?? 0;
  }
  if (typeof value === "string") {
    const parsed = parseObject(value);
    if (Object.keys(parsed).length) {
      value = parsed.amount ?? parsed.total ?? 0;
    } else {
      value = value.replace(/[₹,\s]/g, "");
    }
  }
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 ? amount : 0;
}

function unwrapStatus(payload) {
  const data = payload?.data && typeof payload.data === "object"
    ? payload.data
    : payload || {};
  // The status endpoint may use top-level `status: completed` for extraction
  // processing. Do not mistake that for the admin's claim decision.
  const candidate =
    data.claim_status ??
    data.approval_status ??
    payload?.claim_status ??
    (data.status && ["PENDING", "APPROVED", "REJECTED", "UNDER_REVIEW"].includes(String(data.status).toUpperCase())
      ? data.status
      : "PENDING");
  const status = String(candidate).toUpperCase();

  return {
    data,
    status: status === "HUMAN_REVIEW" ? "PENDING" : status,
  };
}

const api = {
  async sendMessage(message) {
    const form = new FormData();
    form.append("user_id", USER_ID);
    form.append("message", message);

    const payload = await requestJson(`${API_BASE}/chat`, { method: "POST", body: form });
    const response = payload.response ?? payload;

    if (typeof response === "string") return [{ type: "text", text: response }];

    if (
      response.current_balance !== undefined ||
      response.balance_left !== undefined ||
      response.total_limit !== undefined
    ) {
      const left = Number(response.current_balance ?? response.balance_left ?? 0);
      const totalValue = response.total_limit ?? response.total ?? response.wallet_limit;
      const total = totalValue == null ? null : Number(totalValue);
      const usedValue = response.used_amount ?? response.used;
      const used = usedValue == null
        ? (total == null ? null : Math.max(0, total - left))
        : Number(usedValue);

      return [{
        type: "balance",
        left,
        total: Number.isFinite(total) ? total : null,
        used: Number.isFinite(used) ? used : null,
        note: response.note || "Live account data",
      }];
    }

    if (response.claims_history) {
      return [{
        type: "history",
        claims: response.claims_history,
        text: response.message || "Your recent claims history",
      }];
    }

    if (response.answer) {
      return [{ type: "policy", text: response.answer, source: response.source || "Company Policy DB" }];
    }

    if (response.message || response.status) {
      return [{ type: "text", text: response.message || `Request status: ${response.status}` }];
    }

    return [{ type: "text", text: JSON.stringify(response) }];
  },

  async getClaimStatus(claimId) {
    return requestJson(`${API_BASE}/claims/${encodeURIComponent(claimId)}/status`);
  },

  async uploadBill(file) {
    const form = new FormData();
    form.append("user_id", USER_ID);
    form.append("message", "I am uploading a medical bill");
    form.append("file", file);

    const uploadPayload = await requestJson(`${API_BASE}/chat`, { method: "POST", body: form });
    const claimId = uploadPayload.claim_id ?? uploadPayload.data?.claim_id ?? uploadPayload.response?.claim_id;

    if (!claimId) {
      throw new Error("The upload API did not return a claim_id. Check the /chat upload response.");
    }

    for (let attempt = 0; attempt < MAX_UPLOAD_POLLS; attempt += 1) {
      const payload = await api.getClaimStatus(claimId);
      const { data, status } = unwrapStatus(payload);
      const processing = String(data.processing_status ?? payload.processing_status ?? payload.status ?? "").toLowerCase();

      if (["failed", "error"].includes(processing)) {
        throw new Error(data.error || data.message || "Bill extraction failed.");
      }

      const extracted = parseObject(
        data.extracted_data ?? data.extracted ?? data.extracted_json ?? data.extraction
      );

      const hasExtraction =
        Object.keys(extracted).length > 0 ||
        data.amount !== undefined ||
        data.provider !== undefined;

      if (hasExtraction && !["queued", "processing", "started"].includes(processing)) {
        const amount = numberAmount(data.amount ?? extracted.amount ?? extracted.total);
        return [{
          type: "extraction",
          claimId,
          fileName: file.name,
          claimStatus: status,
          fields: [
            { key: "Provider", value: data.provider ?? extracted.provider ?? extracted.provider_name ?? "Unknown", conf: 0.95 },
            { key: "Total", value: money(amount), conf: amount > 0 ? 0.95 : 0.3 },
            { key: "Date", value: data.date ?? extracted.date ?? extracted.bill_date ?? "Unknown", conf: 0.75 },
          ],
        }];
      }

      await sleep(POLL_MS);
    }

    throw new Error("Bill extraction is still processing. Please check the claim status again shortly.");
  },
};

const styles = {
  root: { fontFamily: "system-ui, sans-serif", color: "#14263f" },
  launcher: { position: "fixed", right: 20, bottom: 20, width: 56, height: 56, borderRadius: "50%", border: 0, background: "#14263f", color: "#fff", fontSize: 24, cursor: "pointer", zIndex: 10000 },
  panel: { position: "fixed", right: 20, bottom: 90, width: 460, maxWidth: "calc(100vw - 24px)", height: 650, maxHeight: "calc(100vh - 110px)", background: "#f5f7fa", border: "1px solid #d9e0ea", borderRadius: 14, display: "flex", flexDirection: "column", overflow: "hidden", boxShadow: "0 16px 48px #14263f38", zIndex: 9999 },
  header: { background: "#14263f", color: "#fff", padding: 14, display: "flex", justifyContent: "space-between", alignItems: "center" },
  body: { flex: 1, overflowY: "auto", padding: 14, display: "flex", flexDirection: "column", gap: 10 },
  card: { background: "#fff", border: "1px solid #d9e0ea", borderRadius: 12, padding: 12 },
  row: { display: "flex", justifyContent: "space-between", gap: 12, padding: 9, marginTop: 6, background: "#f5f7fa", borderRadius: 8 },
  footer: { display: "flex", gap: 8, padding: 10, borderTop: "1px solid #d9e0ea", background: "#fff" },
  button: { border: 0, borderRadius: 8, padding: "9px 12px", background: "#14263f", color: "#fff", cursor: "pointer" },
};

function BalanceCard({ message }) {
  const hasTotal = message.total !== null && message.used !== null && message.total > 0;
  const percentage = hasTotal
    ? Math.min(100, Math.max(0, (message.used / message.total) * 100))
    : 0;

  return (
    <div style={styles.card}>
      <div style={{ color: "#4b5b73", fontSize: 13 }}>OPD balance left</div>
      <div style={{ fontSize: 28, fontWeight: 750 }}>{money(message.left)}</div>
      {hasTotal && (
        <>
          <div style={{ height: 8, background: "#e6ebf2", borderRadius: 5, margin: "9px 0", overflow: "hidden" }}>
            <div style={{ width: `${percentage}%`, height: "100%", background: "#0f8f7e" }} />
          </div>
          <div style={{ color: "#4b5b73", fontSize: 13 }}>
            {money(message.used)} used of {money(message.total)}
          </div>
        </>
      )}
      <div style={{ color: "#4b5b73", fontSize: 12 }}>{message.note}</div>
    </div>
  );
}

function Message({ item, onRefresh }) {
  if (item.role === "me") {
    return <div style={{ ...styles.card, alignSelf: "flex-end", background: "#14263f", color: "#fff", maxWidth: "88%" }}>{item.text}</div>;
  }

  if (item.type === "balance") return <BalanceCard message={item} />;

  if (item.type === "extraction") {
    return (
      <div style={styles.card}>
        <b>Check what we read from your bill</b>
        <div style={{ color: "#4b5b73", fontSize: 12 }}>{item.fileName} · Claim #{item.claimId}</div>
        {item.fields.map((field) => (
          <div style={styles.row} key={field.key}>
            <span style={{ color: "#4b5b73", fontSize: 13 }}>{field.key}</span>
            <b style={{ textAlign: "right", overflowWrap: "anywhere" }}>{String(field.value)}</b>
          </div>
        ))}
        <div style={{ marginTop: 10, fontSize: 13 }}>
          Claim status: <b>{item.claimStatus}</b>
          <button style={{ ...styles.button, display: "block", marginTop: 8 }} onClick={() => onRefresh(item.claimId)}>
            Refresh status
          </button>
        </div>
      </div>
    );
  }

  if (item.type === "history") {
    return (
      <div style={styles.card}>
        <b>{item.text}</b>
        {(item.claims || []).map((claim) => (
          <div style={styles.row} key={claim.claim_id}>
            <span>#{claim.claim_id} · {claim.date}</span>
            <b>{money(claim.amount)} · {claim.status}</b>
          </div>
        ))}
      </div>
    );
  }

  return <div style={{ ...styles.card, maxWidth: "90%", whiteSpace: "pre-wrap" }}>{item.text}</div>;
}

export default function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState([{
    id: "welcome",
    role: "bot",
    type: "text",
    text: "Hi, I'm your OPD benefits assistant. Ask about your balance, upload a bill, or ask what's covered.",
  }]);
  const [error, setError] = useState("");
  const fileInput = useRef(null);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  // Continue checking the backend after extraction so admin decisions appear in the user UI.
  useEffect(() => {
    const claimIds = [...new Set(
      messages.filter((m) => m.type === "extraction").map((m) => m.claimId).filter(Boolean)
    )];
    if (!claimIds.length) return undefined;

    let cancelled = false;
    const timer = setInterval(async () => {
      for (const claimId of claimIds) {
        try {
          const payload = await api.getClaimStatus(claimId);
          const { data, status } = unwrapStatus(payload);
          if (!cancelled) {
            setMessages((current) => current.map((m) =>
              m.type === "extraction" && String(m.claimId) === String(claimId)
                ? { ...m, claimStatus: status, adminRemarks: data.admin_remarks ?? m.adminRemarks }
                : m
            ));
          }
        } catch (pollError) {
          console.warn("Claim status refresh failed:", pollError);
        }
      }
    }, POLL_MS);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [messages.filter((m) => m.type === "extraction").map((m) => m.claimId).join(",")]);

  const appendBot = (items) => {
    setMessages((previous) => [
      ...previous,
      ...items.map((item, index) => ({ ...item, id: `${Date.now()}-${index}-${Math.random()}`, role: "bot" })),
    ]);
  };

  const send = async (text = input) => {
    const value = text.trim();
    if (!value || busy) return;
    setInput("");
    setError("");
    setMessages((previous) => [...previous, { role: "me", text: value, id: `${Date.now()}-user` }]);
    setBusy(true);
    try {
      appendBot(await api.sendMessage(value));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const upload = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || busy) return;
    setError("");
    setMessages((previous) => [...previous, { role: "me", text: `Uploaded bill: ${file.name}`, id: `${Date.now()}-file` }]);
    setBusy(true);
    try {
      appendBot(await api.uploadBill(file));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const refreshClaim = async (claimId) => {
    try {
      const payload = await api.getClaimStatus(claimId);
      const { data, status } = unwrapStatus(payload);
      setMessages((previous) => previous.map((m) =>
        m.type === "extraction" && String(m.claimId) === String(claimId)
          ? { ...m, claimStatus: status, adminRemarks: data.admin_remarks ?? m.adminRemarks }
          : m
      ));
    } catch (err) {
      setError(err.message);
    }
  };

  const quickPrompts = ["Check my balance", "Is dental covered?", "Claim status"];

  return (
    <div style={styles.root}>
      {open && (
        <section style={styles.panel} aria-label="OPD benefits chat">
          <header style={styles.header}>
            <div><b>OPD Wallet</b><div style={{ color: "#aebbd0", fontSize: 12 }}>Answers come from your live account</div></div>
            <button style={{ ...styles.button, background: "transparent", border: "1px solid #718198" }} onClick={() => send("Talk to a human")}>Talk to a human</button>
          </header>

          <div style={styles.body} role="log" aria-live="polite">
            {messages.map((item, index) => <Message key={item.id || index} item={item} onRefresh={refreshClaim} />)}
            {busy && <div style={styles.card}>Please wait…</div>}
            {error && <div role="alert" style={{ ...styles.card, color: "#b42318" }}>{error}</div>}
            <div ref={endRef} />
          </div>

          {messages.length < 3 && (
            <div style={{ display: "flex", gap: 6, padding: "0 12px 8px", flexWrap: "wrap" }}>
              {quickPrompts.map((prompt) => (
                <button key={prompt} style={{ ...styles.button, background: "#fff", color: "#14263f", border: "1px solid #d9e0ea" }} onClick={() => send(prompt)}>{prompt}</button>
              ))}
            </div>
          )}

          <footer style={styles.footer}>
            <input ref={fileInput} type="file" accept="application/pdf,image/*" hidden onChange={upload} />
            <button style={{ ...styles.button, background: "#fff", color: "#14263f", border: "1px solid #d9e0ea" }} aria-label="Upload bill" onClick={() => fileInput.current?.click()}>＋</button>
            <input
              style={{ flex: 1, minWidth: 0, border: "1px solid #d9e0ea", borderRadius: 8, padding: 9, font: "inherit" }}
              value={input}
              placeholder="Ask about benefits or claims"
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && send()}
            />
            <button style={styles.button} disabled={busy} onClick={() => send()}>Send</button>
          </footer>
        </section>
      )}
      <button style={styles.launcher} aria-label={open ? "Close chat" : "Open chat"} onClick={() => setOpen((value) => !value)}>
        {open ? "×" : "💬"}
      </button>
    </div>
  );
}
