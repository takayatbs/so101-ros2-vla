# 時刻同期と、収集・変換・学習・推論のデータ保管

理由や仕組みから学ぶ場合は、[画像・圧縮・timestampの教材](learning/05-cameras-and-time.ja.md)を併読できます。

容量を抑える既定の流れは、共有H.264で収集 → 同じ圧縮映像をMP4へ格納 → 必要なフレームだけ展開して学習、です。
H.264の元記録を別コーデックへ再圧縮せず、画像の時刻とロボット状態・送信済みactionの対応を別の表に残します。
現在の独自Dataset形式は `so101_dataset_v2` です。既存のPNG＋NPZ形式v1も学習時に読み込めます。

[RTC・Training-time RTC・VLASH・A2C2](methods/index.ja.md)へ進む場合は、ここから[LeRobotDataset v3.0へ書き出す](methods/common.ja.md)工程を追加します。同期で選んだ画像を等間隔のMP4へ並べるため、そのオフライン工程で1回再エンコードします。元MCAPと中間Datasetは保持し、収集とFoxgloveが共有するライブ圧縮処理は増えません。

## 保存先と形式

コンテナの `/workspace` は、ホスト上でクローンしたプロジェクトディレクトリーと同じ内容を参照します。
操作用PCへは自動保存されません。Foxgloveのライブ表示だけでは操作用PCへの録画は始まりません。

| 工程 | 保存内容 | 保存先 |
|---|---|---|
| Runtime起動・表示だけ | 映像はメモリー上。ROSのログは別途残る | 映像の録画ファイルは作らない |
| 収集 | 3カメラ、Leader入力、Follower位置、指令、診断、時刻をMCAPへ記録。設定・校正・クロックも保存 | `data/bags/EPISODE_ID/` |
| 変換 | モデルで使うカメラのMP4、フレーム対応表、関節とactionのNPZ、同期レポート | `--output`。例 `data/datasets/pick-box-v2/` |
| 学習 | 必要な動画フレームを展開し、前処理した画像をメモリー上で入力。重み・統計・ログを保存 | `--output`。例 `data/logs/training/pick-box-act-v2/` |
| モデル登録 | 実行用Checkpoint、Dataset由来と同期設定、deployment.json | `data/models/MODEL_ID/` |
| デプロイ | 保存済みモデルを読み、圧縮前のRGB画像と関節状態を入力 | 記録を開始した場合だけ新しいMCAPを保存 |

元MCAPは変換後も残します。容量は元記録＋学習用の圧縮動画・表＋学習出力＋登録モデルの合計です。
MP4は同じ圧縮映像の別ファイルなので、その分の容量も使います。全フレームのPNGは生成しません。

```text
data/bags/EPISODE_ID/
  episode.json                    # 作業・操作者・成否・開始/終了時刻（秒と整数ns）
  recording.json                  # 収集時の設定・校正の内容、クロック、時刻の意味
  recording_summary.json          # Topicごとの周波数、時刻逆転、最大間隔等
  raw/
    raw_0.mcap
    metadata.yaml
    message_index.jsonl.gz        # 各メッセージのID、取得/受信/書き込み呼出時刻

data/datasets/pick-box-v2/
  dataset.json
  provenance.json
  episodes/000000/
    frames.npz                    # 全target行、状態・action、元時刻、誤差、valid等
    alignment.jsonl               # 行ごとのTopic、message_id、時刻差、警告/除外理由
    sync_report.json               # 実測周波数、欠落、再使用画像、時刻差の集計
    source_episode.json            # 元Episodeと収集時設定
    review.html                    # 動画・関節・action・時刻差の確認画面
    videos/
      top.mp4
      top.npz                     # 動画フレーム番号→元timestamp/MP4時刻/message_id
      arm.mp4
      arm.npz
```

既定の学習入力はtop・armです。sideは元MCAPに残ります。
state/actionはfloat32・rad、`timestamp_ns` と各元時刻はint64・nsです。従来用に秒のfloat64 `timestamp` も残します。
グリッパ位置・目標は関節配列のgripper要素に含まれます。

## 使い方

