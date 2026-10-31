# VLASHを学習・実行する

**学習済みモデルを動かしたい場合は、[推論・自律動作の手順](../guide/inference.ja.md)へ進んでください。** モデルの起動・準備確認・動作開始・停止を、端末A・Bの順に説明しています。

[手法の入口へ戻る](index.ja.md) ／ [共通データ準備](common.ja.md)

画像を見た時点と、実際に動作を始める時点のずれに対応するモデルを学習します。

## 1. 何を変える手法か

学習では「時刻tの画像」と「t＋δの関節状態・t＋δからの動作列」を組にします。画像は固定し、状態とactionを一緒にずらします。実行時は未実行の関節目標から将来の状態を見積もります。今回の絶対位置指令では、その時刻までに送る最後の目標関節位置を見積もりとして使います。未来の実測値を先読みする処理ではありません。

このページのコマンドは、標準の小型画像条件付きflowモデルを使用します。`--policy smolvla` などでLeRobotモデルへ変更できます。対応する8モデル、初回の依存導入、共通の起動手順は[モデルを引数で選ぶ](models.ja.md)を参照してください。

## 2. 学習する

[共通準備](common.ja.md)で `pick-place-lerobot-v1` を作り、Runtimeを停止してから実行します。

```bash
./scripts/methods.sh train --method vlash \
  --config /workspace/config/runtime/system.real30hz.640x480.vlash.yaml \
  --dataset /workspace/data/datasets/pick-place-lerobot-v1 \
  --output /workspace/data/logs/training/vlash-v1 \
  --model-id vlash-v1 --steps 20000 --batch-size 8
```

モデルは `data/models/vlash-v1/` へ登録されます。学習ログは指定したoutputに残ります。既存の出力・モデル名は上書きしません。学習の小規模動作確認には、別名を付けて `--steps 3 --dim-model 32 --n-layers 1 --denoise-steps 4` を指定できます。この短い学習で実機作業ができるようになるとは判断しません。

## 3. 保存された指標を確認する

学習中の `flow_matching_loss` と、学習終了時の `evaluation.json` を確認します。検証用の元Episodeがあれば、学習に使わないEpisodeで関節誤差と推論時間を計算します。

```bash
./scripts/methods.sh evaluate \
  --model /workspace/data/models/vlash-v1 \
  --dataset /workspace/data/datasets/pick-place-lerobot-v1 \
  --delay-steps 3 \
  --output /workspace/data/logs/evaluation/vlash-v1.json
```

この評価は記録済みのprefix・状態を使って遅延を模擬するオフライン評価です。実機を閉ループで動かした成功率ではありません。同じデータの学習部分を検証部分として使いません。

## 4. 最初にmockで起動する

```bash
./scripts/deploy_policy.sh vlash-v1 mock
```

別のターミナルで `/policy/ready` を確認します。

```bash
ros2 topic echo /policy/ready std_msgs/msg/Bool \
  --qos-reliability reliable --qos-durability transient_local \
  --once --filter 'm.data'
```

`data: true` はモデルの準備完了です。起動直後はIDLEで、駆動指令は送りません。実行する場合だけ次を入力します。

```bash
./scripts/mode.sh AUTONOMOUS
```

Foxgloveの関節値と `/diagnostics` のPolicy項目を見ます。mockのカメラ画像・関節は生成データなので、ここでは処理の接続を確認します。

## 5. 実機で評価する

[実機受入](../hardware_acceptance.ja.md)と共通の起動前確認を済ませた設定で、上のコマンドの `mock` を `real` へ変更します。最初はIDLEで映像・状態・準備完了を確認し、実行時だけAUTONOMOUSへ切り替えます。

実行時の映像を見ながら記録する場合は、従来の `record.sh start` / `record.sh stop` を使えます。終了は記録停止 → `./scripts/mode.sh IDLE` → RuntimeのCtrl+Cです。

## 6. 遅延設定の意味

`config/policy/policy.vlash.yaml` の `action_chunk.scheduler` を使います。

```yaml
method: vlash
max_delay_steps: 3
max_inference_s: 0.2
```

30 Hzの3ステップは約100 msです。チャンク長30、次の推論を始める実行区間10の設定になっています。推論中も以前のチャンクからその時刻の動作を取り出し、推論にかかった時刻以前の動作は新たに実行しません。モード変更後に届いた古い推論結果は採用しません。

許容範囲を超える遅延や動作列の枯渇では出力を止め、既存Watchdogの停止対象にします。古い指令を繰り返して運転を継続する処理はありません。

Training-time RTC・VLASHでは、学習した遅延範囲を超える設定は拒否します。遅延範囲を変える場合は設定と学習をそろえます。RTC単体のprefix誘導は推論時の処理なので、チャンクの範囲内で遅延予算を変更できます。

原典：[論文](https://arxiv.org/abs/2512.01031)。

## 7. 毎ステップ補正を追加する

このベースモデルを学習した後、[A2C2の専用手順](a2c2.ja.md)へ進むと、VLASH＋A2C2として実行できます。
