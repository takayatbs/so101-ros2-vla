# 困ったときに確認すること

[READMEへ戻る](../README.md) ／ [手順書・参考資料の一覧](index.ja.md)


まずコマンド応答の `success` と `message` を確認し、RuntimeのターミナルAと `/diagnostics` で原因を調べます。

| 症状・メッセージ | 最初に確認すること |
|---|---|
| GUI内で `docker: command not found` | Dockerのbuild/up/downはホスト作業。GUI内のタスク用スクリプトはDockerなしで直接動く統合版を使う |
| `dev` で機器へアクセスできない | dialout/videoの数値グループID、USBの権限、Composeのデバイス設定を確認。GUIをrootに変更しない |
| SSHの `Permission denied` | `operator@接続先` になっているか。Linuxホストの認証を使っているか。VNCと操作用PCのパスワードを混同していないか |
| `/workspace` や統合環境の印がない | 旧GUIに接続していないか。ホストで統合版への移行と起動イメージを確認する |
| GUIのROSにロボットのTopicがない | AでRuntimeが起動しているか、各端末のROS_DOMAIN_IDが同じか、ビルド後の環境が読み込まれているか |
| `Select an active control mode before recording` | 現行実装はIDLEでの記録開始を拒否する。状態・停止手段を確認し、TELEOPまたはAUTONOMOUSを明示的に選ぶ |
| 起動しても動かない | IDLEのままではないか、両Controllerがactiveか、モード切替が成功したか |
| モード切替が拒否される | ESTOP/FAULTが保持されていないか、Followerの新しい状態が届くか、現在位置が制限内か |
| `Return to IDLE before switching command sources` | TELEOPとAUTONOMOUSなどの切替はIDLEを経由する |
| 実機起動が拒否される | 起動前の受入確認と `hardware_qualified` の設定 |
| `Missing motor IDs` | 駆動電源、配線、アーム割当、ポート使用、モーターID・通信設定 |
| `Calibration mismatch` など | その実機のキャリブレーションと保存JSONの一致 |
| 動作開始後にFAULT | 指令・状態のタイムアウト、Follower通信エラー、制御周期遅延 |
| `Policy not ready` | IDLEのまま `/policy/ready` がtrueになることを確認。カメラ・状態・同期と初回計算が完了しているか調べる |
| `Overrun` / `trajectory ... ends in the past` | 予定周期を超える遅延で指令が期限切れになった。停止理由とControllerのread/write時間を確認する |
| ログの `Action status ... 20.00 Hz` | ControllerのAction状態を通知する周期。カメラやDatasetの30 Hz設定とは別 |
| 記録開始・途中で容量エラー | 保存先の空き容量。設定の最低空き容量は10 GB |
| `Incomplete episode` | 正常なrecord stopができたか。JSONを手でcompleteへ変更して回避しない |
| `Failed episode requires --include-failed` | 成功Episodeを選ぶか、失敗教材を含める目的を確認する |
| Robot設定の不一致 | 記録後に`config/hardware/robot.yaml`を変更していないか |
| 同期エラー・画像不足 | カメラの実測FPS、画像欠損、USB構成、負荷、Profile |
| 変換のメモリー超過 | Episodeを短くする。現状はエピソード単位の読み込み |
| 出力先が存在する | Dataset・学習出力・モデルIDに新しいバージョン名を付ける |
| 学習ログが画面へ出ない | `data/logs/training/モデル名/metrics.jsonl` を確認する |
| `Runtime/training is already using Robot Compute` または終了コード75 | 他のRuntime・学習・Probe・テストを終了する。ロックファイル削除で回避しない |
| モデルの前処理・単位・hashの不一致 | 学習時と実行時の設定・関節順・rad単位・Checkpointを確認する |
| `ros2` や独自Packageが見つからない | 統合GUIの新しい端末を開く。ビルド後はinstall/setup.bashを読み直す |
| ROS Service待ちが終わらない | Runtimeが起動しているか、ターミナルAに起動失敗がないか。待ちはCtrl+Cで終了可能 |

停止原因を解消した後は、`./scripts/reset.sh` でIDLEへ戻し、状態を確認してから必要なモードを再選択します。

停止理由は `safety_node` のERRORログと `/diagnostics` に残ります。停止中も再公開するため、ターミナルBで次を実行し、`name: safety_node` の `message` を確認できます。複数ノードの診断が流れるので、1件だけ表示する `--once` を付けず、確認後にCtrl+Cで表示を終了します。

```bash
ros2 topic echo /diagnostics
```

両Controllerが `inactive`、またはHardwareが `unconfigured` へ移った場合、SafetyのResetだけではControllerを復旧しません。記録を閉じてRuntimeをCtrl+Cで終了し、再起動後にControllerのactiveを確認します。繰り返す場合は送信待ちや処理負荷を調べます。現在のHardware adapterは送信を別スレッドに分離し、制御ループでDDS送信を待たない構成です。

Raw Bagを再生して内容を調べる場合は、実機Runtimeと違うROS Domainを使います。記録には指令Topicも含まれるため、実機へ再生指令を流さないように分離します。

```bash
ROS_DOMAIN_ID=74 ros2 bag play /workspace/data/bags/EPISODE_ID/raw
```

この例の74は、初期Runtimeの42とは別の番号です。RuntimeのDomainを変更している場合は、重ならない番号を選びます。再生を観察するROSツールも、同じ再生用Domainへ合わせます。

