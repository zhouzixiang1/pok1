FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN pip install --no-cache-dir anthropic openai

COPY . /app/pokerskill_agent/

ENTRYPOINT ["python", "-m", "pokerskill_agent.cli"]
