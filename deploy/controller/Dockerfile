# Build from the repository root:
#   docker build -f deploy/controller/Dockerfile -t <your-registry>/<image>:<tag> .
# Push the resulting image to a registry that both AKS clusters can pull from.
# The repository intentionally does not provision a registry or embed registry
# credentials; choose one available to the active Azure subscription.
# Two stages: the build stage resolves and installs the locked dependency set;
# the runtime image receives only /app (sources and virtual environment), so
# it carries neither uv, its download cache, nor a package installer step.
FROM python:3.11-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lockfile changes,
# not on every source edit.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN pip install --no-cache-dir uv==0.13.7 \
    && uv sync --frozen --no-dev --no-install-project \
        --extra kubernetes \
        --extra storage \
        --extra agent \
        --extra observability

COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev \
        --extra kubernetes \
        --extra storage \
        --extra agent \
        --extra observability

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN addgroup --system --gid 10001 scheduler \
    && adduser --system --uid 10001 --ingroup scheduler scheduler \
    && mkdir -p /var/lib/carbon-scheduler \
    && chown scheduler:scheduler /app /var/lib/carbon-scheduler

COPY --from=build --chown=10001:10001 /app /app
# Job templates are reviewed source inputs for the Kubernetes executor.  Keep
# them in the image so a target YAML may use the repository-relative draft
# paths without requiring a writable host checkout inside the controller Pod.
COPY --chown=10001:10001 deploy/deathstarbench ./deploy/deathstarbench

USER 10001:10001

# The deployment can override this with CONTROLLER_COMMAND while the core
# workflow is being developed. This is deliberately not a web server command.
CMD ["python", "-m", "carbon_scheduler.controller"]
