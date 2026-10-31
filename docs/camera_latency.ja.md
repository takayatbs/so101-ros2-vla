# カメラの配信待ちを減らす変更と測定方法

実機のカメラは取得完了通知（ROS GuardCondition）で配信処理を起動します。取得スレッドとは別の30Hzタイマーで最新画像を取り出す方式から変更し、カメラの実際の到着時刻とタイマーの位相差による待ち・画像の取りこぼしを減らしました。過負荷時は最新画像1枚だけを保持し、古い画像の列を溜めません。

画像展開・色変換は共有ロックの外で行います。取得停止と取得スレッドのエラーは別の監視タイマーで検出します。mock画像はこれまで通り設定FPSのタイマーで生成します。

取得元timestampはホストのV4L2 grab直後のままです。解像度、QP、GOP、コーデック、同期の除外条件は変更していません。センサー露光時刻や表示完了時刻を取得する変更ではありません。

## 利用環境で比較する

カメラ・解像度・FPS・コーデック・USB接続・負荷をそろえて比較します。カメラとRecorderだけの計測と、制御・推論・Foxgloveを併用した計測を区別します。

保存したJSONから受信遅延の中央値・P95・最大値、実FPS、フレーム欠損・重複・長い間隔を確認します。これらはホストでの取得から受信までの値で、センサー露光から画面表示・物理動作までの遅延とは異なります。

比較図は `scripts/analysis/compare_camera_latency.py` に2回の計測JSONと新しい出力先を渡して作成できます。元の記録と図はローカルのデータ保存先で管理します。

## 測り直す

Runtime・学習を終了してから実行します。ベンチマークは3カメラとRecorderだけを起動し、ロボットDriverやモーターへの指令は起動しません。

```bash
./scripts/benchmark_camera_latency.sh \
  --config config/runtime/system.real30hz.640x480.h264.yaml \
  --duration 60 \
  --output data/logs/camera-latency-check.json
```

出力JSONにカメラ別の中央値・P95・最大値、実FPS、50ms超の間隔、取得済み画像の取りこぼし、処理段階の時刻を保存します。既存の出力ファイルは上書きしません。カメラごとのログも出力先と同じ場所に保存します。

計測に使用したMCAPと時刻索引は、JSON内の `bag` が示す `data/bags/camera_latency_probe_.../raw/` に残します。これらはベンチマークの記録で、学習用の `episode.json` は作成しません。起動中のRuntime・学習とは共有ロックで排他します。

通常の収集・記録には今までのコマンドを使います。

```bash
./scripts/collect.sh --config_path config/collection/record.yaml
```

既存Runtimeは再起動して変更を読み込んでください。収集済みデータの遅延・欠落は変わりません。

Foxgloveのカメラ診断には `capture_decode_ms`（画像展開・色変換時間）と `publish_wait_ms`（展開完了後の配信待ち）も表示されます。旧記録にはこの追加情報はありません。
