FROM python:3.11-slim

# Hugging Face Spaces containers run as a non-root user by convention.
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
# --no-deps: see requirements.txt's comment above -- mdwarf-contin's own
# pinned numpy<2.0 conflicts with astropy/healpy's numpy>=2.0 above; its
# real runtime deps are already installed via requirements.txt instead.
RUN pip install --no-cache-dir --user --no-deps \
    "mdwarf-contin @ https://github.com/imedan/mdwarf_contin/archive/refs/heads/main.tar.gz"

COPY --chown=user ingest/ ingest/
COPY --chown=user sync/ sync/
COPY --chown=user webapp/ webapp/

ENV PYTHONUNBUFFERED=1
EXPOSE 7860

CMD ["python3", "-m", "webapp.app"]
