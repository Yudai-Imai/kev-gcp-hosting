FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    TOKENIZERS_PARALLELISM=false \
    PORT=8080 \
    KEV_MODEL=jaredpalmer/kev-4b

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV VIRTUAL_ENV=/opt/venv
RUN uv venv $VIRTUAL_ENV --python 3.13
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# PyTorch (CUDA 12.4)
RUN uv pip install torch --index-url https://download.pytorch.org/whl/cu124

# kev 本体は upstream を取得して、非同期初期化のパッチを当てる。
# 再現性のため、実際にはコミットを固定すること(--build-arg KEV_REF=<commit>)。
ARG KEV_REPO_URL=https://github.com/jaredpalmer/kev.git
ARG KEV_REF=main
RUN git clone "${KEV_REPO_URL}" /app && git -C /app checkout "${KEV_REF}"
WORKDIR /app
COPY patches/async-init.patch /tmp/async-init.patch
RUN git apply /tmp/async-init.patch
RUN uv pip install -e ".[serve]"

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
