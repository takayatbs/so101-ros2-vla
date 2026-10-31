# ACTを学習し、実行する

**学習済みモデルを動かしたい場合は、[推論・自律動作の手順](inference.ja.md)へ進んでください。** モデルの起動・準備確認・動作開始・停止を、端末A・Bの順に説明しています。

理由や仕組みから学ぶ場合は、[入力・正規化・学習と検証の意味](../learning/07-models-and-learning.ja.md)を併読できます。

[手法の選択へ戻る](../methods/index.ja.md) ／ 前：[Datasetを作る](dataset.ja.md)

**このページのゴール：** 収集した単一タスクのDatasetでACTを学習し、モデルを読み込んで評価します。

**開始条件：** `pick-place-aligned-v1` を作成・確認済み。別の出力名にした場合は以下のパスも合わせます。この例は640×480・H.264の収集設定とCUDAを使います。実際の収集設定が違う場合は、その設定を使います。

**入力場所：GUIの端末。** Runtime・収集・変換を終了し、`cd /workspace` を実行します。

<a id="train"></a>

## 1. 学習する

```bash
./scripts/train.sh \
  --config /workspace/config/runtime/system.real30hz.640x480.h264.yaml \
  --dataset /workspace/data/datasets/pick-place-aligned-v1 \
  --output /workspace/data/logs/training/pick-place-act-v1 \
  --model-id pick-place-act-v1 \
  --steps 20000 --batch-size 8 --device cuda
```

`steps` は重みを更新する回数、`batch-size` は1回に使うサンプル数です。開始例であり、この回数で作業が成功するという保証ではありません。最初に保存までの接続だけを試す場合は、別の出力名・モデルIDにして `--steps 2 --batch-size 1` を使えます。その短い学習の重みを実機作業へ使いません。

学習・検証はEpisodeで分け、正規化は学習側から計算します。検証用の独立したEpisodeを用意してください。モデルは画像と関節状態を使い、単一タスクを学習します。

## 2. 学習結果と登録を確認する

必要なら別のGUI端末で進行を表示します。

```bash
tail -f data/logs/training/pick-place-act-v1/metrics.jsonl
```

表示だけの終了は、その端末のCtrl+Cです。学習中の端末でCtrl+Cを押すと学習を中断します。lossや `validation_l1_rad` は予測誤差の指標で、作業の成功率とは別です。

正常終了後、登録されたモデルを確認します。

```bash
cat data/models/pick-place-act-v1/deployment.json
```

モデルと一緒に、校正・関節順・前処理・学習データの由来が保存されます。再学習では `v2` など新しい出力名とモデルIDを使います。

<a id="deploy"></a>

## 3. Aでmockのモデル実行を起動する

```bash
./scripts/deploy_policy.sh pick-place-act-v1 mock
```

Bで起動状態を確認します。

```bash
ros2 control list_controllers
ros2 topic echo /control_mode --once
ros2 topic echo /policy/ready --once --qos-reliability reliable --qos-durability transient_local
```

2つのControllerがactive、IDLE、readyがtrueになってから実行します。

```bash
./scripts/mode.sh AUTONOMOUS
ros2 topic echo /policy/action --once
```

応答の成功とAction出力を確認します。mockでは配線・モデルの読み込みを確認します。合成画像なので実際の作業成功は評価できません。

Bで `./scripts/mode.sh IDLE`、続いてAでCtrl+Cを押して終了します。

## 4. 実機で短く評価する

[実機受入](../hardware_acceptance.ja.md)と当日の接続・支持・停止手段を確認します。学習時と同じ校正・設定・カメラ配置・作業条件を使います。上の起動コマンドの `mock` を `real` に変え、同じ順でIDLE・映像・状態・readyを確認してからAUTONOMOUSへ切り替えます。

評価を記録する場合は、BでAUTONOMOUSへの切り替え成功後に実行します。切替直後からロボットは動作し、記録開始までの動きは保存されません。

```bash
./scripts/record.sh start '物体を箱に入れる' 'evaluation'
```

## 5. 終了する

アームを支持し、BでまずAIの動作を止め、記録している場合は直後に閉じます。

```bash
./scripts/mode.sh IDLE
./scripts/record.sh stop true
```

失敗なら `stop false '失敗理由'` を使います。記録していなければ `record.sh stop` は不要です。その後、AでCtrl+Cを押します。危険な動きでは記録整理より停止手段を優先します。

**完了の目安：** モデルが登録され、mockで読み込み・指令・停止を確認できた。実機では別に成功・失敗を評価できる。

次は評価結果をもとに収集へ戻ります。入力・損失・分割・ログの説明が必要な場合だけ、[ACT学習の参考資料](../learning/07-models-and-learning.ja.md)を開きます。CPUでの実行条件は[設定の参考資料](../configuration.ja.md)にあります。
