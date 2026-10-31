# 現在の実装と、確認が必要な範囲

機能の実装、mockでの接続確認、実機での作業性能を分けて記載します。実行手順は[README](../README.md)、方式の選択は[学習・実行手法の入口](methods/index.ja.md)から進みます。

| 項目 | 実装 | 確認範囲 |
|---|---|---|
| 統合GUI・Docker・ROS 2 Jazzy | 同じコンテナで操作・記録・学習 | aarch64のビルドとGUI操作。詳細は[GUI検証](validation.ja.md#gui) |
| SO-101・校正 | Serial ID固定、校正JSON照合、LeRobot制御Profile | 模擬サーボと合成校正による単位・制御の試験。実機ごとにProbe・受入が必要 |
| Teleop・Safety | 仲裁、時刻・有限値検査、Watchdog、停止保持 | mockの追従・停止。実機の停止時間は利用環境で測定 |
| カメラ | 3台、V4L2、取得バッファ2、解像度Profile | 合成画像による配信・同期試験。実機のFPS・帯域は個々の機器と設定で測定 |
| 映像・Foxglove | NVENC共有H.264、画像の必要時配信、監視レイアウト | 同じ圧縮ストリームを記録と表示へ分配。Raw/JPEGの構成も選択可 |
| 連続収集 | LeRobot風の開始・保存・やり直し、Session記録 | キー操作、MCAP、終了処理、採用Episodeだけの変換をmockで確認 |
| 時刻同期・Dataset v2 | 整数ns、因果的な直前値選択、時刻誤差・欠損保持 | MCAP→MP4＋NPZ、元映像の再圧縮なし。USBカメラは取得直後のホスト時刻 |
| LeRobotDataset v3.0 | Parquet・CFR MP4・統計・元timestamp列 | ローカルのLeRobot 0.6.2本体で読み込み確認。欠損はEpisode分割で保持 |
| ACT | 独自CNN・Transformer・CVAE、Standard Scheduler | 小規模学習、登録、ROS出力。既存の経路を維持 |
| Flowモデル | 画像と関節状態を使うrectified flow、学習・評価・登録 | CPUとCUDAで小規模学習。単一タスク、外部重みなし |
| RTC | soft-prefixの勾配誘導、非同期の動作列実行 | 誘導計算、遅延中の継続、経過した動作の破棄、ROS出力 |
| Training-time RTC | clean prefix、postfixのみの損失、推論prefix固定 | 損失・勾配の試験、小規模学習、ROS出力 |
| VLASH | 状態とactionの共同時刻ずらし、将来状態での推論 | 学習区間のずらし、確定済み絶対位置指令による状態見積もり、ROS出力 |
| A2C2 | 固定ベースモデル、最新観測の特徴と残差教師、小型MLP | 補正器学習、RTC＋A2C2・VLASH＋A2C2のROS出力、モデル取り違え拒否 |
| 非同期停止 | 世代管理、推論時間予算、動作列枯渇時の出力停止 | モード変更後の古い結果・例外の破棄、失敗後に出力を再開しない試験 |
| モデル登録 | schema・校正・前処理・同期・重みのhash | 独自ACT/Flow・LeRobotモデルの登録とRuntime照合。学習時のRuntime設定を保存し、補正器はベースの指紋も照合 |

手法の確認方法は[手法追加の検証記録](validation.ja.md#methods)に記載します。試験は小規模な合成データで処理経路を確認します。実機の把持成功率や論文の性能は別途評価します。

## モデルとデータの適用範囲

独自小型flowに加え、ACT・Diffusion・SmolVLA・π₀・π₀.₅・π₀-FAST・GR00T・XVLAを選ぶLeRobotアダプターを追加しました。対応する手法、依存導入、検証の範囲は[モデル選択](methods/models.ja.md)に記載します。登録モデルは1タスクを扱い、VLAにはタスク文も入力します。データには単位・関節順・校正・前処理の対応が必要で、その対応を `so101_bridge.json` と `provenance.json` に残します。

ACTは従来のNative Datasetを読み、Flow経路ではNativeまたは本プロジェクトが書き出したLeRobotDatasetを読めます。再開学習のCLIは実装していません。

## 現場や追加開発で確認すること

- 実機でのタスク成功率、推論・補正時間、負荷時の制御周期。短い試験用モデルを作業用モデルとして扱わない。
- 独立した駆動電源遮断、停止時間、実測に基づく制御設定。[実機受入手順](hardware_acceptance.ja.md)を使う。
- 露光時刻そのものの取得、ハードウェア同期。現在のUSBカメラのホスト時刻を露光時刻と同一視しない。
- 正確なCAD・Collision、MoveIt Planner、MuJoCoの実機Dynamicsと接触・映像環境。

各機能の確認方法は[検証手順](validation.ja.md)、NodeとTopicの対応は[構成](architecture.ja.md)を参照してください。
