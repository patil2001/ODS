# ODS Voice Agent: Pipecat call loop over the ODS STT, LLM and Kokoro services.
FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY voice_agent/ voice_agent/
RUN chmod -R a+rX /app

USER 1000:1000

EXPOSE 8090

CMD ["python3", "-m", "voice_agent.server"]
