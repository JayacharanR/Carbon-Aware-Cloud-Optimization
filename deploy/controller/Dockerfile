# Build from the repository root:
#   docker build -f deploy/controller/Dockerfile -t <your-registry>/<image>:<tag> .
# Push the resulting image to a registry that both AKS clusters can pull from.
# The repository intentionally does not provision a registry or embed registry
# credentials; choose one available to the active Azure subscription.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY scripts ./scripts
# Job templates are reviewed source inputs for the Kubernetes executor.  Keep
# them in the image so a target YAML may use the repository-relative draft
# paths without requiring a writable host checkout inside the controller Pod.
COPY deploy/deathstarbench ./deploy/deathstarbench

RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev \
        --extra kubernetes \
        --extra storage \
        --extra agent \
        --extra observability \
    && addgroup --system --gid 10001 scheduler \
    && adduser --system --uid 10001 --ingroup scheduler scheduler \
    && mkdir -p /var/lib/carbon-scheduler \
    && chown -R scheduler:scheduler /app /var/lib/carbon-scheduler

USER 10001:10001

# The deployment can override this with CONTROLLER_COMMAND while the core
# workflow is being developed. This is deliberately not a web server command.
CMD ["python", "-m", "carbon_scheduler.controller"]
