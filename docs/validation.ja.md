# 検証の実行方法と確認範囲

[README](../README.md) ／ [実装状況](implementation_status.ja.md)

利用する環境で実行し、コマンド・設定・依存バージョン・結果を `data/logs/` などに保存します。個人の校正・画像・実験ログはソース公開に含めません。

## 共通の回帰テスト

Runtime・収集・学習を終了し、GUIの端末で実行します。

```bash
cd /workspace
./scripts/test.sh -q -m 'not ml and not soak and not browser'
RUN_ML_TESTS=1 ./scripts/test.sh -q -m 'not soak and not browser'
```

| 対象 | 確認する内容 |
|---|---|
| 設定・校正 | ローカル設定、相対参照、設定hash、バックアップと中断復元 |
| 通信・制御 | 模擬シリアル、有限値・鮮度検査、モード切替、指令途絶時の停止 |
| 記録・Dataset | MCAP・動画と時刻の対応、Episode分割、保存・再読込 |
| 可視化 | 順次／並列の一致、入力順の保持、異常終了・中断の後片付け |
| モデル登録 | 重み・校正・前処理の照合、登録と再ロード |

長時間試験は `RUN_SOAK_TESTS=1` で有効にします。短い合成データの学習は処理経路の確認であり、実機のタスク成功率は [実機受入](hardware_acceptance.ja.md)と個別の評価で測定します。

<a id="methods"></a>
<a id="models"></a>

## モデル・手法のテスト

追加モデル用環境の導入後、Runtime・学習を終了してから実行します。

```bash
./scripts/test_policies.sh -q
```

| 対象 | 確認する内容 |
|---|---|
| Standard・VLASH | 合成データの学習、将来状態、動作列の実行、任意指定の平滑化 |
| RTC・Training-time RTC | prefix誘導、学習時のprefix条件、実行済み指令の保持 |
| A2C2 | ベースモデルの照合、補正学習、補正後の平滑化 |
| ACT・Diffusion | 公式processorの保存・再読込、評価、対応するRTC・VLASH |
| SmolVLA・π₀・π₀.₅ | 小型の実VLM／Action Expertで速度場・逆伝播・キャッシュ推論を確認 |
| GR00T・XVLA | 小型の実Action Headと合成VLM特徴で時間条件・逆伝播を確認 |
| ROS | 合成観測で準備完了、非同期出力、モード変更後の停止を確認 |
| 共通 | 古い結果の破棄、遅延枠・動作列枯渇時の停止、重みの取り違え拒否 |

公開重みのダウンロードを伴う大規模検証は、通常試験と分けて行います。実データでの学習・実機の閉ループ成功率・別GPUの推論時間・論文と同条件の性能は各環境で評価します。使用するモデルと対応方式は [モデル選択](methods/models.ja.md)を参照してください。

<a id="gui"></a>

## GUIの確認

1. ホストで `./scripts/gui.sh ps` を実行し、healthyを確認します。
2. ブラウザから接続し、MATEの端末・エディター・ROSツールを開きます。
3. GUI端末でdevユーザー、`/workspace`、Python環境を確認します。
4. Runtimeを終了し、ホストで `./scripts/gui.sh check` を実行します。
5. [mockの操作手順](guide/mock.ja.md)で起動・記録・停止を確認します。
6. 作業を保存してから `./scripts/gui.sh recreate-check` で共有フォルダーとホームの保持を確認します。

結果はGit対象外の `gui/artifacts/` へ保存します。GPU認識とデスクトップの描画方式は別に確認します。管理コマンドは [GUI手順書](../gui/README.md)を参照してください。

## 公開ファイルと単位換算の検証

公開対象・文書リンク・代表的な漏洩パターンは、ホストのプロジェクトルートで確認します。

```bash
python3 scripts/audit_public.py
```

LeRobot単位換算の回帰ベクトルは合成校正を使います。更新する場合は固定した上流commitの計算から生成します。プロジェクト内のソースキャッシュを使い、未取得なら自動取得します。

```bash
python3 scripts/generate_lerobot_reference.py
```
