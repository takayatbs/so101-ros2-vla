# Training-time RTCを学習・実行する

[手法の入口へ戻る](index.ja.md) ／ [共通データ準備](common.ja.md)

動作列の先頭がすでに決まっている状況を、学習時から練習します。

## 1. 何を変える手法か

例えば30個の動作の先頭3個を既知の正解として与え、残り27個を予測させます。既知部分へノイズを加えず、損失は残りの有効な部分だけで計算します。推論でも既知prefixを固定するため、RTCの勾配誘導は不要です。

このページのコマンドは標準の小型flowモデルを使います。`--policy smolvla`・`pi0`・`pi05`・`groot`・`xvla`で事前学習モデルへ変更できます。初回の依存導入、モデルと手法の対応表は[モデルを引数で選ぶ](models.ja.md)を参照してください。

## 2. 学習する

[共通準備](common.ja.md)で `pick-place-lerobot-v1` を作り、Runtimeを停止してから実行します。

```bash
./scripts/methods.sh train --method training_time_rtc \
  --config /workspace/config/runtime/system.real30hz.640x480.training_time_rtc.yaml \
  --dataset /workspace/data/datasets/pick-place-lerobot-v1 \
  --output /workspace/data/logs/training/training-time-rtc-v1 \
  --model-id training-time-rtc-v1 --steps 20000 --batch-size 8
```

モデルは `data/models/training-time-rtc-v1/` へ登録されます。学習ログは指定したoutputに残ります。既存の出力・モデル名は上書きしません。学習の小規模動作確認には、別名を付けて `--steps 3 --dim-model 32 --n-layers 1 --denoise-steps 4` を指定できます。この短い学習で実機作業ができるようになるとは判断しません。

## 3. 保存された指標を確認する

学習中の `flow_matching_loss` と、学習終了時の `evaluation.json` を確認します。検証用の元Episodeがあれば、学習に使わないEpisodeで関節誤差と推論時間を計算します。

```bash
./scripts/methods.sh evaluate \
  --model /workspace/data/models/training-time-rtc-v1 \
  --dataset /workspace/data/datasets/pick-place-lerobot-v1 \
  --delay-steps 3 \
  --output /workspace/data/logs/evaluation/training-time-rtc-v1.json
```

この評価は記録済みのprefix・状態を使って遅延を模擬するオフライン評価です。実機を閉ループで動かした成功率ではありません。同じデータの学習部分を検証部分として使いません。

## 4. 最初にmockで起動する

```bash
./scripts/deploy_policy.sh training-time-rtc-v1 mock
```

別のターミナルで `/policy/ready` を確認します。

```bash
ros2 topic echo /policy/ready --once --qos-reliability reliable --qos-durability transient_local
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

`config/policy/policy.training_time_rtc.yaml` の `action_chunk.scheduler` を使います。

```yaml
method: training_time_rtc
max_delay_steps: 3
max_inference_s: 0.2
```

30 Hzの3ステップは約100 msです。チャンク長30、次の推論を始める実行区間10の設定になっています。推論中も以前のチャンクからその時刻の動作を取り出し、推論にかかった時刻以前の動作は新たに実行しません。モード変更後に届いた古い推論結果は採用しません。

許容範囲を超える遅延や動作列の枯渇では出力を止め、既存Watchdogの停止対象にします。古い指令を繰り返して運転を継続する処理はありません。

Training-time RTC・VLASHでは、学習した遅延範囲を超える設定は拒否します。遅延範囲を変える場合は設定と学習をそろえます。RTC単体のprefix誘導は推論時の処理なので、チャンクの範囲内で遅延予算を変更できます。

原典：[論文](https://arxiv.org/abs/2512.05964)。
