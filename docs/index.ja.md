# 手順書・参考資料の一覧

[READMEへ戻る](../README.md)

操作する場合は「作業別の手順」から1ページを選びます。仕組みや設定を調べる場合は、その下の参考資料を使います。

初学者向けに理由と仕組みを順に読む場合は、[9つの教材](learning_path.ja.md)から始めます。操作手順の各ページからも対応する教材へ進めます。

## 作業別の手順

| 作業 | ページ |
|---|---|
| 初回の環境構築 | [Docker・ROSを準備する](setup.ja.md) |
| 操作用PCから操作画面へ入る | [GUIへ接続する](guide/desktop.ja.md) |
| 実機なしの練習 | [mockで起動・記録・停止](guide/mock.ja.md) |
| 実機を初めて使う・変更後に確認する | [接続と校正を確認する](guide/hardware.ja.md) |
| 記録せずに動かす | [テレオペだけ行う](guide/teleop.ja.md) |
| 作業を連続して記録する | [Foxgloveを見ながら収集する](collection_script.ja.md) |
| 映像と機器の状態を見る | [Foxgloveを接続する](foxglove_monitoring.ja.md) |
| 同期映像・関節・遅延・EE軌跡と全実演を比較する | [記録データを確認する](recording_review.ja.md) |
| 記録から学習データを作る | [Datasetを作る](guide/dataset.ja.md) |
| 学習済みモデルで自律動作する | [モデル起動・準備確認・開始・停止](guide/inference.ja.md) |
| 新しいモデルを学習する | [手法を選ぶ](methods/index.ja.md) → ACT／RTC／Training-time RTC／VLASH／A2C2 |
| 通常学習とVLASH・平滑化の効果を比較する | [対話ベンチマーク・4条件の比較と集計](four_condition_comparison.ja.md) |
| エラーを調べる | [症状から調べる](troubleshooting.ja.md) |

## 作業中に必要になったときの専用手順

| 必要な作業 | ページ |
|---|---|
| 校正の再測定、サーボIDの設定、復旧 | [キャリブレーション](calibration.ja.md) |
| 実物の支持・停止・動作・記録の確認 | [実機受入](hardware_acceptance.ja.md) |
| GUIのパスワード・画面・コンテナ管理 | [GUIの管理](../gui/README.md) |

## 仕組みを調べる参考資料

| 調べたいこと | ページ |
|---|---|
| 公開範囲・環境固有値・Git除外 | [公開用設定とローカル設定](public_repository.ja.md) |
| 依存パッケージ・モデル重みのライセンス、配布時の表示 | [ライセンスと配布](licenses.ja.md) |
| 設定の選び方・変更の反映・保存場所 | [設定ファイルと追加機能](configuration.ja.md) |
| 画像圧縮、timestamp、MCAP・MP4・NPZ | [時刻同期とデータ保管](data_storage_video.ja.md) |
| Node、Topic、Service、QoSなど | [ROS通信の教材](learning/02-ros-communication.ja.md) |
| ROSを順に学ぶ教材 | [学習経路](learning_path.ja.md) |
| ロボット・カメラ・制御のつながり | [構成](architecture.ja.md)と[制御](control.ja.md) |
| SO-101とLeRobotの動作設定の対応 | [LeRobot互換設定](lerobot_compatibility.ja.md) |
| カメラの取得・配信遅延を測る | [カメラの計測](camera_latency.ja.md) |
| モデルの入力・損失・検証 | [モデルと学習の教材](learning/07-models-and-learning.ja.md) |
| Dataset・モデルの保存形式 | [データ保管](data_storage_video.ja.md) |
| 独自ロボットへ応用する | [移植と1関節教材](custom_robot.ja.md) |
| 実装と検証の範囲 | [実装状況](implementation_status.ja.md)、[テストの実行](validation.ja.md) |
