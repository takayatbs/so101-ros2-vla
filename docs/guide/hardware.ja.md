# 実機の接続と校正を確認する

理由や仕組みから学ぶ場合は、[単位・校正・制御・停止の理由](../learning/03-hardware-and-control.ja.md)を併読できます。

[READMEへ戻る](../../README.md) ／ 前：[mock練習](mock.ja.md)

**このページのゴール：** 接続中のアームとカメラを確認し、必要な校正と実機受入へ進みます。

**入力場所：GUIの端末。** 実物のアームを支持し、駆動電源を止められる場所で行います。記録・Runtime・他の実機操作プログラムを終了しておきます。

## 1. 校正を測り直す必要があるか選ぶ

| 状況 | 進む先 |
|---|---|
| 同じアームをLeRobotなどで校正済み。組立・サーボ・原点を変更していない | 下の接続確認へ進む |
| サーボ交換・組立変更・原点変更がある、校正の由来が不明 | [校正専用の手順](../calibration.ja.md)を行い、完了後にこのページへ戻る |
| 新品サーボ、ID重複、通信速度の不一致がある | [サーボの初期設定](../calibration.ja.md#motor-setup)から進む |

人が手で動かすアームがLeader、作業するアームがFollowerです。USBのby-idと実物の対応を[USB照合の手順](../calibration.ja.md#ports)で確認します。Leader用のJSONをFollowerへコピーして合わせる操作は行いません。

<a id="probe"></a>

## 2. 読み取りProbeを実行する

```bash
cd /workspace
./scripts/probe.sh \
  --config /workspace/config/runtime/system.real30hz.640x480.h264.yaml \
  --output /workspace/data/logs/hardware_probe_latest.json
```

このコマンドは目標位置・トルク・校正値を書き込まず、サーボとカメラを読み取ります。すでにONのトルクを解除する処理もありません。

```bash
cat data/logs/hardware_probe_latest.json
```

| 確認する項目 | 次へ進む目安 |
|---|---|
| アームとカメラの `error` | エラーがない |
| 両アームの `calibration_matches` | ともにtrue |
| 6関節の `joint_positions_rad` | 現在位置を取得できている |
| Followerの `inside_configured_limits` | true。falseなら対象関節と校正・姿勢を確認 |
| カメラ3台の `shape` | 640×480設定では `[480, 640, 3]` |
| カメラの `actual_fps` | 実測値を確認。設定30 FPSだけで達成と判断しない |

不一致や通信エラーがある場合は[校正の確認・復旧](../calibration.ja.md#recovery)へ進みます。電圧の適否は、実物のサーボ・電源の仕様と照合します。

## 3. 実機受入を行う

[実機受入手順](../hardware_acceptance.ja.md)へ進み、支持・台座・作業空間・独立した駆動電源遮断、小さな動作、追従方向、停止、記録を確認します。

`hardware_qualified` は、その起動前確認を済ませたことを記録する設定です。起動エラーを消すためだけに変更せず、受入手順の順序で扱います。

**完了の目安：** 両アームの校正一致、3カメラの取得、小さな操作・停止・短い記録を確認した。

次は[作業を収集する](../collection_script.ja.md)へ進みます。記録せずに操作したい場合は[テレオペだけ行う](teleop.ja.md)を使います。
