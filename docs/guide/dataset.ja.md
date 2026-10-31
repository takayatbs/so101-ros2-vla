# 記録からDatasetを作る

理由や仕組みから学ぶ場合は、[時刻をそろえて学習データを作る理由](../learning/06-collection-and-datasets.ja.md)を併読できます。

[READMEへ戻る](../../README.md) ／ 前：[作業を収集する](../collection_script.ja.md)

**このページのゴール：** 採用した作業記録を、画像・関節状態・指令の時刻をそろえた学習用データへ変換します。この工程まではACT・RTC系・VLASHで共通です。

**入力場所：GUIの端末。** 記録とRuntimeを終了してから `cd /workspace` を実行します。`collect.sh --attach` を使った場合は、残っているRuntimeも起動端末のCtrl+Cで終了します。

## 1. 収集Sessionの保存先を確認する

`collect.sh` の終了時に表示された次の場所を使います。

```text
data/collections/SESSION_ID/session.json
```

Sessionは1回の連続収集のまとまりです。`accepted_episodes` に、今回学習へ採用したEpisodeが並びます。失敗・やり直しの元記録も残ります。

変換前の元記録を確認する手順は[収集したデータの確認](../collection_script.ja.md#6-収集したデータを確認する)にあります。単体Episodeや可視化のオプションは[記録データの確認手順](../recording_review.ja.md)を参照してください。このページの手順3では、変換後の学習用データを確認します。

## 2. 変換する

次の `SESSION_ID` を実際の値に置き換えます。

```bash
./scripts/convert.sh --collection /workspace/data/collections/SESSION_ID \
  --dataset-name local/pick-place \
  --output /workspace/data/datasets/pick-place-aligned-v1
```

収集時のSystem設定をSessionから読み、採用したEpisodeだけを変換します。`pick-place-aligned-v1` は出力名です。すでにある場合は上書きできないため、`v2` など新しい名前を選び、以降の手順でもその名前へそろえます。

`record.sh` で個別に記録した場合は、代わりに次を使います。Episode IDと、実際の記録時の設定を指定します。

```bash
./scripts/convert.sh /workspace/data/bags/EPISODE_ID \
  --config /workspace/config/runtime/system.real30hz.640x480.h264.yaml \
  --dataset-name local/pick-place \
  --output /workspace/data/datasets/pick-place-aligned-v1
```

複数本を使う場合はEpisodeのパスを並べます。元の独立した作業が2本以上あると、学習と検証に分けられます。

## 3. 結果を確認する

```bash
cat data/datasets/pick-place-aligned-v1/dataset.json
```

Episode一覧と作業名を確認します。GUIのファイル管理で出力フォルダーを開き、各 `episodes/000000/` などの `review.html` をブラウザで表示すると、映像・関節・指令・時刻差を確認できます。

変換では、既定で基準時刻以前の最新データを対応付けます。時刻差が大きい行は無効として残し、学習から除外します。元の整数ns時刻も保持します。変換が成功しても、画像欠損や同じ画像の再利用がないことまでは保証しないため、レビューと `sync_report.json` を確認します。

元MCAPは `data/bags/` に残り、今回の中間DatasetはMP4＋NPZの `so101_dataset_v2` です。詳しいファイル構成は[データ保管の参考資料](../data_storage_video.ja.md)にあります。

<a id="method"></a>

## 4. 学習する手法を選ぶ

**ここでこのページの作業は完了です。** [手法を選ぶページ](../methods/index.ja.md)へ進みます。

| 選んだ手法 | この後のデータ処理 |
|---|---|
| ACT | 今回のDatasetを直接使う |
| RTC・Training-time RTC・VLASH | 専用の共通準備でLeRobotDataset v3.0へ書き出す |
| A2C2 | 先にベースモデルを学習し、その予測から補正器用データを作る |

変換エラーが出た場合は[症状から調べる](../troubleshooting.ja.md)へ進みます。記録時の設定・校正を合わせるため、メタデータのhashを手で変更して通す操作は行いません。
