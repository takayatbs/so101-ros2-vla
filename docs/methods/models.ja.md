# SO-101のモデルを引数で選ぶ

**学習済みモデルを動かしたい場合は、[推論・自律動作の手順](../guide/inference.ja.md)へ進んでください。** モデルの起動・準備確認・動作開始・停止を、端末A・Bの順に説明しています。

[手法の入口](index.ja.md) ／ [共通データ準備](common.ja.md)

`methods.sh train` の `--policy` でモデルを選びます。`--method` は実行方式です。モデルの選択以外は、同じデータ、登録、評価、A2C2、ROSの起動経路を使います。省略時は従来の独自小型モデル `flow` です。

コードと重みのライセンスは別です。既定のπ₀系の重みはGemma規約、GR00T N1.7の重みの同梱LICENSEは非商用条件です。[モデル別の条件と一次資料](../licenses.ja.md#model-weights)を確認してください。`--pretrained` で変更した重みにも配布元の条件が適用されます。

## モデルと手法の対応

○はアダプターを実装した組み合わせです。ダウンロード済み重みでの実機作業成功を保証する記号ではありません。

| `--policy` | 通常 `standard` | RTC | Training-time RTC | VLASH | A2C2の追加 | 初期重み |
|---|---|---|---|---|---|---|
| `flow` | ○ | ○ | ○ | ○ | ○ | 独自小型モデルをランダム初期化 |
| `act` | ○ | — | — | ○ | ○ | LeRobot ACTを新規学習 |
| `diffusion` | ○ | ○ | — | ○ | ○ | LeRobot Diffusion Policyを新規学習 |
| `smolvla` | ○ | ○ | ○ | ○ | ○ | `lerobot/smolvla_base` |
| `pi0` | ○ | ○ | ○ | ○ | ○ | `lerobot/pi0_base` |
| `pi05` | ○ | ○ | ○ | ○ | ○ | `lerobot/pi05_base` |
| `pi0_fast` | ○ | — | — | ○ | ○ | `lerobot/pi0fast-base` |
| `groot` | ○ | ○ | ○ | ○ | ○ | `nvidia/GR00T-N1.7-3B` |
| `xvla` | ○ | ○ | ○ | ○ | ○ | `lerobot/xvla-base` |

ACTはCVAE、π₀-FASTは離散トークン生成のため、今回のflow用RTCをそのまま使えません。DiffusionのUNetは全動作で共通のノイズ時刻を使うため、動作ごとに時刻を変えるTraining-time RTCに対応しません。DiffusionのRTCは、DDPM/DDIMの正解動作推定に対してprefixの勾配誘導を行います。GR00Tにはnative overlap blendingと別に勾配誘導RTCを接続し、GR00T・XVLAとも各動作の時刻を条件にする学習を追加しています。対応しない組み合わせは、重み取得や学習出力の作成前に理由付きで拒否します。

対応表は次でも確認できます。この確認には重みのダウンロードは不要です。

```bash
./scripts/methods.sh models
```

## 初回の環境準備

必要なモデルの依存を導入します。ホストのプロジェクトルートでも、MATE内の `/workspace` でも同じコマンドを使えます。公式LeRobotの固定commit `22bd7a2f489b367d8df42de803b1e8c4ca63a3f9` を自動取得し、プロジェクト内の `data/envs/lerobot-source-<commit>/` に保存します。隣のフォルダーや別リポジトリを手作業で用意する必要はありません。初回取得にはネットワーク接続が必要で、次回から取得済みのソースを再利用します。

```bash
./scripts/install_policies.sh smolvla
# 対象8モデルの依存をまとめて用意する場合
./scripts/install_policies.sh all
```

専用Python環境は `data/envs/policies/` に作られます。ROSの既存環境を維持し、既存のtorchビルドを引き継ぎます。後から別のモデルを追加するときも同じコマンドです。モデル重みは学習時に取得します。配布元で利用条件への同意が必要な重みは、そのアカウントで事前にアクセスを設定してください。

## 学習する

[共通準備](common.ja.md)で書き出したデータを使います。例はSmolVLA＋VLASHです。

```bash
./scripts/methods.sh train --policy smolvla --method vlash \
  --dataset /workspace/data/datasets/pick-place-lerobot-v1 \
  --output /workspace/data/logs/training/smolvla-vlash-v1 \
  --model-id smolvla-vlash-v1 --steps 20000 --batch-size 8
```

`--policy pi0` などへ変更するとモデルを切り替えられます。保存先とモデルIDには新しい名前を付けます。`--config` を省略すると、`--method` に対応する30 Hz・640×480プロファイルを選びます。追加モデルでは遅延範囲を15ステップ（500 ms）、推論の上限を0.6秒とし、従来の小型flowでは3ステップ・0.2秒を維持します。これは許容範囲の設定であり、時間内に推論できる保証ではありません。独自のカメラや制御周期を使う場合はSystem YAMLを `--config` に指定し、その設定の遅延枠を使います。

遅延枠だけを変える場合は `--max-delay-steps` と `--max-inference-s` を指定できます。チャンク長と実行区間を含む上限を検証し、学習した範囲を超える実行を拒否します。登録時に実際の設定を `runtime/system.yaml` と `runtime/policy.yaml` へ保存し、A2C2用プロファイルも作ります。

非同期実行では、`max_delay_steps` の枠全体を推論待ち時間として予約します。直近の推論が速くても予約を縮めず、その枠内の時間変動で予測を破棄して再計算することを防ぎます。VLASHは予約先の関節目標を将来状態として使い、その時刻から新しい動作列へ切り替えます。実際の推論時間が学習範囲を超えた場合、動作列を使い切った場合、観測や指令が古い場合の停止条件は維持します。

`standard` は通常学習、`rtc` は通常のflow学習と実行時prefix誘導、`training_time_rtc` は既知prefixをノイズなしで条件にする学習、`vlash` は画像を固定して状態と正解動作列を同時にずらす学習です。画像と一緒にタスク文を渡しますが、現在のSO-101実行系は登録モデルごとに1タスクを扱います。

事前学習元を変える場合は `--pretrained HF_ID_OR_LOCAL_PATH` を指定します。追加設定は `--policy-config model-options.json` で渡せます。例えば大きなモデルでbatch sizeを下げ、モデル側の `gradient_checkpointing` を指定できます。モデルが持たない設定や、単位・関節・時刻の対応を壊す設定は拒否します。`--dim-model` など従来の小型flow用引数は、LeRobotモデルのサイズ変更には使いません。

ACTとDiffusionは方策を新規学習しますが、標準では画像用ResNetの事前学習重みを使います。画像モデルも新規学習する検証では `{"pretrained_backbone_weights": null}` を設定できます。

## 評価・実行する

モデルの種類は保存済みの情報から読み取るため、評価・起動時に `--policy` を指定し直す必要はありません。

```bash
./scripts/methods.sh evaluate \
  --model /workspace/data/models/smolvla-vlash-v1 \
  --dataset /workspace/data/datasets/pick-place-lerobot-v1 \
  --delay-steps 3 \
  --output /workspace/data/logs/evaluation/smolvla-vlash-v1.json

./scripts/deploy_policy.sh smolvla-vlash-v1 mock
```

`deploy_policy.sh` は登録情報に対応するモデル、学習方式、Python環境と保存したRuntimeプロファイルを選びます。カメラや遅延枠を変えて学習した場合も、その設定を引き継ぎます。起動後は既存手順と同じくIDLEで `/policy/ready` を確認し、準備が整ってからモードを切り替えます。実機では[実機受入](../hardware_acceptance.ja.md)を済ませ、`mock` を `real` に変更します。起動用のSystem YAMLを明示する場合は `--config` に指定します。

`runtime.sh` はFast DDS用の `config/runtime/fastdds.xml` を読み込みます。640×480 RGBの配信用に共有メモリーを64 MiBへ拡張し、UDP通信はlocalhostへ限定します。[Fast DDSの標準共有メモリー領域は512 KiB](https://fast-dds.docs.eprosima.com/en/2.x/fastdds/transport/shared_memory/shared_memory.html)です。撮影FPSが正常でも受信が欠落すると、Policyが同期不良を検出して停止します。独自の通信設定を使う場合は、起動側の `FASTRTPS_DEFAULT_PROFILES_FILE` にコンテナ内のXMLパスを指定します。画像の同期許容値と制御のWatchdogは変更しません。

```bash
./scripts/deploy_policy.sh smolvla-vlash-v1 mock --dry-run
```

上の確認では起動コマンドを表示するだけです。起動時に前処理、校正、関節順、重みのhashを照合します。実測した推論時間が学習時の遅延範囲を超える場合は、遅延設定と学習を見直します。30 Hzでの実行指令は、モデル推論が毎秒30回完了するという意味ではありません。

予測した関節目標の細かな揺れをならす場合は、全モデルの推論入口で指定できます。既定は無効（`none`）です。

```bash
./scripts/deploy_policy.sh smolvla-vlash-v1 mock --smoothing three_point

./scripts/runtime.sh mock --smoothing three_point
```

同じ `--smoothing three_point` はオフライン評価にも指定できます。

```bash
./scripts/methods.sh evaluate \
  --model /workspace/data/models/smolvla-vlash-v1 \
  --dataset /workspace/data/datasets/評価用データセット \
  --smoothing three_point \
  --output /workspace/data/logs/evaluation/smoothed.json
```

通常のACT・Diffusion・各VLAと、対応するRTC・Training-time RTC・VLASHは共通処理を使います。予測列の腕5関節へ前・現在・次の重みが `0.25 / 0.5 / 0.25` の平均を適用します。端は複製し、グリッパーとRTCの確定済みprefixは変更しません。処理済みの列を実行用に保存するため、VLASHの将来状態の見積もりにも同じ目標が使われます。

A2C2併用時は、学習時と同じ未平滑化の予測を補正器へ渡し、その補正後の指令へ直近3点の同じ重みを適用します。未来の補正結果は使えないため、これは約1制御周期の遅れを伴う因果的な処理です。モード切り替えでは履歴を消去します。A2C2学習用キャッシュとモデル学習時の損失・検証は生の出力を使い、既存の補正器の入力分布を維持します。オフライン評価結果には `action_smoothing` と適用箇所を記録します。

`--smoothing none` で解除できます。直接ROS launchを使う場合は `action_smoothing:=three_point` を指定します。`runtime.sh` も `--dry-run` で起動コマンドだけを表示できます。動作列の継ぎ目を混ぜる処理は含まず、重み・校正・Watchdog・遅延範囲も変更しません。滑らかさと把持・初動への影響はモデル・方式ごとに評価します。

## A2C2を追加する

[A2C2手順](a2c2.ja.md)の `prepare-a2c2` と `train-a2c2` に、上で登録したモデルを渡します。全モデルの対応済み方式に追加できます。`standard` や `training_time_rtc` に追加するプロファイルも用意しています。

```bash
./scripts/methods.sh prepare-a2c2 \
  --model /workspace/data/models/smolvla-vlash-v1 \
  --dataset /workspace/data/datasets/pick-place-lerobot-v1 \
  --output /workspace/data/logs/training/smolvla-vlash-a2c2-cache-v1

./scripts/methods.sh train-a2c2 \
  --cache /workspace/data/logs/training/smolvla-vlash-a2c2-cache-v1 \
  --output /workspace/data/models/smolvla-vlash-a2c2-v1

./scripts/deploy_policy.sh smolvla-vlash-v1 mock \
  --correction /workspace/data/models/smolvla-vlash-a2c2-v1
```

LeRobotモデル向け補正器の特徴は、正規化した最新関節状態と、各画像を8×8へ平均プールしたRGBです。大きなVLMを制御ステップごとに再実行せず、小型MLPで残差を学習します。独自flow用の補正器は従来どおり学習済みエンコーダーの特徴を使います。ベース重み、前処理、統計の指紋を保存し、異なるモデルへの補正器の付け替えを拒否します。

## 検証の範囲

同じSmolVLAで通常学習／VLASHと平滑化なし／ありを比較する場合は、[4条件の比較手順](../four_condition_comparison.ja.md)を使います。比較用の通常版にも非同期実行を選べる `deploy_policy.sh --async-inference` を追加しています。直接ROS launchを使う場合は `asynchronous_inference:=true` です。いずれも自律動作の開始は既存のモード切り替えで行います。

合成画像・関節データで、LeRobot ACTとDiffusionの短い学習、登録、公式processorの保存・読み込み、オフライン評価、A2C2の学習・推論を確認します。SmolVLA・π₀・π₀.₅では、実ネットワークの次元を小さくした試験で、元の速度場との一致、キャッシュ推論、clean-prefix学習の逆伝播、RTC誘導、推論時のprefix固定を確認します。

GR00T・XVLAは小さくした実際のAction Headへ合成VLM特徴を渡し、元の時間条件付けとの一致、学習の逆伝播、RTC、prefix固定を確認します。ROSではLeRobot ACT＋VLASHのモデル準備完了、合成観測からの非同期指令、モード変更後の指令停止を試験します。

大規模な公開重みでの全モデル学習、GR00T・XVLA・π₀-FASTでの実データ学習、実機の閉ループ成功率は別の検証です。モデルを選べることと、その重みでSO-101の作業を達成できることを区別して評価します。

公開重みを使う場合は、少量の合成データで学習・保存・登録・読み直し・評価を確認してから実データへ進みます。確認方法と範囲は[モデルの検証手順](../validation.ja.md#models)を参照してください。

参照：[LeRobot公式モデル一覧](https://github.com/huggingface/lerobot)、[VLASH論文](https://arxiv.org/html/2512.01031v2)。
