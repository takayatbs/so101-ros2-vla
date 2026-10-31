# 4. 設定を選んでRuntimeを起動する

[教材の入口](../learning_path.ja.md) ／ [前の教材](03-hardware-and-control.ja.md)

`collect.sh` と `teleop.sh` はどちらもロボットを使えますが、担当する作業が違います。前者はRuntimeの起動とEpisodeの繰り返しをまとめ、後者はRuntimeを起動したままにします。実行するスクリプトと、読み込む設定の組を追うと、自動で行われる操作と人が行う操作を区別できます。

公開設定は例です。[ローカル設定](../public_repository.ja.md)を作成すると、対応する `*.local.yaml` の全体が優先されます。実機の値やタスクを変更するときはローカル側を編集します。

## 収集設定から機器設定まで読む

標準の収集は次の順で設定を読みます。

```text
collect.sh
  → config/collection/record.yaml
    → config/runtime/system.real30hz.640x480.h264.yaml
      → config/hardware/robot.yaml          ポート、校正、関節、制御方式
      → config/hardware/controllers.yaml    Controllerの関節と周期
      → config/cameras/camera.640x480.h264.yaml
      → config/control/teleop.yaml           LeaderからFollowerへの対応
      → config/control/safety.yaml           更新期限と停止条件
      → config/dataset/dataset.h264.yaml      記録Topicと時刻同期
      → config/observation/observation.yaml   モデル入力と前処理
      → config/policy/policy.real30hz.yaml    推論と動作列の扱い
      → config/training/training.yaml        学習条件
```

`config/collection/record.yaml` の本数を変えると、繰り返す成功Episode数が変わります。カメラの幅や高さは、収集設定ではなく参照先のCamera YAMLで変更します。同じFPSや画像サイズを複数の場所で指定すると、どの値が実機へ渡るか分かりにくくなるため、役割ごとに分けています。

相対パスは参照する設定ファイルの場所を基準に解決します。収集YAMLの `runtime.config: ../runtime/system.real30hz.640x480.h264.yaml` は、collectionの1つ上からruntimeフォルダーへ進んだ場所を指します。端末を開いた場所だけで、読み込む相手が変わる仕様にはしていません。[用途別の設定一覧](../../config/README.md)でも参照関係を確認できます。

## 起動引数とYAMLとROS Parameter

`config:=...` はLaunchへ設定ファイルの場所を渡す引数です。各Nodeにはそのパスや `backend` などをROS Parameterとして渡します。Nodeは起動時にYAMLを読み、自分が使う項目を取り出します。

YAMLのすべての項目が動作中に変更できるROS Parameterになるわけではありません。例えばファイルを編集しても、起動済みのカメラが自動で解像度を変更する実装にはなっていません。記録を閉じてRuntimeを終了し、編集後に起動し直す手順を使います。

`backend:=real` は実機へ接続する指定です。`mock` は合成の関節と画像を使い、`mujoco` は近似の関節モデルを使います。モデルの選択は別の `policy_backend` で行います。`hold` は現在位置を返す確認用、`act` と `flow` は学習済みモデルを読む入口です。

## 起動すると何が使えるようになるか

LaunchはDriver、Controller Manager、Controllerの起動処理、Teleop、Safety、カメラ、Recorder、Episode Manager、Policy、診断、Foxglove Bridgeを組み合わせます。これがRuntimeです。

起動直後はIDLEです。準備が終わった後でTELEOPを選ぶと、人の入力が採用されます。記録管理は起動済みでも、Episodeを開始するまで作業のMCAP記録は始まりません。モデルを読み込む起動でも、AUTONOMOUSを選ぶまでは自律動作へ進みません。

Controllerは起動とactiveへの移行に時間がかかるため、起動ログが出始めたことだけで操作を始めません。現在のProfileでは `joint_state_broadcaster` と `forward_position_controller` のactiveを確認します。Follower Driver、Controller Manager、Safetyの重要プロセスが終了した場合は、Runtime全体を終了する監視もあります。

## 設定値が変える処理を知る

| 設定 | 変える対象 | 値だけでは分からないこと |
|---|---|---|
| `robot.update_rate_hz` | ロボット側の処理周期の目標 | 実測周期、サーボ内部の制御周期 |
| `camera.cameras.*.fps` | カメラへ要求する撮影FPS | 実際の取得・配信FPS |
| `video.enabled` | NVENCによるH.264経路の有効化 | 機器やライブラリーが利用できるか |
| `observation.cameras` | AIへ渡すカメラ | 記録されるカメラの総数 |
| `preprocessing.*.resize` | AI入力画像の大きさ | カメラが取得する画像の大きさ |
| `dataset.fps` | 学習用に作る共通時刻列の間隔 | 毎時刻で異なる画像が得られるか |
| `action_chunk.chunk_size` | 1回に予測する指令数 | 推論時間や実機の到達時間 |
| `n_action_steps` | 次の予測へ進むまで使う動作列の長さ | 非同期時の遅延と切替時刻 |

例えば画像を小さくしたいときは、保存量を減らしたいのか、モデル計算を軽くしたいのかを分けます。撮影サイズを下げると取得後の画像量が減り、Resizeだけを下げると主にモデル入力が小さくなります。撮影済みの元データを保持していれば、新しい前処理でDatasetを作り直せます。

## 変更を反映する操作を選ぶ

| 変更したもの | 行う操作 |
|---|---|
| Runtimeが読むYAML | 記録終了、Runtime終了、再起動 |
| C++・独自メッセージ・パッケージ構成 | ROSビルドと環境の読み直し、Runtime再起動 |
| Dockerfile・導入ライブラリー | イメージのビルドとコンテナ再作成 |
| `.env` のGPU・USBグループ・Domainなど | コンテナ再作成 |
| ロボット・校正・前処理・入力の定義 | 影響するDatasetとモデルの互換性も確認 |

Pythonソースが共有されていても、実行中のプロセスは以前読み込んだコードを使っています。ファイルを保存したことと、新しい処理で動作していることを分けて確認します。

モデル登録では設定や校正のhashを保存します。hashは内容を識別する値です。例えば関節の原点を変えると、同じ0.5 radでも学習時と違う姿勢になり得るため、古いモデルを誤って使う組合せを拒否します。hashの一致は、物理的な校正や作業性能が正しいことの証明ではありません。

## 使用中の機器と計算資源を管理する

Runtime、Probe、変換、学習などは共通のロックを使い、競合する処理の同時実行を拒否します。USBを二重に開くことに加え、重い学習が制御の実行を遅らせる事態を避けるためです。

ロックは、処理が資源を使っていることを表す管理です。ロックファイルを削除しても、起動中のプログラムが終了するわけではありません。使っている処理を確認して正常終了する手順を選びます。別の機体を同時に動かす場合は、機器だけでなくGPU・CPU・記録先の分け方も設計します。

設定の読み取りは[config.py](../../ros2_ws/src/so101_core/so101_core/config.py)、起動対象は[RuntimeのLaunch](../../ros2_ws/src/so101_bringup/launch/runtime.launch.py)、設定の一覧は[参考資料](../configuration.ja.md)で追えます。自分の設定図で「どのファイルの、どの値を、どの処理が使うか」を説明できれば、変更の反映先を判断しやすくなります。

[次の教材：画像と時間と保存容量](05-cameras-and-time.ja.md)
