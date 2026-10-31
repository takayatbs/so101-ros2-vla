# 学習済みモデルでアームを動かす

[README](../../README.md) ／ [手順書一覧](../index.ja.md)

学習を終えて `data/models/` に登録したモデルを使う手順です。モデルの読み込み → 準備確認 → 自律動作 → 停止の順に進みます。

入力場所は、ブラウザのMATEデスクトップ内の端末です。**端末Aを起動用、端末Bを確認・操作用として、2つ開きます。** 実機の接続・校正・受入を済ませ、アームの初期姿勢・物体・カメラの配置を学習時に合わせます。収集・学習・可視化など、他の処理は終了しておきます。

## 1. モデルIDを確認する

端末Aで実行します。

```bash
cd /workspace
ls data/models/*/deployment.json
```

例えば `data/models/smolvla-vlash-v1/deployment.json` があれば、モデルIDは `smolvla-vlash-v1` です。以下の `MODEL_ID` を、そのフォルダー名に置き換えてください。チェックポイントのファイル名や、モデルの種類を表す `smolvla` とは異なります。

## 2. 端末Aでモデルを起動する

```bash
cd /workspace
./scripts/deploy_policy.sh MODEL_ID real --smoothing three_point
```

この例は実機用で、腕の目標を平滑化します。平滑化を使わない場合は `--smoothing three_point` を省略します。既定では無効です。

モデルの種類・学習方式・Python環境・保存した実行設定は登録情報から選びます。SmolVLA＋VLASHでも `--policy` や `--method` を指定し直す必要はありません。通常は `--config` も省略し、学習時のカメラ・遅延設定を引き継ぎます。

**起動直後はIDLE・トルクOFFです。** モデルを準備していても、自律動作はまだ始まりません。端末Aはログを表示したままにし、次の操作は端末Bで行います。

## 3. 端末Bで準備完了を確認する

```bash
cd /workspace
ros2 topic echo /policy/ready std_msgs/msg/Bool \
  --qos-reliability reliable --qos-durability transient_local \
  --once --filter 'm.data'
```

`data: true` が表示され、コマンドが終了するまで待ちます。準備中のfalseは表示しません。表示されない場合は端末Aの起動ログを確認し、動作開始へ進みません。

次にControllerを確認します。

```bash
ros2 control list_controllers
```

`joint_state_broadcaster` と `forward_position_controller` の**両方がactive**であることを確認します。inactiveやエラーがあれば、端末Aのログから原因を確認します。

## 4. 端末Bで自律動作を開始する

実機の配置と周囲を確認してから実行します。

```bash
./scripts/mode.sh AUTONOMOUS
```

`success=True` の応答を確認します。ここからモデルの予測に従ってアームが動きます。この手順には時間制限や自動初期位置復帰はなく、停止操作まで動作を続けます。物体の置き直しは作業条件に合わせて行います。

## 5. 動作を止め、Runtimeも終了する

まず端末Bで実行します。

```bash
./scripts/mode.sh IDLE
```

`success=True` を確認します。アームの動作が止まり、トルクOFFになります。Runtimeはカメラ・状態監視・モデルを読み込んだまま残ります。

作業を終える場合は、**続けて端末AでCtrl+C**を押し、プロンプトが戻るまで待ちます。これでRuntime自体が終了し、可視化・学習などで使う実行ロックも解放されます。

| 行いたいこと | 操作 |
|---|---|
| 自律動作を一時停止する | 端末Bで `./scripts/mode.sh IDLE` |
| 同じRuntimeで再び動かす | 準備とControllerを再確認して端末Bで `./scripts/mode.sh AUTONOMOUS` |
| モデル実行を終え、可視化や学習へ移る | 端末BでIDLE → 端末AでCtrl+C |

## 起動できない・動かない場合

- `Runtime/training is already using Robot Compute`：別のRuntime・学習・可視化が残っています。実行中の端末を確認し、その処理を終了します。
- `data: true` が出ない：端末Aでモデル・カメラ・設定照合のエラーを確認します。
- Controllerがinactive、FAULT/ESTOPになった：停止原因を確認して修復し、Runtimeを起動し直します。準備確認を省略して動作開始を繰り返しません。

実機なしでモデルの読み込みを確認する場合は、起動コマンドの `real` を `mock` に変更します。合成画像・関節での確認になり、実機タスクの成功率を評価するものではありません。
