# SO-101 ROS 2 VLA

SO-101を人が操作して作業を記録し、モデルを学習させ、ロボットで実行するためのプロジェクトです。ロボット・カメラ・記録・学習は、Linuxホスト上の1つのコンテナで動きます。操作用PCからはブラウザのデスクトップとFoxgloveで操作・確認できます。

リポジトリ名は `so101-ros2-vla` です。このプロジェクトのソースだけでセットアップでき、隣接するLeRobotのチェックアウトは不要です。追加モデルの依存は公式LeRobotの固定commitから取得し、`data/envs/` 内へ保存します。Dockerと、初回の依存・モデル重み取得のネットワーク接続が必要です。

本プロジェクト独自のコード・文書・設定は [Apache-2.0](LICENSE) です。外部コードの表示は [NOTICE](NOTICE) と [第三者ライセンス一覧](THIRD_PARTY_NOTICES.md) にまとめています。モデル重みには別の条件があり、π₀系のGemma規約やGR00Tの非商用条件などは[ライセンスと配布の説明](docs/licenses.ja.md)を確認してください。

初回は [公開用設定とローカル設定](docs/public_repository.ja.md) を確認してください。ホスト・USB識別名・校正値は各自の環境に合わせ、`*.local.yaml` に保存します。標準設定は実機受入前の状態で、データ・重み・実験記録は配布物に含めません。NVIDIA GPUは必要に応じて追加できます。

操作の理由や、別のロボットで実装する考え方から学びたい場合は、[ROS 2でPhysical AIを実装するための教材](docs/learning_path.ja.md)を使ってください。観測・通信・制御・設定・時刻・データ・学習・評価・移植を9つの教材で説明しています。

**すでにテレオペできる場合は、下の「作業を収集する」から進めてください。** 初回の環境構築や校正を、毎回やり直す必要はありません。

<a id="terminals"></a>
<a id="gui-bootstrap"></a>
<a id="gui-terminals"></a>
<a id="setup"></a>
<a id="mock"></a>
<a id="so101-ros-helper"></a>
<a id="record"></a>
<a id="probe"></a>
<a id="real"></a>
<a id="collect"></a>
<a id="convert"></a>
<a id="train"></a>
<a id="deploy"></a>
<a id="foxglove"></a>

## 今やりたい作業を選ぶ

| やりたいこと | 開く手順書 |
|---|---|
| **学習済みモデルでアームを動かす** | **[推論・自律動作の手順](docs/guide/inference.ja.md)** |
| **Foxgloveを見ながら作業を収集する** | **[作業を収集する](docs/collection_script.ja.md)** |
| 記録せず、Leaderを動かしてFollowerを操作する | [テレオペだけ行う](docs/guide/teleop.ja.md) |
| カメラ・ロボットの状態をFoxgloveで見る | [Foxgloveを接続する](docs/foxglove_monitoring.ja.md) |
| 記録したデータを学習用に変換する | [Datasetを作る](docs/guide/dataset.ja.md) |
| ACT・RTC・Training-time RTC・VLASH・A2C2を学習・実行する | [手法を選ぶ](docs/methods/index.ja.md) |
| SmolVLA・π₀・π₀.₅・GR00Tなどのモデルを切り替える | [モデルを引数で選ぶ](docs/methods/models.ja.md) |
| 通常学習／VLASHと平滑化をキー操作で比較する | [1コマンドのベンチマーク](docs/four_condition_comparison.ja.md) |
| 操作用PCから操作画面へ入り直す | [GUIへ接続する](docs/guide/desktop.ja.md) |

各手順書には、入力場所・操作順・結果の確認・終了方法があります。その作業のページを開き、上から順に進めます。

<a id="workflow"></a>

## 初めて使う場合の順番

| 順番 | 作業 | 終わったと判断する目安 |
|---|---|---|
| 1 | [環境を準備する](docs/setup.ja.md) | GUIで端末を開ける。ROSのビルドが通る |
| 2 | [実機なしで起動・記録・停止を練習する](docs/guide/mock.ja.md) | mockの記録がcompleteで保存される |
| 3 | [実機の接続と校正を確認する](docs/guide/hardware.ja.md) | 両アーム・カメラを確認し、実機の受入を終える |
| 4 | [作業を収集する](docs/collection_script.ja.md) | 同じ作業の記録を保存し、採用するEpisodeを選べる |
| 5 | [Datasetを作る](docs/guide/dataset.ja.md) | 映像・関節状態・指令の対応を確認できる |
| 6 | [手法を選び、学習・実行する](docs/methods/index.ja.md) | 専用手順で学習し、mock確認後に実機で評価する |

```text
初回準備 → mock練習 → 実機の確認
                         ↓
                    収集 → Dataset → 手法別の学習 → モデル実行・評価
```

校正の再測定は、組立・サーボ・原点の変更や不一致がある場合に行います。[実機確認のページ](docs/guide/hardware.ja.md)で必要な作業を選べます。

## コマンドを入力する場所

通常の `./scripts/...` と `ros2 ...` は、**ブラウザに表示したMATEデスクトップの端末**で実行します。各端末では最初に `cd /workspace` を入力します。

| 場所 | 行うこと |
|---|---|
| 操作用PCの端末 | SSHトンネル、レイアウトのダウンロード |
| Linuxホストの端末 | 初回のDocker構築・コンテナ管理 |
| GUIの端末 | ロボット操作・記録・変換・学習・モデル実行 |

起動用と操作用の2端末を使う手順では、**A＝起動したままにする端末、B＝操作を入力する端末**と表記します。[画面と端末の準備](docs/guide/desktop.ja.md#terminals)に具体例があります。

<a id="troubleshooting"></a>

## 途中で困ったら

[症状から調べる](docs/troubleshooting.ja.md)を開いてください。Foxgloveの切断は[接続の確認](docs/foxglove_monitoring.ja.md)、機器の取り外し・再接続は[テレオペの再起動手順](docs/guide/teleop.ja.md#reconnect)へ進みます。

<a id="reference"></a>

## 仕組みや設定を詳しく調べたい場合

[設定ファイルは用途別のフォルダー](config/README.md)に整理しています。収集は `config/collection/`、起動設定は `config/runtime/` から選べます。

[手順書・参考資料の一覧](docs/index.ja.md)から、ROSの基礎、設定、保存形式、制御、実装状況を選べます。通常の操作は、上の作業別手順だけで進められます。
