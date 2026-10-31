# Foxgloveでカメラと機器の状態を見る

[READMEへ戻る](../README.md)

**このページのゴール：** 起動中のRuntimeへ操作用PCのFoxgloveを接続し、カメラと機器の状態を一覧で見ます。収集中は[収集用レイアウトと手順](collection_script.ja.md#3-macのfoxgloveを接続する)を使います。

以下は、作業別手順で使う `config/runtime/system.real30hz.640x480.h264.yaml` に合わせた監視用レイアウト [so101-monitor-h264.json](../config/foxglove/so101-monitor-h264.json) の接続方法です。カメラ3台と機器の診断をまとめて表示します。

`collect.sh` で作業を収集するときは、代わりに[収集用レイアウトの接続手順](collection_script.ja.md#3-macのfoxgloveを接続する)へ進みます。収集用は本数・残り時間も表示します。

## 操作用PCから接続する

操作用PCのターミナルで、レイアウトをダウンロードします。

```bash
scp 'robot-user@robot-host:/path/to/so101-ros2-vla/config/foxglove/so101-monitor-h264.json' \
  ~/Downloads/so101-monitor-h264.json
```

続いて、同じ操作用PCでSSHトンネルを開きます。[GUI接続](guide/desktop.ja.md)ですでに8765を転送している場合は、追加のSSH接続は不要です。まだの場合だけ、次を実行します。このコマンドは接続中、そのまま待機します。

```bash
ssh -N -T \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -L 127.0.0.1:8765:127.0.0.1:8765 \
  robot-user@robot-host
```

1. 操作用PCの最新の [Foxgloveデスクトップアプリ](https://foxglove.dev/download) を開き、必要ならサインインします。
2. **Open connection → Foxglove WebSocket** を選び、`ws://127.0.0.1:8765` に接続します。
3. **Layouts → Import from file…** で、ダウンロードした `so101-monitor-h264.json` を読み込みます。

Runtimeは `foxglove:=true` が既定なので、Bridgeを追加起動する必要はありません。
8765はFoxglove用、6080はnoVNC用です。6080のトンネルだけではFoxgloveへ接続できません。
SSH先、プロジェクトの場所が変わった場合はコマンドも合わせて変更してください。

## 画面の見方

| 場所 | 表示するもの |
|---|---|
| 上段 | top・side・armのカメラ映像を同時表示 |
| 左下「機器の状態」 | カメラ、Leader、Follower、コントローラーなどの診断一覧。深刻な状態から並ぶ |
| 中央下の診断タブ | カメラ別のFPS・経過時間・撮影サイズと、グリッパーの状態 |
| 右下 | 現在のモードと、Leader/Followerの関節値・メッセージ時刻 |

一覧は **OK＝正常、WARN＝警告、ERROR＝異常、STALE＝更新が途絶えた状態** です。
このレイアウトでは、最後の診断から5秒経過するとSTALEとして扱います。
診断を一度も送っていない機器は一覧に現れません。起動時はカメラ3台とLeader/Followerが揃っているかも確認してください。
診断行をクリックすると、その機器の詳細パネルを開けます。

カメラの値は次のように読みます。

| 値 | 意味 |
|---|---|
| `actual_fps` | カメラNodeが実際に配信した1秒あたりの撮影枚数 |
| `frame_age_s` | ホストでフレームを取得してから診断を作るまでの秒数。操作用PCへの表示遅延は含まない |
| `dropped_frames` | 取得済みの画像をROS配信処理が取り出すまでに飛ばしたフレームの累積数。USB全体の欠落数ではない |
| `capture_decode_ms` | ホスト取得から画像の展開・RGB変換完了までのms |
| `publish_wait_ms` | RGB変換完了からROS配信処理開始までの待ち時間（ms）。実機は取得完了通知で配信する |
| `width` / `height` | 収集用画像の実際の幅と高さ |

撮影枚数が設定FPSの80%未満だと、現在のカメラNodeはWARNを出します。
たとえば30 FPS設定で実測14.8 FPSなら、映像が見えていてもWARNになります。
映像が止まっても最後の画像が残ることがあるので、診断と合わせて確認します。

関節値は `name` と `position` の同じ添字が対応し、単位はradです。
トピックや値が表示されても、配信が続いているとは限りません。`header.stamp` の更新も確認します。
特に `/follower/joint_states` はros2_controlからの配信なので、コントローラー停止時には最後の値が残る場合があります。
速度・力の計測インターフェースを使っていない現在の構成では、`velocity` / `effort` のNaNは正常です。

中央下の「グリッパー」タブでは、Followerの診断を表示します。更新したレイアウトを使う場合はJSONを操作用PCへ再取得し、再インポートしてください。

| 値 | 意味 |
|---|---|
| `gripper_load_percent` | 負荷の指標。サーボの駆動出力を%表示し、接触力そのものではない |
| `gripper_position_rad` | 実測した開度 |
| `gripper_target_rad` | 駆動中に送っている目標開度 |
| `gripper_overload_reported` | 直近または前回診断以降に、グリッパーが過負荷を通知したか |
| `servo_status` | 検出したアラームbit。グリッパー単独の `0x20` はLeRobot profileではWARN |

物を挟んでいる間、実測開度と閉じ目標に差が出るのは自然です。WARNを見て閉じ指令を少し緩める判断に使えますが、サーボの保護で保持力が下がる場合もあるため、物体が保持できているか映像も確認します。Leaderへ反発力を返す機能はありません。

## 表示解像度と収集解像度

この手順のH.264設定では、録画と共通の圧縮映像を表示します。表示のために別の動画エンコードは増えません。
Foxgloveのパネルを拡大しても、収集用の撮影サイズやモデル入力サイズは変わりません。
撮影サイズは使用するSystem設定が参照するCamera YAMLで指定します。

Raw画像の設定 `config/runtime/system.real30hz.640x480.yaml` などを使っている場合だけ、ダウンロード・インポートするレイアウトを [so101-monitor.json](../config/foxglove/so101-monitor.json) へ変えます。こちらは `/camera/{top,side,arm}/preview/compressed` のJPEGを表示し、表示サイズはCamera YAMLの `preview` 欄で指定します。

このレイアウトは表示パネルだけで構成しており、モード変更やトルク操作のボタンはありません。
接続を閉じてもRuntimeは継続します。

**完了の目安：** 3カメラの映像とLeader/Followerの診断が更新されている。接続を閉じるときは、ロボットの動作・Runtimeの終了をそれぞれの操作手順で行ってから閉じます。

## 接続できない場合

- SSHが `Address already in use` を出したら、操作用PCの8765を既存トンネルなどが使用しています。既存トンネルが同じLinuxホストへ転送しているなら、そのまま利用できます。
- Foxgloveが接続できなければ、SSHターミナルが開いているか、LinuxホストでRuntimeが起動しているかを確認します。
- `Missing expected sec-websocket-protocol header` なら、現在のBridge 3.xに対応した最新のFoxgloveアプリを使います。一般のWebSocketクライアントや古いStudioでは接続できない場合があります。
- 接続できても映像がなければ、Imageパネルのトピックと `/diagnostics` のカメラの状態を確認します。

公式資料：[レイアウトのインポート](https://docs.foxglove.dev/docs/visualization/layouts)、[診断パネル](https://docs.foxglove.dev/docs/visualization/panels/diagnostics)、[Foxglove Bridge](https://docs.foxglove.dev/docs/fleet/bridge)。
