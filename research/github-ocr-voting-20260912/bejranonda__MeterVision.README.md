# 👁️ MeterVision **Enterprise Edition**

**Multi-Tenant B2B SaaS Platform for AI-Powered Meter Reading**

[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://www.python.org/)
[![OCR Ensemble](https://img.shields.io/badge/OCR-Ensemble-green)](https://github.com/tesseract-ocr/tesseract)
[![Gemma AI](https://img.shields.io/badge/Gemma-3--27B%20%26%2012B-blue?logo=google)](https://ai.google.dev/gemma)
[![Modern UI](https://img.shields.io/badge/UI-Modern%20Glass-emerald)](https://metervision.enterprise)

MeterVision is a comprehensive enterprise platform for offering "Meter Reading as a Service" to multiple organizations. It combines hierarchical asset management, AI-powered OCR, multi-tenancy, role-based access control, and camera-based installation workflows.

---

## 🎨 Modernized UI/UX
The Enterprise Edition features a completely redesigned user interface:
- **Dashboard**: Real-time summary statistics and active alert monitoring.
- **Glassmorphism Design**: Sleek, modern emerald-and-white theme.
- **Data Visualization**: Interactive historic reading charts powered by Chart.js.
- **Mobile First**: Fully responsive layouts optimized for field installers and office managers.

---

## 📖 Key Documentation
For a deep dive into how MeterVision works, see our detailed guides:
- [**Core Concepts**](./docs/CONCEPTS.md) - Vision, Multi-tenancy, and AI Ensemble explanation.
- [**Technical Architecture**](./docs/ARCHITECTURE.md) - Backend structure, DB schema, and AI voting logic.

---

## 🌟 Enterprise Features

### 🏢 Multi-Tenant Architecture
- **Organization Management**: Support multiple customer organizations with complete data isolation.
- **Role-Based Access Control (RBAC)**: 5 distinct user roles with granular permissions.
- **Flexible User Assignment**: Users can belong to multiple organizations with different roles.

### 👥 User Roles
1.  **Super Admin** - Full system access, create organizations, view system logs.
2.  **Platform Manager** - Manage installers globally.
3.  **Org Manager** - Manage specific organization, assign users.
4.  **Org Viewer** - Read-only access to organization data.
5.  **Installer** - Install and validate meters in assigned organizations.

### 📦 Hierarchical Asset Management
Organize your infrastructure with a six-layer hierarchy:
- **Organization** (Tenant)
  - **Project** (Container)
    - **Customer** (Client)
      - **Building** (Physical Structure)
        - **Place** (Location)
          - **Meter** (Device)

### 🤖 Intelligent OCR Ensemble
MeterVision uses a sophisticated **voting ensemble** to ensure maximum accuracy:
1.  **Google Gemma 3 (27B & 12B)**: State-of-the-art vision models.
2.  **Qwen 2.5 VL**: Highly accurate visual-language model.
3.  **EasyOCR & Tesseract**: Specialized OCR engines for corroboration.
*Includes a calibration system using expected values for improved confidence.*

---

## 🛠️ Tech Stack

- **Backend**: FastAPI, SQLModel (SQLAlchemy + Pydantic)
- **Database**: SQLite (dev), PostgreSQL (production recommended)
- **OCR Engine**: Tesseract, EasyOCR, Gemma 3, Qwen 2.5 VL
- **Frontend**: Vanilla JavaScript + Modern CSS (No heavy frameworks)
- **Visualization**: Chart.js
- **Icons**: Phosphor Icons

---

## 🏗️ System Architecture

MeterVision runs as a coordinated stack of three primary services:

1.  **FastAPI Backend** (`metervision.service`): The core API, RBAC engine, and dashboard server.
2.  **MQTT Listener** (`metervision-mqtt.service`): An asynchronous gateway that captures snapshots and telemetry from IoT cameras.
3.  **MQTT Broker** (Docker): A `mosquitto` instance that facilitates communication between cameras and the listener.

---

## ⚙️ Service Management

The application is managed via `systemd` and `docker-compose`.

### Backend & Listener (Systemd)
```bash
# Check status of all services
systemctl status metervision metervision-mqtt

# View live logs for the MQTT gateway
journalctl -u metervision-mqtt -f

# Restart services
sudo systemctl restart metervision metervision-mqtt
```

### MQTT Broker (Docker)
```bash
# Start the broker
docker-compose up -d mqtt

# Check broker logs
docker-compose logs -f mqtt
```

---

## ⚡ Quick Start

### 1. Initial Setup
```bash
chmod +x setup.sh
./setup.sh
```

### 2. Deploy the Service
```bash
chmod +x deploy.sh
./deploy.sh
```

### 3. Verification
```bash
python verify_setup.py         # Verify DB schema and OCR pipeline
python verify_installation.py  # Simulate end-to-end installation workflow
```

---

## 🔐 Security & Multi-Tenancy
- **Data Isolation**: All entities include `organization_id` foreign key.
- **Permission Enforcement**: Route-level checks via `PermissionChecker` middleware.
- **Scoped Responses**: Implicit organization filtering on all queries.

---

## 🚀 Roadmap

### ✅ Completed
- **Phase 1-4**: Database, RBAC, Installer Frontend, and Validation Backend.
- **Phase 5**: UI/UX Optimization (Modernized Dashboard & Mobile Installer).
- **Phase 6**: Branding Integration (Integrated Smartrplace logo).
- **Phase 7**: Comprehensive Concept & Architecture Documentation.

### 📅 Upcoming
- **Native Mobile Apps**: Dedicated iOS/Android installer tools.
- **Advanced Analytics**: Predictive usage patterns and leak detection.
- **Cloud Scale**: Support for geo-distributed deployments.

---

## ⚖️ License
This project is licensed under the MIT License - see the LICENSE file for details.