# GCE の T4 Spot VM で Kev-4B を動かす

VM は普段は止めておき、`../cloudrun-proxy/` が必要なときに起こします。最後の推論から15分で自動停止します。検証用の構成です。

## ファイル

| ファイル | 内容 |
| :--- | :--- |
| `provision.sh` | ファイアウォールと Spot VM の作成 |
| `startup.sh` | VM の初回セットアップ(2回目以降は何もしない) |
| `server.py` | 推論サーバー(Spot 中断検知、アイドル停止、`/v1/status`、ウォームアップ) |
| `kev-serve.service` | systemd のサービス定義 |
| `requirements.txt` | 依存関係(torch は kev 本体の範囲に合わせている) |
| `test_client.py` | 動作確認クライアント |

`server.py` は upstream の `kev.checkpoint.DecisionModel` の参照を差し替えています(GPU へ直接ロードするため)。upstream を更新したら、起動ログと `test_client.py` で確認してください。

## 構築

```bash
export GCP_PROJECT_ID="<PROJECT_ID>"
export KEV_ALLOWED_CIDR="<運用者のグローバルIP>/32"   # 0.0.0.0/0 は拒否される
export KEV_API_KEY="$(openssl rand -hex 32)"
./provision.sh
```

- `KEV_API_KEY` は VM のメタデータ属性として渡り、`/etc/kev/kev.env` に書かれます。メタデータは `compute.instances.get` 権限を持つ人から見えます。
- kev 本体は upstream を clone します。既定は `main` なので、再現性のため `KEV_REPO_BRANCH` にタグかブランチを指定して固定してください(`git clone --branch` を使うので、コミットハッシュは指定できません。`server.py` は upstream の内部実装に依存します)。`server.py`・`kev-serve.service`・`requirements.txt` はメタデータ属性で渡し、VM 上の `deploy/gce/` に置きます。この3ファイルを変えたら、VM の作り直しかメタデータの更新が必要です。
- 構築中は外部IPを付け、`test_client.py` で確認できたら外します。

```bash
python test_client.py --host <VMの外部IP> --api-key "$KEV_API_KEY"
```

## T4 で必要だった設定

| 項目 | 設定 | 理由 |
| :--- | :--- | :--- |
| 精度 | `fp16` | T4(Turing)は bf16 の Tensor Core に対応していない |
| LoRA マージ | `merge=True` | 起動時にベースへマージし、推論時の上乗せをなくす |
| CUDA Graphs | `0` | 検証で安定していた設定 |
| Prefix cache | `8` | 同じ長い state への複数の質問を速くする |

## 起動時間

VM の start から `/ready` が 200 になるまで、約61秒です(OS 起動 約26秒、import 約22秒、モデルロード 約11秒、ウォームアップ 約2秒)。bf16 から fp16 への変換を CPU でやるとロードが遅れるので、GPU へ直接ロードしています。出力は従来の経路と一致することを、4つの state と3問で確認しました。

## 停止

- アイドル停止は `poweroff` で行います。そのあと、シャットダウンスクリプトとゲストエージェントの停止待ちで、VM が TERMINATED になるまで約90秒かかります。
- Spot の中断通知はメタデータサーバーで見て、`/ready` を 503 にします。
- 念のため、外から VM を止めるインスタンススケジュールを別に設定することを勧めます(このリポジトリには含まれません)。
