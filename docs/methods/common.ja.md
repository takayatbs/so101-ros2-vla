# RTC系・VLASHの共通準備：LeRobotDatasetへ書き出す

[手法の入口へ戻る](index.ja.md)

**このページのゴール：** 中間DatasetをLeRobotDataset v3.0へ書き出します。ACTだけを使う場合は、この追加変換は不要です。

## 1. 収集を終える

[連続収集](../collection_script.ja.md)で成功Episodeを集め、記録とRuntimeを終了します。独立した元Episodeが2本以上あると、学習用と検証用を分けられます。

以降のコマンドはGUIのターミナルで `cd /workspace` の後に実行します。別のRuntimeや学習が動いている間は、共通ロックにより変換・学習を開始しません。

## 2. 中間Datasetがあることを確認する

[Datasetを作る手順](../guide/dataset.ja.md)で `pick-place-aligned-v1` を作成済みなら、そのまま次へ進みます。まだなら先にその手順を行い、ここへ戻ります。同じ中間Datasetを再度作る必要はありません。

画像・実測関節状態・送信済み指令の時刻対応と、時刻差が大きい行の無効判定は、この中間Datasetに保存されています。

## 3. LeRobotDataset v3.0へ書き出す

```bash
./scripts/methods.sh export \
  --dataset /workspace/data/datasets/pick-place-aligned-v1 \
  --output /workspace/data/datasets/pick-place-lerobot-v1
```

既定はLinuxホストのNVENCです。CPUだけの環境や、NVENCの最小寸法より小さい試験画像では、明示的に `--encoder libx264` を追加できます。自動でCPUへ切り替わる処理はありません。

```text
meta/info.json                     FPS、特徴、保存形式
meta/tasks.parquet                 作業名
meta/episodes/chunk-000/*.parquet   Episodeと動画の対応
meta/stats.json                    標準形式の統計
data/chunk-000/*.parquet            関節状態、action、フレーム番号、取得時刻
videos/observation.images.top/...  等間隔の映像
videos/observation.images.arm/...
so101_bridge.json                  元Episode・元フレーム・単位の対応
provenance.json                    校正・設定・元データの由来
```

## 4. 動画と時刻の意味を理解する

LeRobot用MP4は、同期で選択した画像を設定FPSに並べ、画像前処理を適用して書き出します。そのためこの工程でオフラインの再エンコードが1回増えます。ライブ収集・Foxglove配信の圧縮は従来どおり共有したままです。元MCAPと中間Datasetの映像は残します。

`timestamp` は新しいEpisode内の相対秒です。元の取得時刻は `source_timestamp_ns`、`source_state_timestamp_ns`、`source_action_timestamp_ns`、`source_image_timestamp_ns.カメラ名` にint64で残します。元記録にns時刻がない旧形式については、その旧記録の精度から復元した値です。

無効な区間を除去して前後をつなぐことはせず、連続して有効な区間ごとに別Episodeを作ります。モデルの動作列が欠損区間をまたがないためです。

Stateとactionは関節順付きのrad、actionは実際に送った絶対関節目標です。任意の外部LeRobot Datasetが同じ単位・グリッパー表現を使っているとは限りません。今回の学習入口は、この書き出しで由来を確認できるDatasetを対象とします。

## 5. 手法を選ぶ

- [RTCへ進む](rtc.ja.md)
- [Training-time RTCへ進む](training_time_rtc.ja.md)
- [VLASHへ進む](vlash.ja.md)
- [ベースモデル学習後にA2C2を追加する](a2c2.ja.md)

LeRobotDataset形式の説明は[公式資料](https://huggingface.co/docs/lerobot/en/lerobot-dataset-v3)を参照できます。書き出したデータをローカルのLeRobotDatasetクラスで読み込む検証も行っています。