本数・記録時間・戻し時間をYAMLで指定し、Foxgloveで確認しながら連続収集する場合は、[collect.shの手順](collection_script.ja.md)を使います。`convert.sh --collection SESSION_PATH` で、やり直しを除いたEpisodeだけをDatasetにできます。以下はRuntime・記録・変換を個別に起動する場合です。

収集には共有H.264設定を使います。既存Runtimeがある場合は記録停止・IDLE・Runtime終了後に切り替えます。

```bash
./scripts/teleop.sh real \
  config:=/workspace/config/runtime/system.real30hz.640x480.h264.yaml
```

各カメラで1回NVENC圧縮し、同じH.264をMCAPとFoxgloveへ配信します。3台で3セッションがLinuxホストのNVENCを共有します。
JPEGプレビューの別生成と、RGBの二重録画は行いません。圧縮待ち画像は1枚までで、古い待機画像は圧縮前に置き換えます。
Bフレーム・先読みは無効です。推論用RGBは圧縮処理を待たず配信します。

カメラ取得用のV4L2バッファは2枚です。接続中の3台では、1枚にすると640×480・30 FPS設定でも約15.3〜15.5 FPSになり、2枚・4枚では約29.5 FPSでした。取得スレッドは連続して読み出し、配信用には最新画像だけを保持します。この取得用バッファと、H.264圧縮待ち画像の上限1枚は別のものです。

Foxgloveには [so101-monitor-h264.json](../config/foxglove/so101-monitor-h264.json) をインポートします。
SSH接続は [Foxglove監視手順](foxglove_monitoring.ja.md) と共通です。

```bash
# 操作用PC側で実行
scp 'robot-user@robot-host:/path/to/so101-ros2-vla/config/foxglove/so101-monitor-h264.json' \
  ~/Downloads/so101-monitor-h264.json
```

記録とRuntimeを終了してから変換します。変換と学習はRuntimeと同じロックを使います。

```bash
./scripts/convert.sh /workspace/data/bags/EPISODE_ID \
  --config /workspace/config/runtime/system.real30hz.640x480.h264.yaml \
  --dataset-name local/pick-box \
  --output /workspace/data/datasets/pick-box-v2
```

H.264の記録は、元フレームの取得時刻をMP4のPTSに割り当ててstreamcopyします。解像度変更やCropは学習時の展開後に適用します。
元timestampは整数nsで正確に保存し、MP4のPTSはマイクロ秒単位（最大丸め誤差500 ns）です。末尾フレームの表示時間を確定するため、最後に圧縮パケットだけを再格納します。この処理で動画の再エンコードは行いません。
学習用の等間隔時刻と、動画の実際の取得間隔を分けて管理します。動画を設定FPSだけで再時刻付けしません。
録画途中の開始に備え、MCAPとMP4は直前キーフレームからの短い区間を含む場合があります。学習サンプルはEpisode内から選びます。

従来のRGB記録も変換できます。その場合は、収集後にNVENCで1回H.264化します。H.264の記録を再圧縮する処理は行いません。
`provenance.json` の `video_operations` に `streamcopy` または `h264_nvenc` を残します。
画像を復元できない参照切れや、途中の解像度変更はエラーにします。

```bash
./scripts/train.sh \
  --config /workspace/config/runtime/system.real30hz.640x480.h264.yaml \
  --dataset /workspace/data/datasets/pick-box-v2 \
  --output /workspace/data/logs/training/pick-box-act-v2 \
  --model-id pick-box-act-v2 \
  --steps 20000 --batch-size 8 --device cuda
```

学習時は必要なフレームだけデコードします。1プロセスあたり動画ハンドル最大4個・画像キャッシュ64 MiBに制限します。
複数workerは各プロセスでデコーダーを持ちます。画像の展開・Crop・Resize・Padはメモリー上で行います。
学習開始時に使用可能行数・除外行数・警告行数を表示します。

デプロイも同じ設定です。新しいモデルは、記録された同期設定がRuntimeと異なる場合に起動を拒否します。

```bash
./scripts/deploy_policy.sh MODEL_ID real
```

## 時刻同期のルール

`config/dataset/dataset*.yaml` の `synchronization` を使います。

```yaml
synchronization:
  image_selection: previous
  warning_s: 0.02
  reject_s: 0.05
  camera_skew_s: 0.05
  action_max_age_s: 0.05
  action_offset_s: 0.0
  hardware_synchronized: false
```

