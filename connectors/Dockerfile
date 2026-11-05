FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VEROMESH_API=http://veromesh-service:8700
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10003 connect
USER connect
WORKDIR /work
ENTRYPOINT ["veromesh-connect"]
