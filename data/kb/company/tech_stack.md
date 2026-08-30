---
type: company
visibility: candidate
title: Engineering Tech Stack
---

## Languages
Backend services are written primarily in Python (FastAPI) and Go for latency-sensitive paths.
Data tooling is Python. Frontend is TypeScript with React.

## Data Stores
PostgreSQL is the system of record. We use Redis for caching and rate limiting in production
services, and ClickHouse for analytics. Object storage is on S3-compatible buckets.

## Infrastructure
Services run on Kubernetes (EKS). CI/CD is GitHub Actions to Argo CD. Observability is
Prometheus, Grafana, and OpenTelemetry traces. Infrastructure is managed with Terraform.

## Messaging and Async
Kafka handles the event backbone between services. Background jobs run on a Celery-plus-Redis
setup for Python services and native workers for Go services.

## How We Ship
Trunk-based development, feature flags for risky changes, and progressive rollouts. Every service
owns its on-call rotation.
