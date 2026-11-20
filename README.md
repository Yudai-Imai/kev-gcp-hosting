# kev-gcp-hosting

意思決定モデル [Kev](https://github.com/jaredpalmer/kev)(TypeSafe `/v1/systemone` 互換)を、GCP で自前ホストするために試した2つの構成です。どちらも検証用で、本番運用を保証するものではありません。

| ディレクトリ | 構成 | 向いている場合 |
| :--- | :--- | :--- |
| [`cloudrun-l4/`](cloudrun-l4/) | Cloud Run の GPU(L4)に Kev を直接載せる | 運用を軽くしたい。ゼロスケールは標準機能に任せる |
| [`gce-t4-spot/`](gce-t4-spot/) と [`cloudrun-proxy/`](cloudrun-proxy/) | GCE の T4 Spot VM に載せ、Cloud Run のプロキシが必要なときだけ起こす | VM を自分で制御したい。内部IPだけで閉じたい。GPU を選びたい |

## 実測の比較

| | Cloud Run(L4) | GCE(T4 Spot)+ プロキシ |
| :--- | :--- | :--- |
| コールドスタート | 4B で約47秒、9B で約102秒 | VM 停止後の最初の1回で約89秒(VM の起動は約61秒) |
| ウォーム時の応答(手元PCから各40回) | p50 286ms、p95 416ms | p50 250ms、p95 714ms |
| 運用 | 少ない | VM、停止制御、Spot の中断対応を自分で持つ |

同じ8ケースの出力は、選択(choice)が全て一致し、確率とスコアの差は最大 0.013 でした。応答時間は測るたびに動きます(別の回にプロキシ経由で80回測ったときは p50 約250ms、p95 約420ms)。

## 構成の考え方と、記事

設計の判断と、つまずいた点は記事にまとめています: TBD

## upstream との関係

Kev 本体は [jaredpalmer/kev](https://github.com/jaredpalmer/kev)(Apache-2.0、Copyright 2026 Jared Palmer)で、このリポジトリには含めていません。

- `cloudrun-l4/` は upstream を取得し、`patches/async-init.patch` を当てます(モデルのロード中もポートを開き、`/health` を返すための変更)。
- `gce-t4-spot/server.py` は upstream の `kev.serve` を土台に、Spot 中断検知、アイドル停止、`/ready` などを足したものです。

このリポジトリのコードも Apache-2.0 です(`LICENSE`)。著作権者の表記と upstream の帰属は `NOTICE` にあります。

## 検証状況

最後の推論から15分後の自動停止は、通しで動作を確認しました。

未検証:

- 同時リクエストで VM の start が1回にまとまること(単体テストのみ)
- Spot の容量不足時の 503
- `cloudrun-l4/` のイメージは、このリポジトリの形(upstream の取得とパッチ適用)では、まだビルドして確認していない
