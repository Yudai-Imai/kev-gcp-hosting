# Cloud Run(L4)に Kev を直接載せる

Cloud Run の GPU(NVIDIA L4 1基、8 vCPU、32GiB)で Kev-4B / Kev-9B を動かす構成です。重みは GCS に置き、GCSFuse でマウントして、Hugging Face への通信は止めます(`HF_HUB_OFFLINE=1`)。

> このディレクトリは、動かしていた構成を公開用に組み直したものです。upstream の取得とパッチ適用を含むイメージのビルドは、まだ確認していません。`KEV_REF` は upstream のコミットに固定してください。

## ファイル

| ファイル | 内容 |
| :--- | :--- |
| `Dockerfile` | CUDA 12.4 + PyTorch。upstream を取得して `patches/async-init.patch` を当てる |
| `patches/async-init.patch` | モデルのロード中もポートを開き、`/health` に `loading` / `ready` を返す。推論は `threading.Event` でロード完了を待つ |
| `entrypoint.sh` | GCS のマウント先から `/models` を用意する。`PREFETCH_TO_RAM=1` で `/dev/shm` へ先読みする |
| `cloudbuild.yaml` | Cloud Build でのビルド |
| `scripts/test_kev_service.py` | 疎通テスト |
| `scripts/load_test_kev.py` | 同時実行数を変えた負荷試験 |

## 重みの置き方

GCS バケットに次のディレクトリを置きます。

```
qwen-4b-base/   ベースモデル(Qwen3.5-4B-Base)
kev-4b/         Kev-4B のアダプターと分類ヘッド
qwen-9b-base/   9B 用(使う場合)
kev-9b/
```

アダプターの設定ファイル(`adapter_config.json`)と分類ヘッド(`head.pt`)には、ベースモデルの Hugging Face リポジトリ名が書かれています。オフラインで動かすため、同期するときにこれをコンテナ内のパス(`/models/qwen-4b-base` など)へ書き換える必要があります。

## デプロイの要点

当時の設定を文章にすると次のとおりです。コマンドは、そのまま実行して確認したものではないので、`gcloud run deploy --help` で確認してください。

- GPU: L4 を1基。CPU 8、メモリ 32GiB(この GPU で Cloud Run が許す上限)
- `--min-instances=0`、`--max-instances=1`、`--concurrency=16`
- Direct VPC egress(`--network` と `--subnet`)。サブネットは Private Google Access を有効にする
- GCS ボリュームを `/mnt/gcs` にマウントし、`mount-options` に `enable-buffered-read=true` を付ける
- 環境変数: `KEV_MODEL`(マウント先のパス)、`KEV_API_KEY`、`HF_HUB_OFFLINE=1`、`PREFETCH_TO_RAM`
- `KEV_API_KEY` は Secret Manager に置く

## 実測

> 以下の数字は、古い upstream のスナップショットに手を加えたコードで測ったものです。このリポジトリの「upstream + パッチ」の構成で測り直した数字ではありません。

| モデル | 最初のロード | 最適化後のコールドスタート | 並行16の処理量 |
| :--- | :--- | :--- | :--- |
| 4B(約8GB) | 約160秒 | 約47秒(先読み17秒 + PyTorch 約20秒) | 13.03 req/s |
| 9B(約18.5GB) | 約330秒 | 約102秒 | 11.70 req/s |

- 4B は重みが `/dev/shm`(上限16GB)に収まるので、起動時に先読みする(`PREFETCH_TO_RAM=1`)。
- 9B は18.5GBで `/dev/shm` に入らない(`No space left on device`)。先読みせず、Direct VPC と GCSFuse の buffered-read で直接ストリーミングする。
- Cloud Run の startup probe は最大240秒。ロードが長いモデルは、サーバーを先に起動する `async-init.patch` が必須。
- 負荷試験は同時実行 1、4、8、16 で各16リクエスト。失敗は0件。
- コールドスタートの縮め方は、Djev-run というプロジェクトの設計を参考にしている。

## 使い方

```bash
export KEV_API_KEY="<API_KEY>"
python scripts/test_kev_service.py --url https://<SERVICE_URL>
python scripts/load_test_kev.py --url https://<SERVICE_URL> --concurrencies 1,4,8,16 --requests-per-stage 16
```

初回のリクエストは、コンテナの起動とモデルのロードを待つので、クライアントのタイムアウトは180秒以上にしてください。
