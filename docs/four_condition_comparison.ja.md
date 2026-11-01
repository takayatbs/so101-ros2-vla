# 通常学習／VLASH × 平滑化なし／ありを比較する

[モデルの選択](methods/models.ja.md) ／ [推論の手順](guide/inference.ja.md) ／ [検証](validation.ja.md)

同じSmolVLAを同じ実演・学習条件で通常学習とVLASHの2つに学習し、推論時の平滑化を切り替えて4条件を作ります。平滑化のための追加学習や、VLASH重みを通常学習と呼び替える処理は行いません。

| 条件ID | 学習方式 | 平滑化 | 実行経路 |
|---|---|---|---|
| `standard_none` | 通常学習 | なし | 非同期 |
| `standard_three_point` | 通常学習 | 腕5関節の3点平均 | 非同期 |
| `vlash_none` | VLASH | なし | 非同期 |
| `vlash_three_point` | VLASH | 腕5関節の3点平均 | 非同期 |

平滑化の重みは `0.25 / 0.5 / 0.25` で、グリッパーは対象外です。A2C2は併用しません。4条件とも制御FPS・カメラ・チャンク長・実行区間・推論の遅延枠をそろえます。通常版にはVLASHの将来状態条件付けを入れず、観測時点の状態から予測します。

比較では全条件へ `--async-inference` を付け、同じ非同期スケジューラーを使います。従来の通常版の推論待ちによる指令停止が、学習方式の差と混ざることを避けるためです。通常の起動ではこの指定は既定で無効、VLASH等の従来の非同期方式は従来どおりです。非同期化しても、実際の推論頻度や指令の揺れが同じになるとは限らないため、実測値も残します。

推論を待つ動作列の長さは、共通設定の最大遅延ステップ数と、実際に残っている動作列の短い方に合わせます。例えば最大15ステップに対して14ステップが残っていれば、その14ステップの範囲で次の推論を待ちます。推論がこの待機枠を超えた場合や動作列を使い切った場合は中断し、古い目標の繰り返しで埋めません。診断には`reserved_delay_steps`と`committed_steps_remaining`も記録します。

## 1コマンドで対話ベンチマークを行う

GUIの端末で起動します。モデル2つをメモリーに保持し、試行間に選択モデルと平滑化だけを切り替えます。推論するモデルは選択中の1つです。カメラ・Controller・モデルを試行ごとに起動し直す必要はありません。

```bash
cd /workspace
./scripts/benchmark.sh
```

`config/benchmark/benchmark.yaml`を読み、同名の`benchmark.local.yaml`があればそちらを優先します。公開例のBackendはmockです。自分の実機ではローカル設定に`backend: real`と2つのモデルIDを設定してください。IDが空なら方式ごとに最新登録のSmolVLAを選び、選んだIDを表示・保存します。比較可能な学習・実行設定であることを確認してから起動します。

```yaml
backend: real
models:
  standard: YOUR_STANDARD_MODEL_ID
  vlash: YOUR_VLASH_MODEL_ID
trials_per_condition: 30
seed: 42
max_duration_s: 30
reset_time_s: 15
operator: evaluation
success_rule: '30秒以内に物体を箱の内側へ入れて離す。試行中の人の介入なし。'
output_root: ../../data/benchmarks
startup_timeout_s: 600
service_timeout_s: 15
```

ローカル設定はこの全体を保存します。収集・学習・他のRuntimeは終了しておきます。両モデルの初回読み込みとIDLEでの準備後、画面に示されたモデル・成功基準・初期配置を確認し、Enterで最初の試行を開始します。以後は「試行 → 判定・停止・保存 → 手動復帰の時間 → 次条件の準備 → 次の試行」と進みます。4条件の順番はブロック内でランダム化します。

