# kev-proxy(Cloud Run)

GCE 上の Kev サーバー(`../gce-t4-spot/`)を呼び出すだけの Go 製プロキシです。VM を起動する以外のことはしません。止めるのは VM 側のアイドル停止に任せます。依存ライブラリはありません。

## 動作

- `POST /v1/systemone`: `Authorization: Bearer <KEV_API_KEY>` が一致したときだけ VM を起動し、`/ready` を待って、ヘッダを変えずに転送する。誤ったキーでは VM に触れない。
- `POST /wake`: 同じキーを検証し、ready なら 200、そうでなければ起動を始めて 202(`Retry-After: 10`)を返す。呼び出し側は 200 になるまでポーリングする。
- `GET /health`: VM に触れずに 200。
- 同時に複数のリクエストが来ても、start は1回にまとめる。Spot の容量不足は 503 と `Retry-After: 30`。
- ready を確認してから15秒間は、状態を取り直さずに転送する。接続に失敗したら、状態を取り直して1回だけ再試行する。
- `/ready` や `/wake` のポーリングは、アイドル停止の時間を延ばさない(推論呼び出しだけが数える)。

## 応答時間

- VM 起動中: p50 約250ms(手元PC → Cloud Run → VM、推論込み)
- VM 停止中の最初の1回: 約89秒。呼び出し側のタイムアウトは120秒以上にする

## デプロイ

```bash
gcloud run deploy kev-proxy --project=<PROJECT_ID> --region=<REGION> --source=. \
  --service-account=<SERVICE_ACCOUNT> \
  --network=default --subnet=default --vpc-egress=private-ranges-only \
  --set-secrets=KEV_API_KEY=<SECRET_NAME>:latest \
  --set-env-vars=PROJECT=<PROJECT_ID>,ZONE=<ZONE>,INSTANCE=<INSTANCE_NAME> \
  --allow-unauthenticated --timeout=300 --concurrency=80 --cpu=1 --memory=256Mi \
  --min-instances=0 --max-instances=3 --cpu-boost
```

- ビルド中に `go vet` と `go test` が走り、失敗するとデプロイされません。
- サービスアカウントには、対象インスタンスだけの `compute.instances.get` / `start` と、Secret の `secretAccessor` を付けます。
- VM のファイアウォールで、Direct VPC egress が使うサブネットの範囲を許可してください。`gce-t4-spot/provision.sh` を再実行すると許可元が上書きされるので、再実行後に追加し直します。
- API キーは VM と Secret Manager の2か所にあります。変えるときは両方を更新します。
