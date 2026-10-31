# SO-101実機受入手順：校正後に少しずつ動かして確認する

機器や作業の値は、[公開用設定とローカル設定](public_repository.ja.md)に従って `*.local.yaml` へ保存します。手順中のホスト名・パス・USB識別名は例です。

操作端末は統合コンテナのMATE GUIです。ターミナルで `cd /workspace` を実行し、以降のスクリプトとROSコマンドを直接入力します。初回のDocker準備は[環境構築](setup.ja.md)で行います。

**実機受入は、校正値を保存したあとに、姿勢・動く方向・停止・周期・記録を実物で確認する作業です。** 校正JSONの存在、Probeの終了コード0、TELEOPへの切替成功は、それぞれ確認の一部です。これらだけで全工程が完了したとは扱いません。

初回、サーボ交換、ホーン付け直し、原点や範囲の変更、校正ファイルの由来が不明な場合は、まず[キャリブレーション手順](calibration.ja.md)で測定・apply・check・Probeを済ませます。同じ実機の有効な校正を再利用する場合も、保存値と実物の対応を確認します。

状態を読む経路と目標を送る経路は別です。実機の状態が届いていても、Controllerの起動や安全処理に失敗している場合は動作できません。[構成](architecture.ja.md)と[制御](control.ja.md)を参照し、各段階の確認を進めます。

## 作業順とターミナル

1. Runtimeを起動せず、接続・校正一致・位置制限・停止手段を確認する。
2. 起動前確認を終えたことを `hardware_qualified` に記録する。
3. ターミナルAで実機Runtimeを起動し、ターミナルBでIDLEと状態を読む。
4. 初期姿勢を合わせてから小さなTELEOPを試す。
5. 周期・停止保持を確認する。
6. 短い記録と変換を試してからデータ収集へ進む。

各ターミナルで最初に実行します。

```bash
cd /workspace
```

ターミナルAはRuntimeのログを表示し続けます。Bは状態確認・モード切替用です。Aでログが流れている間に、そこへ別のコマンドを打ち込みません。

<a id="before-motion"></a>

## 1. 起動前：接続・校正・位置制限を確認する

### 1-1. 機器の割当を確認する

次のby-id末尾はプレースホルダーです。ローカル設定に登録した実物の識別名へ置き換えて確認します。`ttyACM0` や `/dev/video0` の番号は、再接続などで変わるため固定の割当ではありません。

| 機器 | 設定されたUSB ID末尾 |
|---|---|
| Leader | `YOUR_LEADER_SERIAL` |
| Follower | `YOUR_FOLLOWER_SERIAL` |
| Arm camera | `YOUR_ARM_CAMERA-video-index0` |
| Side camera | `YOUR_SIDE_CAMERA-video-index0` |
| Top camera | `YOUR_TOP_CAMERA-video-index0` |

