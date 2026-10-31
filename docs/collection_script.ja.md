# Foxgloveを見ながら作業を収集する

機器や作業の値は、[公開用設定とローカル設定](public_repository.ja.md)に従って `*.local.yaml` へ保存します。手順中のホスト名・パス・USB識別名は例です。

理由や仕組みから学ぶ場合は、[Episodeと成功条件・採用条件の意味](learning/06-collection-and-datasets.ja.md)を併読できます。

[READMEへ戻る](../README.md)

**このページのゴール：** 同じ作業を繰り返して記録し、学習へ採用するEpisodeを選んで保存します。保存後に映像・関節・指令の対応と時刻差を確認します。

**開始条件：** [実機の接続・校正・受入](guide/hardware.ja.md)を確認済み。Runtime・学習・Probeは終了しておきます。

**入力場所：** 収集コマンドとキー操作はGUIの端末、映像の表示は操作用PCのFoxgloveです。[GUIと端末の準備](guide/desktop.ja.md)がまだなら先に行います。

`collect.sh` がRuntimeと記録をまとめて扱います。`teleop.sh` や `record.sh start` を別に起動する必要はありません。最初は下の短い収集を試し、変換まで確認してから本数を増やします。

## 1. YAMLで収集条件を決める

[record.yaml](../config/collection/record.yaml) を基に作成した `config/collection/record.local.yaml` をGUIのエディターで開き、作業名・操作者・本数・時間を設定します。公開例はmockです。以下の実機例ではrealとH.264へ変更しています。

以下は30本・各25秒にする設定例です。実行時は、ファイルに保存した本数と時間が使われます。

```yaml
runtime:
  backend: real
  config: ../runtime/system.real30hz.640x480.h264.yaml
  start: true
dataset:
  single_task: 物体を箱に入れる
  operator: operator
  num_episodes: 30
  episode_time_s: 25
  reset_time_s: 10
warmup_time_s: 10
session_root: ../../data/collections
startup_timeout_s: 45
service_timeout_s: 15
```

| 項目 | 意味 |
|---|---|
| `runtime.backend` | 実機はreal。練習はmock |
| `runtime.config` | 解像度・FPS・圧縮・ロボット設定を指定するSystem YAML。収集YAMLからの相対パス |
| `runtime.start` | trueでRuntimeも起動。falseで既存の同じ設定のRuntimeへ接続 |
| `dataset.single_task` / `operator` | 全Episodeで共通の作業名・操作者名 |
| `dataset.num_episodes` | 学習用に選ぶ成功Episode数。失敗・やり直しは数えない |
| `dataset.episode_time_s` | 1回の記録時間。→で早めに終了できる |
| `dataset.reset_time_s` | 記録せずに配置を戻す時間。テレオペと映像配信は続く |
| `warmup_time_s` | 最初の記録前に、テレオペを練習する時間 |

撮影設定はSystem YAMLが参照するCamera YAMLにまとめています。収集YAMLでFPSを二重に指定する必要はありません。

## 2. スクリプトを起動する

Runtimeが停止した状態で、GUI内のターミナルから実行します。

```bash
cd /workspace
./scripts/collect.sh --config_path config/collection/record.yaml
```

最初は次のように短くできます。CLIの値はYAMLより優先します。

```bash
./scripts/collect.sh --config_path config/collection/record.yaml \
  --num-episodes 3 --episode-time-s 10
```

Runtimeのログは `data/collections/SESSION_ID/runtime.log`、収集の進行はターミナルに表示します。起動直後はIDLEです。

## 3. 操作用PCのFoxgloveを接続する

初回は操作用PCの端末でレイアウトを取得します。2回目以降は読み込み済みのものを使えます。

```bash
scp 'robot-user@robot-host:/path/to/so101-ros2-vla/config/foxglove/so101-collect-h264.json' \
  ~/Downloads/so101-collect-h264.json
```

GUI接続で8765も転送している場合は、追加のSSH接続は不要です。まだ転送していない場合だけ、次のトンネルを開いたままにします。

```bash
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:8765:127.0.0.1:8765 robot-user@robot-host
```

Foxgloveで `ws://127.0.0.1:8765` に接続し、Layouts → Import from fileで取得したJSONを読み込みます。

| 場所 | 表示 |
|---|---|
| 上段 | top・side・armの映像 |
| 左下 | 機器の診断 |
| 中央下 | カメラのFPS・遅延、グリッパーの状態 |
| 右下 | 収集フェーズ、本数、残り時間、記録の有無。タブでEpisode・モード・関節値へ切替 |

