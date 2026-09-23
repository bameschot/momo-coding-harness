FROM python:3.12 AS builder
ARG VERSION=1
ENV APP_HOME=/app
RUN pip install build
FROM python:3.12-slim
COPY --from=builder /app /app
