# Warrant collector. Build from the repository root:
#   docker build -f deploy/Dockerfile -t warrantai/collector .
FROM python:3.12-slim
WORKDIR /app
COPY python/ /app/python/
RUN pip install --no-cache-dir "/app/python[collector]"
ENV WARRANT_STORE=postgresql://warrant:warrant@postgres:5432/warrant
EXPOSE 8787
USER 1000
ENTRYPOINT ["warrant", "collector", "--listen", "0.0.0.0:8787"]