H.264は録画と同じ配信を使うため、Foxglove用に別の動画圧縮は増えません。従来のJPEGプレビュー設定を使う場合のレイアウトは `so101-collect.json` です。

## 4. Enterで開始し、キーで操作する

映像と初期配置を確認したら、収集ターミナルへフォーカスしてEnterを押します。TELEOPへ切り替わり、WARMUP → RECORDING → RESET → RECORDING…と進みます。
Foxgloveを横に並べて見ながら、キー入力はターミナルで行います。Foxgloveにフォーカスがある間の矢印キーは収集操作になりません。

| キー | RECORDING中 | RESET中 |
|---|---|---|
| → / s / Enter | 成功として保存し、RESETへ | 戻し時間を早めに終えて次へ |
| ← / r | 学習対象外にして、同じ本数位置で再試行 | 直前の記録を学習対象外にして再試行 |
| f | 失敗として保存し、同じ本数位置で再試行 | 直前の記録を学習対象外にして再試行 |
| Esc / q | 現在の記録を中断・失敗として閉じ、収集終了 | 選択済みの記録を残して収集終了 |
| Ctrl+C | 中断として記録を閉じ、終了 | 終了 |

**記録時間が満了すると成功として保存します。** 物体が落ちた、つかめなかった場合はfか←を使ってください。画像から成功を自動判定する機能ではありません。
最後の1本にもRESET時間があります。最後の記録をやり直したい場合も、この間に←を押します。
RESET中は記録が閉じているので、物体を戻す操作を学習用Episodeへ含めません。アームや物体の配置は人が戻します。初期姿勢へ自動で移動する機能はありません。
RESET時間が終わると次の記録が自動で始まります。人の手で物体を戻している間にRECORDINGへ入らないよう、`reset_time_s` は配置戻しが完了する長さにします。

FAULT/ESTOP、ロボットやカメラの更新停止、Recorderエラーでは収集を中断します。エラー解除や再駆動は自動では行いません。
終了時は記録を閉じてIDLEへ戻し、このスクリプトが起動したRuntimeを終了します。

## 5. 保存先とやり直しの扱い

```text
data/collections/SESSION_ID/
  session.json       # 条件、全試行、学習用に選んだEpisode一覧、終了状態
  runtime.log        # このスクリプトでRuntimeも起動した場合

data/bags/EPISODE_ID/
  episode.json       # 元の作業結果・取得時刻・校正情報
  collection.json    # Session、本数位置、試行番号、学習対象に選んだか
  recording.json
  recording_summary.json
  raw/
    raw_0.mcap
    message_index.jsonl.gz
```

やり直しでも元のMCAPは残します。保存後に←を押した場合、元の `episode.json` の作業結果は変更せず、`collection.json` の `accepted: false` で学習対象から外します。
通常の変換は、この除外指定があるEpisodeを拒否します。Sessionを指定する変換では、選んだEpisodeだけを渡します。
保存先はLinuxホストです。操作用PCのFoxgloveで見るだけでは、操作用PCへ記録ファイルは保存されません。

## 6. 収集したデータを確認する

収集コマンドが終了し、記録が閉じてRuntimeも終了したことを確認してから行います。`--attach` を使った場合は、接続先のRuntimeも起動端末のCtrl+Cで終了します。学習やProbeも終了しておきます。

### 確認画面を生成する

GUIの端末で実行します。入力を省略すると、元記録が残っている最新の終了済みSessionを選びます。対象を固定する場合は、収集終了時に表示された `SESSION_ID` を指定します。

```bash
cd /workspace
./scripts/visualize_recording.sh

# Sessionを明示する場合
./scripts/visualize_recording.sh data/collections/SESSION_ID
```

既定は失敗・やり直しを含む全試行と、設定されている全カメラが対象です。採用したEpisodeだけを見る場合は `--accepted-only` を追加します。毎回新しい出力先を作り、終了時に `index.html` の場所を表示します。

既存レビュー・Dataset v2からの動画再利用、単体Episode、カメラの選択、出力先の指定は[記録データの確認手順](recording_review.ja.md)を参照してください。

### ブラウザで開く

表示された出力先の `index.html` をH.264対応ブラウザで開きます。「エピソード確認」で映像・関節・指令・EE・遅延を、「全体比較・EE軌跡」で実演間のばらつきを確認できます。サーバーは不要です。

操作用PCで見る場合は、操作用PCの端末で出力フォルダー全体をコピーします。`OUTPUT_DIRECTORY` を実際のフォルダー名へ置き換えます。