通常の推論と同じ`config/runtime/fastdds.xml`を自動適用します。録画・Foxglove用のH.264と、推論用の非圧縮640×480画像は別の通信経路です。H.264が更新されていても推論画像の受信が止まる場合があるため、記録開始の前後に`/policy/check_ready`で推論側の画像・関節観測の鮮度と同期を再確認し、有効な入力がそろってからAUTONOMOUSへ移行します。準備待ちの時間はタスク時間に含めません。

| キー | 動作中 | 戻し時間中 |
|---|---|---|
| → / s / Enter | 成功として終了 | 判定には使わない |
| ← / f | 失敗として終了 | 判定には使わない |
| Space / p | 即座にIDLEへ移行し、中断として保存。戻し時間は一時停止で始める | カウントダウンの一時停止／再開 |
| a | 中断として保存して戻し時間へ進む | 変更なし |
| q / Esc / Ctrl+C | 現在の試行を中断として保存して終了 | 残りを未実施として終了 |

時間切れは**失敗**です。成功・失敗のキーを押したら、先にIDLE・トルクOFFへ移行してから記録を閉じます。初期姿勢と物体は人が戻します。自動復帰は行いません。戻し時間の終了後は自動で次の試行を始めるため、時間が足りなければSpace/pで一時停止してください。モデル・平滑化を切り替えるたびに、前の予測列・平滑化履歴をリセットし、指令を出さない準備推論の完了を待ちます。

動作中の一時停止は、その試行を再開する操作ではありません。介入した試行を成功扱いしないため、中断として残し、手動復帰後に次の試行へ進みます。結果を見て同じ試行をやり直す操作はありません。

### Foxgloveで映像とベンチマークの進捗を見る

`benchmark.sh`はBridgeも起動します。Foxgloveアプリ側では、データソースに**Foxglove WebSocket**、接続先に`ws://127.0.0.1:8765`を指定します。ベンチマークを終了するとBridgeも終了するため、接続できるのはRuntime起動中です。

操作用PCでFoxgloveを開く場合は、GUIへのSSH接続で**8765も転送**してください。6080だけの転送はnoVNC用で、Foxgloveには使えません。既存のSSHコマンドへ次の転送を追加できます。

```bash
-L 127.0.0.1:8765:127.0.0.1:8765
```

接続後、[so101-benchmark-h264.json](../config/foxglove/so101-benchmark-h264.json)を**Layouts → Import from file**から読み込みます。操作用PCのFoxgloveを使う場合は、このJSONを操作用PCへコピーしてからインポートします。[Foxglove接続手順](foxglove_monitoring.ja.md)も参照してください。

上段に3台のH.264映像、右下に試行番号・条件・モデル・平滑化・残り時間・一時停止状態・保存済みの成否数を表示します。「モデル・平滑化」「試行状態」のタブで`/policy/condition`と`/benchmark/state`も確認できます。開始待ち・一時停止中も進捗のheartbeatを配信します。

収集用レイアウトは`collection_session`の診断を参照します。ベンチマークは`benchmark_session`なので、収集用のままでは比較の進行状況が表示されません。成功・失敗・一時停止のキー操作は、ベンチマークを起動した端末へフォーカスして行います。

### 時間と記録

各Episodeの記録はIDLE中に開き、その後AUTONOMOUSを要求します。タスク時間は**AUTONOMOUSサービス要求直前から判定キー／制限時間検出まで**の単調時計による秒数です。サービス応答の時刻も保存するため、ROSの制御モード変更と映像を照合できます。モデルの読み込み・準備・戻し時間は含めません。キー入力の判定には人の反応時間が含まれます。

保存されるデータは次のとおりです。

- 全カメラのH.264圧縮映像を含むMCAP。既存の共有エンコーダーを使い、非圧縮画像の重複記録は行いません。
- 関節実測値、モデルの目標・制御系への指令、モーター有効状態、制御モード、診断、カメラの時刻情報。詳細は選択モデルの記録設定に従います。
- 試行ID、モデルID、平滑化、選択世代、成否と理由、開始要求・応答・終了要求・応答の時刻。
- gzip圧縮したメッセージ時刻索引。動画のデコードに必要な開始前のキーフレーム群は含みますが、戻し時間はEpisodeに含めません。

