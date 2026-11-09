"""All guardrail limits in one place. Change numbers here, not in the code."""
# ---- chat input
MAX_MESSAGE_CHARS = 500
INJECTION_BLOCK_SCORE = 3          # sum of pattern weights needed to block a chat message
BILL_INJECTION_FLAG_SCORE = 2      # lower bar for bill text: a bill should never talk to an AI

# ---- uploads
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".png", ".jpg", ".jpeg"}
MAX_PDF_PAGES = 5
MAX_IMAGE_PIXELS = 25_000_000
MAX_TEXT_CHARS = 20_000            # bill text sent to the LLM is cut to this

# ---- rate limits: (max calls, window seconds)
RATE_LIMITS = {
    "chat": (20, 60),
    "upload": (5, 600),
    "handoff": (3, 3600),
    "claim_action": (30, 60),
    "ip": (120, 60),                # all requests from one network address
}

# ---- business policy (checked when the user presses Submit)
DUPLICATE_WINDOW_DAYS = 180
MAX_CLAIMS_PER_DAY = 5
AUTO_APPROVE_MONTHLY_CAP = 5000    # total rupees that may be AUTO-approved per user in 30 days

# ---- LLM output validation
MAX_REASKS = 2
LLM_TEMPERATURE = 0
ENABLE_HUB_VALIDATORS = False      # True only after: uv add guardrails-ai-detect-jailbreak guardrails-ai-toxic-language (see GUARDRAILS.md)

# ---- logging
AUDIT_LOG_PATH = "logs/guardrails.jsonl"
