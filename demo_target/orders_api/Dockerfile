FROM python:3.11-slim
WORKDIR /app
COPY common /app/common
COPY faults /app/faults
COPY orders_api /app/orders_api
RUN pip install --no-cache-dir fastapi uvicorn[standard] prometheus-client sentry-sdk[fastapi] httpx pydantic
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "orders_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