実物との照合方法は[校正手順4](calibration.ja.md#ports)にあります。2台を支持し、適合するモーター電源・USB・カメラを接続します。他の実機操作プログラムは終了します。

### 1-2. Probeの結果を確認する

校正後のProbe結果がない場合は実行します。すでに実施した場合は、結果を読み直します。

```bash
./scripts/probe.sh \
  --config /workspace/config/runtime/system.real30hz.yaml \
  --output /workspace/data/logs/hardware_probe_acceptance.json
```

```bash
cat data/logs/hardware_probe_acceptance.json
```

両アームに `error` がなく、`calibration_matches: true`、6関節の角度が取得でき、Followerの `inside_configured_limits: true` を確認します。各項目の意味と未達時の対応は[校正手順9](calibration.ja.md#probe)を参照します。Probeは目標位置・トルク設定を書き込まず、トルクONが残っていても解除しません。

### 1-3. LeRobot方式の範囲と指令設定を確認する

標準は `control_profile: lerobot` です。[LeRobot互換設定](lerobot_compatibility.ja.md)の対応表を確認します。

| 設定 | 標準の意味 |
|---|---|
| `limits.<関節>.min/max` | 校正JSONからapplyで算出した表示・URDF用の位置範囲 |
| `velocity` / `acceleration` | 旧bounded方式用。LeRobot方式では指令へ作用しない |
| `follower.max_relative_target` | 標準null。指定時だけ現在位置との目標差をLeRobot単位で制限 |
| グリッパーとTeleop倍率 | 閉0%〜開100%の対応を、測定した実角度へ換算 |

回転関節の0 radは校正範囲の中点です。一律±1.5 radや0.05 radのステップ制限は標準では使用しません。LeRobotのdegree方式は校正範囲へソフトで指令を切り詰めないため、範囲内でも起こり得る机・物体・ケーブルとの干渉を実物で確認します。

## 2. 起動前：支持と電源停止を確認する

トルクがOFFになった場合にも倒れない支持、台座の固定、作業空間を確認します。Followerへの駆動電源を、PC・ROS・USBに依存せず停止できる方法を実物で確認します。USB断時にはサーボへTorque OFFを送れないことがあるためです。

`/physical_estop` はROSへの停止状態通知です。Topicを用意しただけでは電源遮断回路になりません。実物の停止回路とその通知方法は、使用している装置に合わせて確認します。

URDF・TF・MuJoCoの寸法とカメラ位置は初期の近似値です。実機の寸法・位置を反映するまでは、これを衝突回避や作業範囲の安全性の根拠にしません。

## 3. 起動前確認の完了を設定へ記録する

手順1〜2を人が確認したあと、GUIのエディターで `config/hardware/robot.yaml` の既存の項目を次へ変更して保存します。末尾へ同じキーを追加しません。

```yaml
hardware_qualified: true
```

この値は「起動前の確認を人が行った」という記録です。trueにすること自体が確認の代わりにはなりません。以降の動作・停止・記録の受入はまだ続きます。再校正、ID設定、apply、restoreを行うとfalseへ戻るので、起動前確認を再実施します。

## 4. 実機RuntimeをIDLEで起動する

### 4-1. ターミナルA：Runtime起動

```bash
./scripts/teleop.sh real \
  config:=/workspace/config/runtime/system.real30hz.yaml
```

`real` は実際のサーボとカメラを使う指定です。`mock` の成功では実機の通信や取り付けを確認できません。起動したらAはログ表示用に残します。実機Runtimeは起動時にサーボの動作モードなどを設定します。読み取りだけの確認にはProbeを使います。

### 4-2. ターミナルB：同じROS環境を使う

統合GUIでは、Bも同じROSとWorkspaceを使います。`cd /workspace` を実行し、以降は `ros2` を直接入力します。

```bash
```

### 4-3. ターミナルB：状態確認

1つずつ実行します。

```bash
ros2 control list_controllers
```

`joint_state_broadcaster` と `forward_position_controller`（bounded方式では `joint_trajectory_controller`） が `active` になるまで起動ログを確認します。`inactive` や `unconfigured` のままなら、モード切替へ進まず原因を調べます。

```bash
ros2 topic echo /control_mode --once
```

起動後は `data: IDLE` を確認します。FAULT/ESTOPなら停止原因を確認します。

```bash
ros2 topic echo /follower/joint_states --once
```

6関節の `position` を確認します。現在の位置専用インターフェースでは `velocity` と `effort` が `.nan` の場合があります。これだけで校正失敗とは判断しません。一方、positionが不正、状態が更新されない、継続的な通信エラーがある場合は未完了です。

```bash
ros2 topic echo /diagnostics
```

継続表示です。関連ノードの `name`、`level`、`message` を読み、確認後はBでCtrl+Cを押して表示だけ終了します。これでAのRuntimeは終了しません。

## 5. 初期姿勢を合わせ、小さなTELEOPを試す

**モード切替直後もLeaderの現在姿勢がFollowerの目標になります。** 校正が一致していても、2台が大きく違う姿勢なら切替直後から追従しようとします。

IDLEでトルクOFFであることを確認し、支持しながら2台を近い姿勢へ合わせます。Followerの現在位置が使用範囲内にあることも確認します。先端のひねり方向とグリッパー開度も対応させます。

支持・停止手段・作業空間を確保して、Bで実行します。

```bash
./scripts/mode.sh TELEOP
```

`success=True, message='TELEOP'` は、モード変更を受け付けたという意味です。その後も停止していないことを確認します。

```bash
ros2 topic echo /control_mode --once
```

`data: TELEOP` を確認してから、Leaderを1関節ずつごく小さく動かします。

| 確認対象 | 正しい対応の確認 |
|---|---|
| 台座・肩・肘・手首の曲げ | 意図したFollowerの関節が、対応する方向へ小さく動く |
| 手首のひねり | 先端の向きが対応し、ケーブルに無理がない |
| グリッパー | Leaderを閉じる操作でFollowerも閉じ、開く操作で開く |
| 動作全体 | 突然の跳び、不自然な姿勢差、振動、干渉、繰り返すFAULTがない |

最初は物を挟まず、広い動作へ進む前に方向を確認します。異なる関節が動いたり逆方向へ動いたりした場合は、動かし続けず停止し、ID・組立・基準姿勢・teleop mappingを見直します。

通常の動作終了は支持を確保してIDLEです。

```bash
./scripts/mode.sh IDLE
```

異常時のソフトウェア停止は、Runtimeが動いているBから次を実行します。差し迫った危険には、用意した給電停止方法を使います。

```bash
./scripts/estop.sh
```

## 6. 周期と停止保持を確認する

### 6-1. 動作周期

Aのoverrun・通信エラーと、Bの `/diagnostics` でFollowerの読み取り、制御・Teleop周期、カメラFPSを確認します。標準60 Hzは1周期約16.7 msです。カメラとDatasetの30 Hzとは別の周期です。

60 Hzを維持できない場合は、読み取り・書き込み・画像処理の負荷を調べます。制御周期を下げるなら、Runtimeを終了して `robot.update_rate_hz`、`controllers.update_rate`、`teleop.rate_hz`、`safety.rate_hz` をそろえて変更し、再起動後に検証します。タイムアウトだけを広げて異常を見えなくしないようにします。

### 6-2. 停止と再開

支持・停止手段を準備した状態で、まずSoftware E-stopの停止保持を確認します。停止中の通常モード切替が拒否されること、原因を解消してResetした後はIDLEへ戻ることを確認します。

```bash
./scripts/reset.sh
```

```bash
ros2 topic echo /control_mode --once
```

Resetは停止保持を解除してIDLEへ戻す操作です。Followerの新しい状態が届き、物理停止通知が解除されている必要があります。自動でTELEOPへ復帰しません。再開する場合は初期姿勢も再確認して、モードを改めて選びます。

指令Source停止、Leader USB断、Follower通信断でも停止を保持することを、条件を分けて確認し、結果と停止理由を記録します。最初にmockで異常時の操作を練習し、実機で通信断を確認する段階では給電を確実に止められる条件を整えます。通電中のサーボ間ケーブルの抜き差しで試験しません。異常の注入方法・実施した条件・結果を残し、未実施の項目は未確認とします。

FAULT/ESTOPの理由はAのSafetyログと `/diagnostics` にあります。原因が残ったままResetを繰り返しません。Controller自体がinactive/unconfiguredなら、SafetyのResetだけでは復旧しないので、記録を閉じてAをCtrl+Cで終了し、再起動後にactiveを確認します。

<a id="recording"></a>

## 7. 30 Hzの記録と変換を短いEpisodeで確認する

姿勢・方向・停止の確認後に、[記録手順](guide/teleop.ja.md#record)で短いEpisodeを記録します。最初から長時間収集しません。記録を正常に閉じ、Topic・時刻・枚数・欠損・ファイルサイズを確認してから[Dataset変換](data_storage_video.ja.md)を試します。

最低30 Hzの要件には `config/runtime/system.real30hz.yaml` を使い、3カメラ同時の実測を確認します。Runtimeを終了して、GUIで実行します。

```bash
./scripts/probe.sh \
  --cameras-only \
  --samples 300 \
  --config /workspace/config/runtime/system.real30hz.yaml \
  --output /workspace/data/logs/camera_probe_acceptance.json
```

設定30 FPSでも実測20 FPSなら要件未達です。平均FPSだけでは長時間の欠損を判断できません。Raw画像の時刻・枚数・間隔と、Datasetへの同期結果も確認します。DatasetのFPSを上げたり同じ画像を複製したりして、実際の取得頻度を達成したことにはしません。

Bagを読む場合は、Runtimeを終了したあと、GUI内のターミナルで次を実行します。`実際のEPISODE_ID` は記録で作られたディレクトリー名へ置き換えます。

```bash
ros2 bag info /workspace/data/bags/実際のEPISODE_ID/raw
```

再生には別のROS Domainを使います。記録には指令Topicもあるため、実機と同じDomainへ再生しません。

```bash
ROS_DOMAIN_ID=74 ros2 bag play /workspace/data/bags/実際のEPISODE_ID/raw
```

この再生コマンドはGUI内でCPU/GPU共通です。Domain 74は、既定42とは異なる例です。実機のDomainを変更している場合は、それと異なる番号を選びます。確認後はCtrl+Cで再生を終了します。

## 8. 受入結果を記録し、学習へ進む

実施日、対象アームと校正バックアップ名、設定、Probeの保存先、各関節の方向・追従、停止試験の条件と結果、周期、カメラ実測、Episodeと変換結果を記録します。未確認の項目と問題も残します。

確認がそろったらデータ収集・ACT学習へ進みます。モデルはmockで先に動かし、実機推論は低速度・短いEpisodeから評価します。[独自Dataset・学習・推論](data_storage_video.ja.md)に手順があります。

## 受入の記録を次の収集へ引き継ぐ

実行日時、Profile、校正バックアップ、Probeの出力、試した関節と姿勢、周期・停止試験の結果を保存します。`hardware_qualified: true` だけでは、どの条件で確認したかを後から説明できません。

受入後は[テレオペ・収集](guide/teleop.ja.md)で短いEpisodeを作ります。30 HzのDatasetが生成できた場合も、Rawの画像が実測30 FPS以上かを別に調べます。モデル作成は[変換・学習・デプロイ](learning/07-models-and-learning.ja.md)、独自機体への移植は[応用](custom_robot.ja.md)へ進みます。
