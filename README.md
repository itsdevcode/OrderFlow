# OrderFlow API

> **OrderFlow** is a production-grade Order & Inventory Management API built with **FastAPI**, **SQLAlchemy (Async)**, **Alembic**, and **Pydantic v2**.

---

## 🚀 Features

- **Asynchronous Architecture**: Full async database operations with SQLAlchemy 2.0 and `asyncpg` / `aiosqlite`.
- **Clean Layered Architecture**: Strictly separated layers for **API Routers**, **Services**, **Repositories**, **Models**, **Schemas**, and **Dependencies**.
- **User & Role Management**: RBAC roles (`ADMIN`, `MANAGER`, `CUSTOMER`) with password hashing using `pwdlib`.
- **Database Migrations**: Database schema management with **Alembic**.
- **Database Seeding**: Automated script to seed initial/default roles and reference data.
- **Health Checks**: Endpoints for basic service health and real database connectivity validation.
- **Type Safety**: Strictly typed Python codebase tuned for **Pyright**.

---

## 🛠️ Tech Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/)
- **ORM / Database**: [SQLAlchemy 2.0 (Async)](https://www.sqlalchemy.org/)
- **Migrations**: [Alembic](https://alembic.sqlalchemy.org/)
- **Validation & Settings**: [Pydantic v2](https://docs.pydantic.dev/) & `pydantic-settings`
- **Security**: `pwdlib` for password hashing
- **Python**: 3.11+

---

## 📁 Project Structure

```text
OrderFlow/
├── alembic/              # Database migration scripts
│   ├── versions/         # Migration versions
│   └── env.py            # Alembic async environment setup
├── app/
│   ├── api/              # API layer (FastAPI routers & endpoints)
│   │   └── v1/           # Version 1 endpoints (e.g., /users)
│   ├── core/             # Application configurations, security & constants
│   ├── db/               # Database session & initial seeding logic
│   ├── dependencies/     # FastAPI Dependency Injection (DB session, etc.)
│   ├── exceptions/       # Domain-specific custom exceptions
│   ├── models/           # SQLAlchemy ORM models
│   ├── repositories/     # Data access layer (Repository pattern)
│   ├── schemas/          # Pydantic data schemas (Request/Response validation)
│   ├── scripts/          # CLI scripts (e.g., seeding roles)
│   ├── services/         # Business logic layer
│   └── main.py           # Application entrypoint
├── .env                  # Environment configuration file
├── alembic.ini           # Alembic configuration file
├── pyproject.toml        # Project metadata and dependencies
└── README.md             # Project documentation
```

---

## ⚙️ Getting Started

### 1. Prerequisites

- **Python 3.11+** installed.
- **PostgreSQL** or **SQLite** (Async supported).

### 2. Environment Setup

Clone the repository and set up a virtual environment:

```bash
# Clone the repository
git clone https://github.com/your-username/OrderFlow.git
cd OrderFlow

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt # or pip install -e .
```

Create a `.env` file in the root directory:

```env
APP_NAME="OrderFlow API"
APP_VERSION="1.0.0"
DEBUG=True
DATABASE_URL="sqlite+aiosqlite:///./orderflow.db"
# For PostgreSQL:
# DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/orderflow"
```

---

## 🗄️ Database Setup & Migrations

### Run Migrations

To apply database migrations:

```bash
alembic upgrade head
```

### Seed Default Roles

Seed the database with initial roles (`ADMIN`, `MANAGER`, `CUSTOMER`):

```bash
python -m app.scripts.seed
```

---

## 🏃 Running the Application

Start the development server using **Uvicorn**:

```bash
uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

---

## 📖 API Documentation

Once the server is running, you can access the interactive API docs:

- **Swagger UI**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc**: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

### Summary of Available Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Basic application health check |
| `GET` | `/health/db` | Database connection health check |
| `POST` | `/api/v1/users` | Register a new user |

---

## 🧪 Development & Quality

### Code Type Checking

Check type annotations using **Pyright**:

```bash
pyright
```

---

## 📜 License

This project is licensed under the MIT License.
