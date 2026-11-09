# Enterprise AI OPD Digital Wallet - Setup & Run Guide

## Project Structure
- `app/`: Core backend application.
  - `api/`: API endpoints.
  - `agents/`: Agent orchestrator and tools.
  - `core/`: Configuration, Cache, Kafka clients.
  - `models/`: SQLAlchemy database models.
  - `services/`: RAG, OCR, and external API services.
  - `worker/`: Kafka consumers for async processing.
- `admin/`: Human-in-the-loop dashboard.
- `scripts/`: Setup and utility scripts.
- `tests/`: Unit and integration tests.

## Prerequisites
- Python 3.10+
- [uv](https://github.com/astral-sh/uv)
- Docker & Docker Compose
- Groq API Key
- HubSpot API Key (for escalation)

## Step-by-Step Execution

### 1. Infrastructure Setup
Start all required services (MySQL, Redis, Kafka, Prometheus, Grafana) using Docker:
```bash
docker-compose up -d
```

### 2. Environment Configuration
Create a `.env` file in the root directory and add your keys:
```env
GROQ_API_KEY=your_groq_api_key_here
HUBSPOT_API_KEY=your_hubspot_api_key_here
MYSQL_URL=mysql+pymysql://root:root_password@localhost:3306/opd_wallet
```

### 3. Install Dependencies
The project uses `uv` for dependency management. Install everything with:
```bash
uv sync
```

### 4. Run the Application
**Start the FastAPI Server:**
```bash
uv run python app/main.py
```

**Start the Claim Processing Worker (in a separate terminal):**
```bash
uv run python app/worker/claims_worker.py
```

## API Usage
**Chat Endpoint:** `POST /chat`
- **Payload (Form Data):**
  - `user_id`: "USER123"
  - `message`: "What is my current balance?" or "I want to upload a medical bill"
  - `file`: (Optional) Upload bill image/pdf

## Monitoring
- **Prometheus:** `http://localhost:9090`
- **Grafana:** `http://localhost:3000`
- **Admin Panel:** (Placeholder implementation in `admin/` folder)
