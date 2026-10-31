# テレオペだけ行う

理由や仕組みから学ぶ場合は、[Runtime起動とモード切替の違い](../learning/04-configuration-and-runtime.ja.md)を併読できます。

[READMEへ戻る](../../README.md)

**このページのゴール：** 人がLeaderを動かしてFollowerを操作し、終了します。連続して作業を記録する場合は[収集スクリプト](../collection_script.ja.md)を使います。

**開始条件：** [実機の接続・校正・受入](hardware.ja.md)を確認済み。アームの支持と駆動電源の停止手段を確保します。

**入力場所：GUIの端末A・B。** 両方で `cd /workspace` を実行します。他のRuntime・収集・学習は終了しておきます。

## 1. Aで起動する

```bash
./scripts/teleop.sh real \
  config:=/workspace/config/runtime/system.real30hz.640x480.h264.yaml
```

ロボット・カメラ・制御・記録管理・Foxglove Bridgeを起動します。Aは開いたままにします。起動直後はIDLEで、この時点では記録も始まりません。

この設定は640×480・30 FPSを要求し、共有H.264を使います。実測FPSはFoxgloveで確認します。

## 2. Bで起動を確認する

```bash
ros2 control list_controllers
ros2 topic echo /control_mode --once
ros2 topic echo /follower/joint_states --once
```

2つのControllerが `active`、モードが `IDLE`、関節のpositionが表示されることを確認します。[Foxglove](../foxglove_monitoring.ja.md)で映像と診断も確認できます。このProfileの監視レイアウトは `so101-monitor-h264.json` です。

## 3. Bで手動操作を開始する

切替前に、トルクOFFのアームを支持し、LeaderとFollowerを近い姿勢へ合わせます。切替後はLeaderの現在姿勢に対応する目標がFollowerへ送られます。

```bash
./scripts/mode.sh TELEOP
```

応答の `success=True` を確認し、Leaderを小さく動かします。追従方向や開閉がおかしい場合は停止して設定を確認します。

<a id="record"></a>

## 4. 必要な場合だけ、1回分を記録する

操作だけなら、この項目は省略できます。Bで次を実行すると記録が始まります。

```bash
./scripts/record.sh start '物体を箱に入れる' 'operator'
```

応答の成功とEpisode IDを確認します。作業が成功したら記録を閉じます。

```bash
./scripts/record.sh stop true
```

失敗なら代わりに `./scripts/record.sh stop false '失敗理由'` を使います。保存先は `data/bags/EPISODE_ID/` です。正常終了した記録の `episode.json` は `status: complete` になります。

## 5. 終了する

記録中なら先に閉じ、アームを支持します。Bで待機へ戻します。

```bash
./scripts/mode.sh IDLE
```

続いて**AでCtrl+C**を押し、Runtimeが終了してコマンドを入力できる状態へ戻ることを確認します。IDLEや終了時はトルクがOFFになるため、アームの自重に注意します。

異常な動きでは通常の終了操作より停止を優先します。Software E-stopは `./scripts/estop.sh`、PC・USBに依存しない停止は確認済みの駆動電源遮断です。

<a id="reconnect"></a>

## USBを抜き差しした後に再開する

記録を閉じ、アームを支持してRuntimeを終了してから機器を再接続します。by-idの対応を確認し、必要なら[Probe](hardware.ja.md#probe)で測定します。その後、このページの手順1からRuntimeを起動し直します。

USBの再接続だけで、動作中の全ノードが自動復旧する前提にはしていません。FAULTやControllerのinactiveが残る場合は[停止原因の確認](../troubleshooting.ja.md)を先に行います。

**完了の目安：** 操作を終了し、記録はcomplete、Runtimeは停止している。

次は[連続して作業を収集する](../collection_script.ja.md)、または[記録をDatasetへ変換する](dataset.ja.md)へ進みます。
