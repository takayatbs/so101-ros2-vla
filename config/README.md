# 設定を用途から探す

[プロジェクトのREADME](../README.md) ／ [設定の読み込みを学ぶ](../docs/learning/04-configuration-and-runtime.ja.md)

設定を変更する前に [ローカル設定の作成方法](../docs/public_repository.ja.md) を確認してください。標準YAMLは共有用の例です。対応する `*.local.yaml` がある場合は、そのファイル全体を優先して読みます。機器・作業ごとの値はローカル設定へ保存します。

まず、行いたい作業の入口を選びます。通常の収集は `collection/`、手動起動やモデル実行は `runtime/` です。撮影条件や制御条件を変更するときだけ、そのSystem YAMLが参照する設定へ進みます。

| フォルダー | 用途 | 主に使うファイル |
|---|---|---|
| [collection/](collection/) | 作業名、操作者、本数、収集時間 | [record.yaml](collection/record.yaml) |
| [benchmark/](benchmark/) | 比較モデル、試行数、時間制限と手動復帰時間 | [benchmark.yaml](benchmark/benchmark.yaml) |
| [runtime/](runtime/) | 起動する設定一式を選ぶ | [640×480・共有H.264](runtime/system.real30hz.640x480.h264.yaml)、[640×480・Raw](runtime/system.real30hz.640x480.yaml) |
| [hardware/](hardware/) | アームの接続、校正参照、Controller | [robot.yaml](hardware/robot.yaml)、[controllers.yaml](hardware/controllers.yaml) |
| [control/](control/) | 手動操作の対応、監視と停止 | [teleop.yaml](control/teleop.yaml)、[safety.yaml](control/safety.yaml) |
| [cameras/](cameras/) | カメラ機器、撮影サイズ、FPS、圧縮 | [camera.640x480.h264.yaml](cameras/camera.640x480.h264.yaml) |
| [dataset/](dataset/) | 記録Topic、同期、変換と保存 | [dataset.h264.yaml](dataset/dataset.h264.yaml) |
| [observation/](observation/) | モデル入力カメラ、Resizeなどの前処理 | [observation.yaml](observation/observation.yaml) |
| [policy/](policy/) | 推論、動作列、手法ごとの実行条件 | [policy.real30hz.yaml](policy/policy.real30hz.yaml)、[policy.rtc.yaml](policy/policy.rtc.yaml) など |
| [training/](training/) | ACTの構成と共通の学習条件 | [training.yaml](training/training.yaml) |
| [calibration/](calibration/) | 実物ごとの校正JSONとカメラ校正例 | `leader.json`、`follower.json` |
| [foxglove/](foxglove/) | 監視・収集用の表示レイアウト | [so101-collect-h264.json](foxglove/so101-collect-h264.json)、[比較用](foxglove/so101-benchmark-h264.json) |

## よく使う起動

GUIの端末で `cd /workspace` を実行してから使います。既存Runtimeや学習を終了し、[各操作手順](../README.md)の開始条件を確認してください。

収集する場合は、次を使います。スクリプトの省略時もこの収集設定を読みます。公開例はmockとRaw画像を使います。実機では `record.local.yaml` のBackend・System設定を合わせます。

```bash
./scripts/collect.sh --config_path config/collection/record.yaml
```

テレオペ用Runtimeだけを起動する場合は、次を使います。

```bash
./scripts/teleop.sh real \
  config:=/workspace/config/runtime/system.real30hz.640x480.h264.yaml
```

RTC・Training-time RTC・VLASH・A2C2は、[手法ごとの手順](../docs/methods/index.ja.md)から、対応する `runtime/system...yaml` と `policy/policy...yaml` を選びます。

## 参照と変更の扱い

System YAMLの `files` は、そのファイルの場所を基準に参照します。例えば `runtime/` から `../hardware/robot.yaml` を読む構成です。各設定の保存先や校正参照も、その設定ファイルを基準に解決します。

過去に記録された移動前の設定パスは、読み込み時に新しい場所へ解決します。旧ファイルを直下へ複製して保つ構成にはしていません。新たに編集・起動するときは、このページの新しいパスを使います。

移動による相対パスの書き換えは、校正・保存先・モデル照合の意味を変えません。設定hashでは対象のパスをconfig直下基準へ正規化し、移動前の記録・モデルとの照合を保ちます。設定値や参照する校正ファイルを変えた場合の検査は維持します。

記録済みのMCAPやメタデータは変更しません。実機の設定内容を変更する場合は、記録・モデルとの互換性を別途確認します。Runtimeが読むYAMLの変更は、記録とRuntimeを終了してから編集し、再起動して反映します。