起動時に表示する`data/benchmarks/SESSION_ID/`には、計画と試行順、`session.json`、`events.jsonl`、Runtimeログ、`trials/`内の結果、終了時の`report/results.md`・JSON・CSVを保存します。元記録は`data/bags/EPISODE_ID/`に保存し、各Episodeの`benchmark.json`と試行結果から相互に対応をたどれます。成功・失敗・中断を全て残し、未実施は成功率の分母へ入れません。

終了後、今回の全試行をまとめて確認できます。失敗・中断も対象です。

```bash
./scripts/visualize_recording.sh data/benchmarks/SESSION_ID --workers 4
```

設定の一時的な変更も可能です。次は各条件1回のmock確認です。模擬試行の成否は実機成功率として扱いません。

```bash
./scripts/benchmark.sh --backend mock --trials-per-condition 1 --reset-time-s 5
```

q等で終了したSessionは未実施分を残して集計します。次に起動すると別の新規計画を作り、以前の結果を上書きしません。長い休憩で同じ計画を続けたい場合は、終了せず戻し時間の一時停止を使います。

## 個別コマンドで計画・結果を管理する場合

以下は従来の手動管理です。`compare_policies.py`単体はロボットの起動・停止・記録を行いません。`plan.json`の`model_root`は作成した環境の絶対パスなので、ホストで作った計画はホストで操作します。コンテナで操作する計画はコンテナ内で作成してください。

## 1. 学習条件をそろえる

既存VLASHの `checkpoint/train_config.json` と `so101_adapter.json` を基準に、同じ初期の事前学習重みから `--method standard` で学習します。既存のfine-tuning済みVLASH重みから通常版の学習を始めると比較条件が変わります。

そろえる項目は、元実演と分割・学習フレーム数・学習ステップ・バッチサイズ・seed・凍結設定・optimizerと学習率schedule・画像前処理・正規化統計・チャンク長・denoising回数です。導入済みの依存バージョンと初期重みのrevisionも記録します。

全実演で学習した既存モデルを再利用する場合、通常版も同じ全実演で学習します。この学習データ上の誤差を、未知データの性能とは扱いません。主評価は新規の実機試行とし、オフライン誤差を公開する場合は別に収集した実演を使います。

## 2. 試行計画を作る

両モデルの学習・登録後、プロジェクトルートで実行します。IDは自分の登録済みモデルへ置き換えます。このツールは計画と結果を管理するもので、ロボットを起動・移動させません。

```bash
python3 scripts/compare_policies.py prepare \
  --standard-model smolvla-standard-v1 \
  --vlash-model smolvla-vlash-v1 \
  --output data/benchmarks/smolvla-four-conditions-v1 \
  --trials-per-condition 30 --seed 42 --max-duration-s 30 \
  --success-rule '30秒以内に物体を箱の内側へ入れて離す。試行中の人の介入なし。'
```

各条件30回、合計120試行です。4条件を1ブロックにしてブロック内の順番をseedでランダム化し、`plan.json` と `trial-order.csv` に保存します。成功基準・時間制限・初期配置は結果を見る前に決めます。同じブロックでは同じ初期配置へ戻し、試行間の物体の置き直しとアームの復帰は人が行います。

学習中に計画だけ作る場合は `--allow-pending` を追加できます。未登録のモデルが残っている間は、試行用コマンドの表示と結果の登録を拒否します。学習完了後、次で確認します。

```bash
python3 scripts/compare_policies.py check data/benchmarks/smolvla-four-conditions-v1
```

`ready: true` になってから評価します。ツールは登録情報を読み、学習設定・データの指紋・校正と前処理・正規化統計・実行設定の不一致を拒否します。これは初期重みの取得過程や実機の配置が同一だったことまで証明する検査ではありません。

