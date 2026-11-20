#!/usr/bin/env bash
# ==============================================================================
# Kev-4B GCE スタートアップスクリプト (N1 + Tesla T4 Spot VM)
#
# 初回ブート: ドライバ・venv・コード・重みを入れて kev-serve を起動し、/ready 確認後に完了マーカーを作る。
# 2回目以降: VM は停止→起動で再利用されるため何もしない(kev-serve は systemd の enable で自動起動する)。
#           外部IP除去後(NATなし)の再起動でも、ネットワークアクセスなしで起動できる。
# ==============================================================================
set -euo pipefail

LOG_FILE="/var/log/kev-startup.log"
exec > >(tee -a "${LOG_FILE}") 2>&1

INSTALL_DIR="/opt/kev"
SETUP_MARKER="${INSTALL_DIR}/.setup-done"

if [ -f "${SETUP_MARKER}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') セットアップ済みのため何もしません (kev-serve は systemd が起動します)"
    exit 0
fi

echo "=================================================================="
echo " Starting Kev-4B VM Initialization: $(date '+%Y-%m-%d %H:%M:%S')"
echo " Machine Architecture: $(uname -m), OS: $(lsb_release -ds 2>/dev/null || grep PRETTY_NAME /etc/os-release)"
echo "=================================================================="

export DEBIAN_FRONTEND=noninteractive
SERVICE_USER="kev"
VENV_DIR="${INSTALL_DIR}/.venv"
export HF_HOME="${INSTALL_DIR}/.cache/huggingface"

# インスタンスメタデータ属性を読む (provision.sh が設定する)
meta() {
    curl -sf -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" || true
}
REPO_URL="$(meta kev-repo-url)"
REPO_BRANCH="$(meta kev-repo-branch)"
KEV_API_KEY="$(meta kev-api-key)"
REPO_URL="${REPO_URL:-https://github.com/jaredpalmer/kev.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

if [ -z "${KEV_API_KEY}" ]; then
    echo "ERROR: メタデータ kev-api-key が未設定です。認証なしでは起動しません。"
    exit 1
fi

# 1. NVIDIA ドライバの確認
echo "[1/7] Checking NVIDIA GPU and Driver..."
DRIVER_INSTALLED=0
if ! command -v nvidia-smi &> /dev/null; then
    echo "nvidia-smi not found. Installing NVIDIA driver..."
    apt-get update -q
    apt-get install -y -q linux-headers-"$(uname -r)" software-properties-common
    # 構築中の自動カーネル更新で DKMS が壊れ GPU が消えるのを防ぐ
    apt-mark hold linux-image-gcp linux-headers-gcp linux-gcp || true
    add-apt-repository -y ppa:graphics-drivers/ppa
    apt-get update -q
    apt-get install -y -q nvidia-driver-535 nvidia-utils-535
    DRIVER_INSTALLED=1
fi
if ! nvidia-smi --query-gpu=gpu_name,memory.total,driver_version --format=csv,noheader; then
    if [ "${DRIVER_INSTALLED}" -eq 1 ]; then
        # 導入直後はカーネルモジュールが未ロードのことが多い。マーカーが無いので、再起動後に続きから実行される
        echo "ドライバを導入しました。ロードのため再起動します。"
        reboot
        sleep 120
    fi
    echo "ERROR: nvidia-smi が失敗しました。GPU とドライバを確認してください。"
    exit 1
fi

# 2. 基本パッケージと uv
echo "[2/7] Installing base packages and build tools..."
apt-get update -q
apt-get install -y -q git curl wget ca-certificates build-essential python3-dev python3-pip

if ! command -v uv &> /dev/null; then
    echo "Installing uv package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh
fi
export PATH="/usr/local/bin:${PATH}"

# 3. サービスユーザー
echo "[3/7] Setting up dedicated user: ${SERVICE_USER}..."
if ! id -u "${SERVICE_USER}" &>/dev/null; then
    useradd --system --create-home --shell /bin/bash "${SERVICE_USER}"
fi
usermod -a -G video "${SERVICE_USER}" || true

# 4. コードの取得 (非空ディレクトリには clone できないので、キャッシュ用ディレクトリは clone 後に作る)
echo "[4/7] Preparing Kev codebase from ${REPO_URL} (branch: ${REPO_BRANCH})..."
if [ ! -d "${INSTALL_DIR}/.git" ]; then
    mkdir -p "${INSTALL_DIR}"
    git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi
cd "${INSTALL_DIR}"

# deploy/gce/ は upstream に無いため、provision.sh がメタデータ属性で渡したファイルを配置する
mkdir -p deploy/gce
touch deploy/__init__.py deploy/gce/__init__.py
place_deploy_file() {
    local attr="$1" dest="$2"
    meta "${attr}" > "${dest}"
    if [ ! -s "${dest}" ]; then
        echo "ERROR: メタデータ ${attr} が空です。provision.sh で渡されているか確認してください。"
        exit 1
    fi
}
place_deploy_file kev-deploy-server-py deploy/gce/server.py
place_deploy_file kev-deploy-service deploy/gce/kev-serve.service
place_deploy_file kev-deploy-requirements deploy/gce/requirements.txt
mkdir -p "${HF_HOME}"

# 5. venv と依存関係 (uv の Python を kev ユーザーが読めるよう /opt/kev 配下に置く)
echo "[5/7] Creating Python venv and installing dependencies..."
export UV_PYTHON_INSTALL_DIR="${INSTALL_DIR}/.python"
uv venv "${VENV_DIR}" --python 3.12 --allow-existing
# PyTorch の index は numpy など他パッケージも置いているため、index をまたいで最良版を選ばせる
uv pip install --python "${VENV_DIR}/bin/python" --index-strategy unsafe-best-match -r "${INSTALL_DIR}/deploy/gce/requirements.txt"
uv pip install --python "${VENV_DIR}/bin/python" --index-strategy unsafe-best-match -e "${INSTALL_DIR}[serve]"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# 6. 重みの事前取得: LoRA(kev-4b) とベースモデルの両方。
#    サービスは HF_HUB_OFFLINE=1 で動くため、ここで全部取得しておく必要がある。
echo "[6/7] Pre-downloading model weights (Kev-4B LoRA and its base model)..."
runuser -u "${SERVICE_USER}" -- env HF_HOME="${HF_HOME}" "${VENV_DIR}/bin/python" - <<'PY'
from huggingface_hub import snapshot_download
from kev.checkpoint import Checkpoint

ck = Checkpoint("jaredpalmer/kev-4b")
print("LoRA cached at:", ck.path)
base = snapshot_download(
    ck.meta.base,
    revision=ck.meta.base_revision or None,
    allow_patterns=["*.json", "*.safetensors", "*.pt", "*.txt", "*.jinja"],
)
print("Base model cached at:", base)
PY

# 7. API キー・sudoers(アイドル停止用)・systemd サービス
echo "[7/7] Registering and starting kev-serve systemd service..."
install -d -m 0750 -o root -g "${SERVICE_USER}" /etc/kev
install -m 0640 -o root -g "${SERVICE_USER}" /dev/null /etc/kev/kev.env
echo "KEV_API_KEY=${KEV_API_KEY}" > /etc/kev/kev.env

# アイドル停止のために kev ユーザーへ許可するのは poweroff だけ
echo "${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" > /etc/sudoers.d/kev-poweroff
chmod 0440 /etc/sudoers.d/kev-poweroff
visudo -cf /etc/sudoers.d/kev-poweroff

cp "${INSTALL_DIR}/deploy/gce/kev-serve.service" /etc/systemd/system/kev-serve.service

# 重みのディスク読込(約9.5GB、冷読込で約38秒)が起動時間の大半を占めるため、OS 起動の早い段階で
# ページキャッシュに載せておく。OS 起動や Python の import と並行して読むので、モデルロードの読込待ちが減る。
cat > /etc/systemd/system/kev-prefetch.service <<EOF
[Unit]
Description=Kev model weights page-cache prefetch
DefaultDependencies=no
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'find ${HF_HOME}/hub/blobs -type f -exec cat {} + > /dev/null'

[Install]
WantedBy=multi-user.target
EOF

# アイドル停止の poweroff が、Google のシャットダウンスクリプトのサービス(既定は TimeoutStopSec=0 = 無制限)で
# 固まって VM が止まらないことがあった。タイムアウトを付ける。
mkdir -p /etc/systemd/system/google-shutdown-scripts.service.d
printf '[Service]\nTimeoutStopSec=30\n' > /etc/systemd/system/google-shutdown-scripts.service.d/timeout.conf

systemctl daemon-reload
systemctl enable kev-prefetch.service kev-serve.service
systemctl restart kev-serve.service

echo "Waiting for API server to become ready at http://127.0.0.1:8008/ready..."
READY=0
for _ in $(seq 1 150); do
    if curl -s -f http://127.0.0.1:8008/ready > /dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 2
done

if [ "${READY}" -eq 1 ]; then
    touch "${SETUP_MARKER}"
    echo "=================================================================="
    echo " Kev-4B API Server deployed. Finished at: $(date '+%Y-%m-%d %H:%M:%S')"
    echo " Status: http://127.0.0.1:8008/v1/status (Authorization: Bearer が必要)"
    echo "=================================================================="
else
    echo "WARNING: 300秒以内に ready になりませんでした。マーカーは作りません(次回ブートで再試行します)。"
    journalctl -u kev-serve -n 30 --no-pager || true
fi
