# 実機なしで起動・記録・停止を練習する

理由や仕組みから学ぶ場合は、[ROSの通信と状態・指令の意味](../learning/02-ros-communication.ja.md)を併読できます。

[READMEへ戻る](../../README.md) ／ 前：[環境構築](../setup.ja.md)

**このページのゴール：** 仮想のロボットを動かし、数秒の記録を保存して終了します。実機のUSB機器へは接続しません。

**入力場所：GUIの端末A・B。** [端末の準備](desktop.ja.md#terminals)を済ませ、両方で `cd /workspace` を実行します。他のRuntime・学習・Probeは終了しておきます。

## 1. AでRuntimeを起動する

```bash
./scripts/teleop.sh mock \
  config:=/workspace/config/runtime/system.real30hz.640x480.yaml
```

Aはこのまま開いておきます。Runtimeは、ロボット・カメラ・制御・記録をまとめて動かすプログラム群です。起動直後はIDLE（待機）です。

この練習はRaw画像の640×480設定を使います。実機収集では、圧縮した映像を保存する別の設定を使います。

## 2. Bで準備完了を確認する

```bash
ros2 control list_controllers
ros2 topic echo /control_mode --once
```

現在のLeRobot Profileでは、`joint_state_broadcaster` と `forward_position_controller` がともに `active`、モードが `IDLE` であることを確認します。旧bounded Profileだけは後者が `joint_trajectory_controller` になります。

## 3. Bで仮想ロボットを動かす

```bash
./scripts/mode.sh TELEOP
ros2 topic echo /follower/joint_states --once
```

モード変更の応答が `success=True` になり、関節の `position` が表示されます。少し待って関節表示をもう一度実行すると値が変わります。mockのLeaderはプログラムが動かすため、実物の操作は不要です。

<a id="record"></a>

## 4. Bで5〜10秒記録する

```bash
./scripts/record.sh start 'mock_follow' 'operator'
```

応答の `success=True` を確認し、表示されたEpisode IDを控えます。Episodeは作業1回分の記録です。5〜10秒後、次を1行ずつ実行します。

```bash
./scripts/record.sh stop true
./scripts/mode.sh IDLE
```

`stop true` は記録を正常に閉じ、成功というラベルを付ける操作です。この練習データは実機の作業データへ混ぜません。

## 5. 保存されたことを確認する

次の `EPISODE_ID` を先ほどのIDへ置き換えます。

```bash
cat data/bags/EPISODE_ID/episode.json
ros2 bag info /workspace/data/bags/EPISODE_ID/raw
```

`episode.json` の `status` が `complete` で、bagに画像・関節・指令のTopicがあることを確認します。保存先はLinuxホストです。

## 6. 停止と終了を練習する

Bで次を1行ずつ実行します。

```bash
./scripts/estop.sh
ros2 topic echo /control_mode --once
./scripts/reset.sh
ros2 topic echo /control_mode --once
```

ESTOP → Reset後にIDLEとなることを確認します。リセットだけでは動作を再開しません。

最後に**AでCtrl+C**を押します。Aがコマンドを入力できる状態へ戻ればRuntimeは終了です。

**完了の目安：** 2つのControllerがactiveになり、TELEOP、短い記録のcomplete、停止・リセットを確認できた。

次は[実機の接続と校正を確認する](hardware.ja.md)へ進みます。途中で失敗した場合は[症状から調べる](../troubleshooting.ja.md)を使います。
