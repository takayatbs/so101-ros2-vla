# 設定ファイル・保存先・追加機能の参考資料

機器や作業の値は、[公開用設定とローカル設定](public_repository.ja.md)に従って `*.local.yaml` へ保存します。手順中のホスト名・パス・USB識別名は例です。

理由や仕組みから学ぶ場合は、[設定の読込順と反映方法の教材](learning/04-configuration-and-runtime.ja.md)を併読できます。

[READMEへ戻る](../README.md) ／ [手順書・参考資料の一覧](index.ja.md)

通常の操作で順に読む必要はありません。設定変更や開発時に、必要な項目を参照します。

## 設定ファイルをどこから読むか

`config/runtime/system*.yaml` は、実際に読む設定ファイルを列挙した入口です。[用途別フォルダーの一覧](../config/README.md)から探せます。現在の作業別手順では、次の設定を使います。

| 用途 | System YAML | カメラ・保存 |
|---|---|---|
| 初回のmock練習 | `config/runtime/system.real30hz.640x480.yaml` | 640×480、Raw画像。NVENCを使わない |
| Linuxホストで実機収集・通常のACT | `config/runtime/system.real30hz.640x480.h264.yaml` | 640×480、共有NVENC H.264 |
| RTC系・VLASH | `config/runtime/system.real30hz.640x480.手法名.yaml` | H.264と手法別Policy。専用手順で選ぶ |

H.264のSystemは `config/cameras/camera.640x480.h264.yaml`、`config/dataset/dataset.h264.yaml`、共通の `config/hardware/robot.yaml` と `config/observation/observation.yaml` を読みます。撮影解像度はCamera YAML、AIの入力サイズはObservation YAMLで別々に指定します。YAML変更はRuntimeを終了してから編集し、再起動して反映します。

旧Profileの `config/runtime/system.real30hz.yaml` は1080pのRaw画像、`config/runtime/system.real20hz.yaml` は過去の20 Hz測定用です。名前だけで判断せず、Systemが参照するファイルを確認します。

| 設定 | 内容と、変更を検討する場面 |
|---|---|
| `config/hardware/robot.yaml` | シリアル、関節順、位置・速度・加速度制限、グリッパー換算、実機受入。機器や関節設定を合わせる |
| `config/hardware/controllers.yaml` | ros2_controlの関節と周期。ロボット制御周期を調整する |
| `config/training/training.yaml` | 独自ACTのモデル構成、学習率、損失、検証分割、保存周期 |
| `config/control/teleop.yaml` | 関節対応、Scale、Offset、周期、Watchdog。LeaderとFollowerの対応を合わせる |
| `config/cameras/camera.640x480.h264.yaml` / `config/cameras/camera.640x480.yaml` | Capture解像度、FPS、USB識別名、Preview。カメラ交換や実測FPSへの調整 |
| `config/observation/observation.yaml` / `config/observation/observation.real20hz.yaml` | AIが使うカメラ、前処理、同期条件。入力画像とモデルの仕様を決める |
| `config/dataset/dataset.h264.yaml` / `config/dataset/dataset.yaml` | 記録Topic、Dataset FPS、同期、変換メモリー上限。収集・変換条件を決める |
| `config/control/safety.yaml` | 制御周期、Watchdog、急変処理、最低空き容量。停止条件と指令制限の動作を決める |
| `config/policy/policy.yaml` / `config/policy/policy.real30hz.yaml` | デバイス、モデル保存先、task、推論処理周期、Chunk設定。モデル実行条件を決める |

Profile名だけでなく、参照する各YAMLの内容が一致している必要があります。カメラやResizeを変える場合は、既存モデルとの互換性を確認し、必要に応じてDataset生成・学習からやり直します。

## プログラムとデータの配置

ROSパッケージは9つです。`so101_hardware` は通信とカメラ、`so101_teleop` は手動操作、`so101_safety` は指令選択と安全チェック、`so101_dataset` は記録管理、`so101_policy` はモデル実行、`so101_bringup` は全体起動と診断を担当します。`so101_msgs` はメッセージ、`so101_description` はロボット形状、`so101_core` はROSへ依存しない共通処理を持ちます。

| 場所 | 保存するもの |
|---|---|
| `data/bags` | 元のEpisode、Raw MCAP、記録メタデータ |
| `data/datasets` | 変換したDatasetと教材の由来 |
| `data/models` | 実行用モデル、正規化統計、登録情報 |
| `data/logs/training` | 学習結果とログ |
| `data/logs/system` | 保存されたビルド・試験ログ |
| `data/logs/hardware_probe*.json` | 実機・カメラのProbe結果 |

## CPU専用環境と学習を含む試験

CPU専用PCでは `.env` を `GPU=0` にします。x86_64のCPU wheelを使う場合は `TORCH_INDEX=https://download.pytorch.org/whl/cpu` を選び、イメージを再ビルドします。`train.sh` には `--device cpu` を指定し、モデル実行には使用するProfileのPolicy設定（この手順では `config/policy/policy.real30hz.yaml`）の `device` も `cpu` に変更します。

CPU専用環境ではNVENCを使えないため、Camera・DatasetもRaw設定の `config/runtime/system.real30hz.640x480.yaml` などへそろえます。Flow経路のLeRobot書き出しには `--encoder libx264` を指定します。

GUI内のコマンドはCPU/GPUで共通です。GPU割当の変更はホストでコンテナを再作成し、学習とPolicyの計算先をそれぞれ指定します。

学習・モデル出力まで含める試験は、Runtimeなどを終了してから次を実行します。短いCPU学習で、実機は駆動しません。

```bash
RUN_ML_TESTS=1 ./scripts/test.sh -q
```

## MuJoCo・他のモデル・未実装機能

通常の短い試験に加え、1080p・3カメラ・30 Hzのmockを120秒操作し、2秒のMCAP記録まで確認する継続試験もあります。Runtimeなどを終了してから実行します。保存領域を約1 GB以上使用する場合があります。

```bash
RUN_SOAK_TESTS=1 ./scripts/test.sh -m soak -q
```

この試験は通常はskipされます。実行した環境で結果を保存し、[検証手順](validation.ja.md)に沿って対象範囲を確認します。合成画像を使うため、実機カメラの30 FPS達成は別途ProbeとRaw記録で確認します。

MuJoCoの制御経路は、mock起動の代わりに次を使って確認できます。Controllerのactive確認後にTELEOPを選びます。

```bash
./scripts/teleop.sh mujoco \
  config:=/workspace/config/runtime/system.real30hz.640x480.yaml
```

従来の独自ACTは `so101_dataset_v2`（旧PNG形式v1も読込可）と `so101_act_v1` を使用します。[手法別の入口](methods/index.ja.md)には、LeRobotDataset v3.0の書き出しと `so101_flow_v1` の経路もあります。[モデル選択](methods/models.ja.md)の8モデルは `so101_lerobot_v1` で登録します。公式の `config.json` とprocessorを保ち、SO-101固有の情報を `so101_adapter.json` に保存します。外部データには単位・関節順・校正・前処理の対応を確定する必要があります。

Episode分割・正規化・損失は[モデルと学習の教材](learning/07-models-and-learning.ja.md)、Dataset・Checkpoint・登録情報は[データ保管](data_storage_video.ja.md)を参照してください。SmolVLAなどの追加モデルと言語条件付けは [モデル選択](methods/models.ja.md)を参照してください。再開学習のCLIは未実装です。