## 3. 計画した順で実機評価する

試行1の起動コマンドを表示します。既定はmockです。実機では `--backend real` を指定します。

```bash
python3 scripts/compare_policies.py command \
  data/benchmarks/smolvla-four-conditions-v1 --trial-id 1 --backend real
```

表示された `deploy_policy.sh` コマンドを端末Aで実行し、端末Bで[推論の手順](guide/inference.ja.md)に従って準備確認とモード切り替えを行います。起動直後はIDLEであり、コマンドの表示・起動だけでは自律動作を開始しません。**以下の手動手順には30秒後の自動停止機能はありません。** 成功時・制限時間到達時・中止時にIDLEへ戻します。

時間の基準は、IDLEでのモデル準備が終わった後、AUTONOMOUSへ切り替えた時点とします。動画等で切り替え時刻と成功時刻を残し、計測の起点を条件間でそろえます。Episodeの記録開始時刻がその起点とずれる場合、Episodeの長さをそのまま初動待ちや完了時間として使いません。

成功条件を満たすための試行中の介入は失敗、設備停止などの中断は `aborted` として記録し、成功率の分母にも含めます。うまく動かなかった試行を未実施へ戻したり、成功した動画だけを集計したりしません。既存の運転記録を比較へ加える場合も、試行条件と成功判定がそろっていることを確認します。

## 4. 成功・失敗と実測値を登録する

次はCLI書式の例です。試行ID・成否・時間・記録先を実測値へ置き換えて実行します。値を例のまま結果として登録しないでください。

```bash
python3 scripts/compare_policies.py record \
  data/benchmarks/smolvla-four-conditions-v1 \
  --trial-id 1 --outcome success --duration-s 14.2 --start-delay-s 1.3 \
  --recording /workspace/data/recordings/EPISODE_DIRECTORY
```

`--outcome` は `success`・`failure`・`aborted`、`--duration-s` は実際に実行した秒数です。時間制限を超えた成功、同じIDの上書き、比較途中のモデル・計画の変更を拒否します。

追加の計測を行った場合だけ、`--inference-p50-ms`・`--inference-p95-ms`・`--measured-arm-jerk-rms-rad-s3` を指定できます。未測定の値は省略し、0で埋めません。jerkは実測関節位置の時間微分に基づく値で、時刻・欠損・微分方法をそろえて別途計算する必要があります。予測列をならしたこと自体を、実機の震えが減った証拠とはしません。

## 5. 公開用の表とCSVを出力する

```bash
python3 scripts/compare_policies.py summarize \
  data/benchmarks/smolvla-four-conditions-v1 \
  --output data/benchmarks/smolvla-four-conditions-v1/report-v1
```

`results.md`・`results.json`・`trials.csv` を出力します。成功数／実施数、Wilson法による95%信頼区間、失敗・中断・未記録数、成功試行の完了時間中央値、追加測定の中央値と測定件数を含みます。推論p95を複数試行で測った場合の中央値は、全推論をまとめたp95とは異なります。

未記録の試行があれば `incomplete` とし、未実施を失敗や成功に変換しません。成功率の分母は、成功・失敗・中断を含む記録済み試行です。記録先の生パスと自由記述のメモは公開用出力に含めません。画像・動画や別の補足資料に個人情報がないかも確認してから公開します。元データと出力は `data/` 内のGit除外対象です。

公開時は、成功基準、初期配置、成功率と全試行数、実機とGPU、モデル・データ・コードの版、学習設定、実行設定、測定方法、失敗例を添えます。各方式1つの学習seedによる比較なので、試行の信頼区間だけで「別の学習seedや別タスクでも優れる」とは主張しません。オフライン評価のVLASHは実演の未来状態を使う近似であり、閉ループでの成功率や遅延耐性とは分けて説明します。