1. 必要Topicすべてに共通する時間範囲を使い、設定FPSの等間隔target時刻を整数nsで生成します。
2. 関節状態は `/hardware/follower/state` の計測結果を優先します。過去の記録でデータがない場合は設定State Topicへfallbackし、実際に使ったTopicをレポートへ残します。
3. 画像・状態・actionは、既定ではtarget以前の最新値を選びます。位置の線形補間はしません。
4. `image_selection: nearest` を明示した場合だけ最近傍を使います。未来画像を選んだ場合も符号付き時刻差に残します。
5. 20 msを超えるずれは警告、50 msを超えるずれはその行を除外します。actionの古さ、カメラ間のずれにも別の上限があります。
6. 全target行を保存し、除外行には `valid=false` と理由を付けます。時間を詰めず、学習のactionチャンクも除外行をまたぎません。使用可能行が2行未満なら変換を拒否します。
7. actionは実測位置でなく、実際に送信した目標値です。既定はoffset=0でtarget以前の送信済み値を保持します。`action_offset_s` を明示した場合はtarget+offsetで選び、その意味をDatasetとモデルに保存します。
8. TELEOPで収集した新しいEpisodeは、Leader入力とteleop入力も共通時間範囲に入れ、直前値と元時刻・時刻差を表へ残します。AUTONOMOUSや古い記録で存在しない入力は捏造しません。
9. 元Topicのtimestamp重複・逆転は、並べ替えて隠さず変換を拒否します。FPS低下、長い欠落、同じ画像の再使用は集計します。

`frames.npz` の主な追加列は `valid`、`warning`、`state_time_ns`、`action_time_ns`、`state_time_error`、
`action_time_error`、`image_index/カメラ名`、`image_time_ns/カメラ名`、`image_time_error/カメラ名` です。
操作入力には `input/leader`・`input/teleop` と、それぞれの `input_time_ns/名前`・`input_time_error/名前`・`input_valid/名前` も保存します。
時刻差は「元時刻−target時刻」の秒です。actionのoffsetは別に `action_query_timestamp_ns` としてJSONLへ保存します。

RuntimeのACT観測も同じ選択方式・許容差を使い、未来の関節状態を選ばないようにします。実測State Topicが新鮮なら優先し、存在しない環境だけros2_controlのStateへfallbackします。
画像の形状・前処理・校正・関節順の整合性検査は維持します。

## timestampの意味と、現在の機器の制約

- RGBの `header.stamp`、H.264の `timestamp` は、ホストがV4L2でgrabした直後・展開前の時刻です。センサーの露光時刻は取得していません。
- `/hardware/follower/state` とLeader入力は、サーボ位置の読み取り完了直後のホスト時刻です。サーボ内部の計測瞬間は取得していません。
- `/follower/applied_action` は、サーボへの送信成功後のホスト時刻です。物理的に動き始めた瞬間を測った時刻ではありません。
- Recorderは、メッセージの元時刻と受信時刻・書き込み呼出時刻を分けて残します。ディスクに永続化が完了した時刻は計測しません。
- H.264用の録画開始前の区間についても、元の取得・受信時刻を保ちます。ファイル内の書き込み順を同期時刻に使いません。
- `/camera/NAME/frame_metadata` には画像の取得時刻、ホスト内のフレーム連番、実際の幅・高さ、設定FPS、カメラ名を記録します。カメラ内部やUSBで失われた画像の総数を数える連番ではありません。
- `capture_ready_timestamp_ns` は画像展開・RGB変換完了、`publish_started_timestamp_ns` はROS配信処理開始のホスト時刻です。取得時刻との差から処理時間と待ち時間を分けて確認できます。実機の配信は取得完了通知で起動し、別のFPSタイマーによる待ちを入れません。
- `recording.json` はROS Clock種類、use_sim_time、ホスト名、boot ID、設定・校正内容を記録します。外部PCとのNTP/PTP同期は未検証と明記します。
- ハードウェアトリガとカメラ内timestampは、このUSB機器向け実装にはありません。ソフトウェア同期で完全な同時撮影を保証しません。