```bash
scp -r \
  'robot-user@robot-host:/path/to/so101-ros2-vla/data/reviews/OUTPUT_DIRECTORY' \
  ~/Downloads/
open ~/Downloads/OUTPUT_DIRECTORY/index.html
```

動画も必要なので、出力フォルダーを丸ごと取得します。

### 映像・動作・時刻を確認する

Episodeを選び、画面下の共通再生ボタンで全カメラを同時に再生します。スライダー、コマ送り、0.25倍速、区間ループも全映像・EE・グラフへ共通に適用されます。最初は `gripper` の開閉や方向転換を映像と波形で照合します。実測は緑、送信済み目標は橙です。

| 確認するもの | 見方 |
|---|---|
| 作業の始まりと終わり | 初期配置、作業の成否、人による配置戻しの混入、遮蔽・ぼけ・画角外を確認する |
| 映像と状態・指令の対応 | 開閉・方向転換を同期映像とSignalsで照合する。物体を挟んでいる間の目標と実測の差は、遅延だけでは説明できない |
| 同期と原記録の対応範囲 | Qualityで同期条件内の割合と原記録のカバー率を確認する。Signalsの同期差は「基準時刻 − 取得元時刻」で、30msなら30ms前の値。対応表に入らない先頭・末尾も確認する |
| 受信遅延と画像更新 | Signalsの受信遅延とQualityの実FPS・最大取得間隔・画像再使用を確認する。約68ms空いたことと約68ms遅れて届くことは別 |
| 1実演のEE軌跡 | 映像と同時に3Dを見る。急な変化や指令との差がある箇所へ移動する |
| 全実演の一貫性 | 「全体比較・EE軌跡」で同じ比較グループの軌跡・始点・終点を重ね、ずれの大きい実演を映像で確認する |

**既定のEEモデルは統合テスト用の近似URDFです。表示位置は、実機で校正した把持点の絶対位置ではありません。** 実機に合ったモデル・TCPを指定する方法は[EEモデルとTCP](recording_review.ja.md#7-eeモデルとtcpを指定する)に記載しています。

現在の同期設定では、時刻差20ms超を警告、50ms超などを学習対象外にします。これは対応付けの条件で、受信遅延の合否基準ではありません。条件内の割合や軌跡の一致だけで、学習後の成功率は判断できません。[時刻差の意味と計測の範囲](recording_review.ja.md#時刻差の意味と計測の範囲)も確認してください。

Review欄へ所見・時刻マーカー・確認範囲を残し、確認結果JSONと一覧CSVを書き出せます。ブラウザを変える前にJSONを保存してください。確認判定・範囲はレビュー用で、元記録・採用可否・学習Datasetは変更しません。問題が見つかった場合は学習へ渡す前に採用対象や収集条件を見直します。

## 7. 選択済みEpisodeをDatasetへ変換する

**収集・確認の完了の目安：** Runtimeが終了し、`session.json` の採用一覧と、採用予定の各Episodeの映像・状態・指令の対応を確認できた。

次は[Datasetを作る](guide/dataset.ja.md)へ進みます。収集終了時に表示された `SESSION_ID` を使い、採用したEpisodeだけを変換します。

上の確認画面は既定で全カメラを表示しますが、現在の学習設定はtop・armです。変換後にも、実際に学習へ使うカメラ・採用Episodeで生成された `review.html` と `sync_report.json` を確認します。その後、学習する手法を選びます。

<details>
<summary>補足：既存Runtimeを使う場合、mockで練習する場合</summary>

## 既存Runtimeとmock練習

GUI以外から操作する場合、Linuxホストではプロジェクトのディレクトリーから同じ収集コマンドを使えます。ホスト版は対話用TTYをコンテナへ渡します。操作用PCからSSHで実行する場合は `ssh -t` でターミナルを確保してください。

既存Runtimeを使う場合は、そのRuntimeと同じSystem YAMLを収集YAMLの `runtime.config` に指定して次を使います。

```bash
./scripts/collect.sh --config_path config/collection/record.yaml --attach
```

設定パスとbackendが異なる場合や、すでに記録中の場合は開始しません。終了時はIDLEへ戻しますが、既存Runtimeは残します。Dataset変換前にはそのRuntimeも終了してください。

実機に接続しない練習は、他のRuntimeを停止して次を使います。画像・関節ともmockの生成値です。

```bash
./scripts/collect.sh --config_path config/collection/record.yaml \
  --backend mock --num-episodes 2 --episode-time-s 5 --reset-time-s 3
```

`--yes` は開始時のEnter待ちを省略する自動実行用です。通常の実機収集ではEnterで映像を確認してから始めます。

</details>
