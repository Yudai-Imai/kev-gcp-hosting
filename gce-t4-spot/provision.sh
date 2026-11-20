#!/usr/bin/env bash
# ==============================================================================
# Kev-4B を GCE (N1 + Tesla T4 Spot VM) に構築するスクリプト
#
# 構築中は外部IPを付ける。API として動作確認できたら外部IPを外す(README 参照)。
# 必須の環境変数:
#   KEV_ALLOWED_CIDR  API(8008)へのアクセスを許可する送信元。例: 構築中は運用者のグローバルIP "203.0.113.5/32"
#   KEV_API_KEY       API の Bearer 認証キー。例: export KEV_API_KEY="$(openssl rand -hex 32)"
# 任意:
#   KEV_REPO_URL      kev 本体の git リポジトリ(既定: upstream)。deploy/gce/ は upstream に無いため、
#                     server.py・kev-serve.service・requirements.txt をメタデータ経由で VM に渡す
# ==============================================================================
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo '')}"
ZONE="${GCP_ZONE:-asia-northeast1-a}"
INSTANCE_NAME="${GCP_INSTANCE_NAME:-kev-4b-t4-spot}"
MACHINE_TYPE="${GCP_MACHINE_TYPE:-n1-standard-4}"
ACCELERATOR="type=nvidia-tesla-t4,count=1"
BOOT_DISK_SIZE="60GB"
BOOT_DISK_TYPE="pd-ssd"      # 重み(約8GB)の読込を速くするため pd-balanced ではなく pd-ssd
KEV_REPO_BRANCH="${KEV_REPO_BRANCH:-main}"
# Git Bash から Windows 版 gcloud を呼ぶとき、/c/... 形式のパスは通らないため C:/... 形式にする (Linux では pwd にフォールバック)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && { pwd -W 2>/dev/null || pwd; })"
STARTUP_SCRIPT="${SCRIPT_DIR}/startup.sh"

if [ -z "${PROJECT_ID}" ]; then
    echo "ERROR: GCP_PROJECT_ID が未設定です。'gcloud config set project <ID>' か export GCP_PROJECT_ID を実行してください。"
    exit 1
fi
KEV_REPO_URL="${KEV_REPO_URL:-https://github.com/jaredpalmer/kev.git}"
for var in KEV_ALLOWED_CIDR KEV_API_KEY; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: ${var} が未設定です(スクリプト冒頭のコメント参照)。"
        exit 1
    fi
done
if [ "${KEV_ALLOWED_CIDR}" = "0.0.0.0/0" ]; then
    echo "ERROR: KEV_ALLOWED_CIDR に 0.0.0.0/0 は指定できません。"
    exit 1
fi

echo "=================================================================="
echo " Provisioning Kev-4B API Server on GCE Spot VM"
echo " Project:      ${PROJECT_ID}"
echo " Zone:         ${ZONE}"
echo " Instance:     ${INSTANCE_NAME} (${MACHINE_TYPE}, ${ACCELERATOR}, ${BOOT_DISK_TYPE})"
echo " Allowed CIDR: ${KEV_ALLOWED_CIDR}"
echo " Repo:         ${KEV_REPO_URL} (${KEV_REPO_BRANCH})"
echo "=================================================================="

# 1. ファイアウォール: API(8008) は指定の送信元のみ。SSH は IAP 経由のみ。
create_firewall() {
    local name="$1" rules="$2" ranges="$3" description="$4"
    if gcloud compute firewall-rules describe "${name}" --project="${PROJECT_ID}" &>/dev/null; then
        # 旧版のスクリプトが 0.0.0.0/0 で作ったルールを残さないよう、毎回送信元を上書きする
        echo "Firewall rule '${name}' already exists. 送信元を ${ranges} に更新します。"
        gcloud compute firewall-rules update "${name}" --project="${PROJECT_ID}" --source-ranges="${ranges}"
        return
    fi
    gcloud compute firewall-rules create "${name}" \
        --project="${PROJECT_ID}" --direction=INGRESS --priority=1000 --network=default \
        --action=ALLOW --rules="${rules}" --source-ranges="${ranges}" \
        --target-tags=kev-api --description="${description}"
}
create_firewall allow-kev-api-8008 tcp:8008 "${KEV_ALLOWED_CIDR}" "Kev API server (port 8008)"
create_firewall allow-kev-iap-ssh tcp:22 35.235.240.0/20 "SSH via IAP for Kev VM"

# 2. Spot VM 作成。停止しても削除されない(--instance-termination-action=STOP)ので、
#    起動ディスクと内部IPは残り、2回目以降の起動は startup.sh の再セットアップなしで済む。
gcloud compute instances create "${INSTANCE_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --accelerator="${ACCELERATOR}" \
    --provisioning-model=SPOT \
    --instance-termination-action=STOP \
    --maintenance-policy=TERMINATE \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size="${BOOT_DISK_SIZE}" \
    --boot-disk-type="${BOOT_DISK_TYPE}" \
    --metadata-from-file="startup-script=${STARTUP_SCRIPT},kev-deploy-server-py=${SCRIPT_DIR}/server.py,kev-deploy-service=${SCRIPT_DIR}/kev-serve.service,kev-deploy-requirements=${SCRIPT_DIR}/requirements.txt" \
    --metadata="kev-repo-url=${KEV_REPO_URL},kev-repo-branch=${KEV_REPO_BRANCH},kev-api-key=${KEV_API_KEY}" \
    --tags=kev-api \
    --scopes=logging-write,monitoring-write

EXTERNAL_IP=$(gcloud compute instances describe "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" \
    --format="get(networkInterfaces[0].accessConfigs[0].natIP)")
INTERNAL_IP=$(gcloud compute instances describe "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" \
    --format="get(networkInterfaces[0].networkIP)")

echo ""
echo "=================================================================="
echo " Created '${INSTANCE_NAME}'  external: ${EXTERNAL_IP}  internal: ${INTERNAL_IP}"
echo ""
echo "1. 初回セットアップのログ(数分〜10分):"
echo "   gcloud compute ssh ${INSTANCE_NAME} --zone=${ZONE} --tunnel-through-iap --command='sudo tail -f /var/log/kev-startup.log'"
echo "2. 動作確認:"
echo "   python deploy/gce/test_client.py --host ${EXTERNAL_IP} --api-key \"\$KEV_API_KEY\""
echo "3. 確認できたら外部IPを外す:"
echo "   gcloud compute instances delete-access-config ${INSTANCE_NAME} --zone=${ZONE} --access-config-name='external-nat'"
echo "   (その後は firewall allow-kev-api-8008 の送信元を呼び出し元の範囲に更新する)"
echo "=================================================================="