ハードウェア同期が用意できた場合の `image_selection: exact` は、`hardware_synchronized: true` の明示が必要です。
これはオフラインの完全一致選択で、カメラをトリガ制御する機能やROS ExactTimeの自動設定ではありません。
別PCの機器を加える場合は、各機器の取得時刻のクロックをNTP/PTP等で整合させ、実測で検証してから使用します。

関節速度は実測位置の差分から推定し、最初のサンプルや長い間隔ではNaNにします。関節トルクは未計測なのでNaN/未取得のままです。
学習用表には速度・effortと、それぞれの値が利用可能かを示すmaskも保存します。サーボのloadを関節トルクへ置き換えません。

## 変換後の確認

```bash
cat data/datasets/pick-box-v2/episodes/000000/sync_report.json
head -n 2 data/datasets/pick-box-v2/episodes/000000/alignment.jsonl
zcat data/bags/EPISODE_ID/raw/message_index.jsonl.gz | head
```

`review.html` をH.264対応ブラウザで開くと、スライダーで学習に使う画像、関節位置、action、元時刻との差、除外理由を確認できます。
最初の動画を再生すると表示時刻が追従します。動画ファイルも必要なので、操作用PCで見る場合はEpisodeのディレクトリーごとコピーします。
再生できないブラウザではその旨を表示します。動画プレーヤーでMP4を開くこともできます。

確認する点は、物体やグリッパーの動きと関節・指令が一致すること、画像番号の繰り返し、長い欠落、時刻差、actionの前後関係です。
変換が成功しても、タスクに必要な画質・FPS・動作対応が十分かは実映像で評価します。

画像のQP18・YUV420は非可逆圧縮です。詰め替えでは劣化を追加しませんが、最初の圧縮で失った画素は戻りません。
Camera入力はMJPGなので、保存したRGBもセンサーの未処理データではありません。小物の輪郭や色で設定の適否を確認してください。

仕様：[FFmpeg streamcopy](https://ffmpeg.org/ffmpeg.html#Streamcopy)、[PyAV remux](https://pyav.org/docs/stable/cookbook/basics.html#remuxing)、
[Foxglove CompressedVideo](https://docs.foxglove.dev/docs/sdk/schemas/compressed-video)。

## 登録モデルの構成と互換性

正常に学習を終えたモデルは、次の構成で登録されます。

```text
data/models/MODEL_ID/
  deployment.json             # モデル形式、学習方式、設定・重みのhash
  dataset_provenance.json     # Datasetの由来
  checkpoint/
    model.safetensors
    config.json
    normalization.json
    train_config.json
    metrics.json
  runtime/
    system.yaml
    policy.yaml
```

追加モデルのCheckpointには `so101_adapter.json` と公式processor・tokenizerも保存します。形式は独自ACTが `so101_act_v1`、独自flowが `so101_flow_v1`、LeRobotアダプターが `so101_lerobot_v1` です。元のモデル名だけで互換性を判断せず、関節順・rad単位・カメラ・前処理・同期・校正と重みのhashを照合します。

学習と実行の再現には、重みと一緒に正規化統計・設定・由来を保持します。実行時は `deploy_policy.sh` が登録情報と保存済みの実行設定を使います。[モデル実行の手順](guide/inference.ja.md)に起動・開始・終了をまとめています。

学習時の統計はtrain側のEpisodeから計算し、検証Episodeへ漏らしません。v1のPNG＋NPZとv2のMP4＋NPZはReaderで扱いますが、外部データには関節・単位・時刻・由来の対応が必要です。[モデルと学習の教材](learning/07-models-and-learning.ja.md)と[共通データ準備](methods/common.ja.md)を参照してください。

通常は学習終了時に自動登録します。別に保存したCheckpointを手動登録する必要がある場合は、整合するDataset・System設定と新しいモデルIDを指定します。

```bash
./scripts/register_model.sh \
  --checkpoint /workspace/data/logs/training/RUN/checkpoint \
  --dataset /workspace/data/datasets/DATASET_NAME \
  --model-id MODEL_ID \
  --config /workspace/config/runtime/system.real30hz.640x480.h264.yaml
```

保存先の用途は [dataディレクトリー](../data/README.md)で確認できます。
