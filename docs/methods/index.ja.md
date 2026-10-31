# 学習・実行手法を選ぶ

**学習済みモデルを動かしたい場合は、[推論・自律動作の手順](../guide/inference.ja.md)へ進んでください。** モデルの起動・準備確認・動作開始・停止を、端末A・Bの順に説明しています。

理由や仕組みから学ぶ場合は、[各手法の仕組みを学ぶ教材](../learning/07-models-and-learning.ja.md)を併読できます。

[READMEへ戻る](../../README.md) ／ 前：[Datasetを作る](../guide/dataset.ja.md)

収集と中間Datasetの作成が済んだら、下の表から1つ選びます。まだDatasetを作っていない場合は[共通の変換手順](../guide/dataset.ja.md)へ進んでください。各ページには、その手法の準備・学習・評価・起動・終了をまとめています。

| 目的 | 進むページ | 学習するもの |
|---|---|---|
| まず通常のモデルで一周試したい | [ACT](../guide/act.ja.md) | 画像と関節状態から動作列を予測 |
| 次の動作を考えている間も、現在の動作を続ける | [RTC](rtc.ja.md) | 通常のflowモデル |
| 動作の続きを予測することを学習時から練習する | [Training-time RTC](training_time_rtc.ja.md) | 既知の先頭動作を条件にするflowモデル |
| 推論が終わる頃の関節状態に合わせて予測する | [VLASH](vlash.ja.md) | 画像は固定し、状態・動作列を同じ時間だけずらして学習 |
| 動作列を実行しながら、最新観測で毎回補正する | [A2C2と組み合わせ](a2c2.ja.md) | ベースモデルを固定して、追加の補正器を学習 |

RTC系・VLASHを選ぶ場合は、先に[LeRobotDatasetへの書き出し](common.ja.md)を1回行います。A2C2は学習済みベースモデルへの追加手順です。

SmolVLA・π₀・π₀.₅などを使う場合は、[モデルを引数で選ぶ](models.ja.md)へ進みます。`--policy` で8種類のLeRobotモデルを選び、対応する手法で学習・評価・実行できます。

## 今回の実装の範囲

RTCの勾配によるprefix誘導、Training-time RTCのclean-prefix学習、VLASHの時刻ずらしと実行時の将来状態条件付け、A2C2の残差教師・補正器を実装しています。RTC＋A2C2、VLASH＋A2C2も使えます。

動作を予測する本体は、標準ではこのプロジェクトの小型画像条件付きflowモデルです。追加したLeRobotアダプターではACT・Diffusion・SmolVLA・π₀・π₀.₅・π₀-FAST・GR00T・XVLAを選べます。手法との対応、初回の依存導入、事前学習元、検証範囲は[モデル選択](models.ja.md)で確認してください。

設定名だけを既存ACTへ付けても、これらのflow手法へ切り替わりません。ここで示す `methods.sh train` と `deploy_policy.sh` を使います。各方式で学習条件の異なるチェックポイントを取り違えると、起動時に拒否します。

## 共通するデータと、手法ごとの違い

```text
MCAP：元画像・状態・指令・取得時刻
  ↓ convert.sh
SO101 Dataset v2：時刻対応・欠損判定
  ↓ methods.sh export
LeRobotDataset v3.0：Parquet＋等間隔MP4＋元の取得時刻
  ├─ RTC：通常のflow学習 → 推論時にprefixを誘導
  ├─ Training-time RTC：学習時にprefixを既知として与える
  └─ VLASH：学習時に状態とactionの時刻を一緒にずらす
       ↓ 各ベースモデルから予測を生成
     A2C2：最新観測に対する残差を追加学習
```

Episode単位で学習・検証を分けます。欠損により元Episodeを複数区間へ分けても、同じ元Episodeの区間が学習側と検証側に分散しないようにします。正規化統計も学習側から計算します。

## 比較するときの見方

同じ元Episode群、同じカメラ、同じ単位、同じ分割、同じ制御FPSで比較します。RTCとTraining-time RTCは動作列をつなぐ工夫、VLASHは状態と実行時刻を合わせる工夫、A2C2は毎ステップ補正する追加機能です。

学習lossやオフラインの関節誤差は、箱に物体を入れられる成功率とは異なります。オフライン評価では記録済みのprefixや将来状態を条件にしており、閉ループで動かしたときの成功率を測っていません。実機性能は別の試行で評価してください。短いスモーク学習の重みは操作配線を確認するためのものです。

小型flowの確認は[手法の検証手順](../validation.ja.md#methods)、LeRobotモデルと公開重みの確認は[モデルの検証手順](../validation.ja.md#models)を使います。

原典：[RTC](https://arxiv.org/abs/2506.07339)、[Training-time RTC](https://arxiv.org/abs/2512.05964)、[VLASH](https://arxiv.org/abs/2512.01031)、[A2C2](https://arxiv.org/abs/2509.23224)。
