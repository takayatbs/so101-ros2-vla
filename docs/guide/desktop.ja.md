# GUIへ接続し、端末を準備する

理由や仕組みから学ぶ場合は、[環境と画面を分ける理由](../learning/01-loop-and-environment.ja.md)を併読できます。

[READMEへ戻る](../../README.md)

**このページのゴール：** 操作用PCからLinuxホストのデスクトップを開き、`/workspace` でコマンドを入力できるようにします。

コンテナをまだ作っていない場合は[初回の環境構築](../setup.ja.md)から始めます。すでにGUIが開いている場合は[端末の準備](#terminals)へ進みます。

## 1. 操作用PCで接続用の端末を開く

**入力場所：操作用PCのターミナル。** 次は現在のLinuxホストへの接続例です。別の機器では、SSHの接続先を自分のものへ置き換えます。

```bash
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:6080:127.0.0.1:6080 \
  -L 127.0.0.1:8765:127.0.0.1:8765 \
  robot-user@robot-host
```

この端末は接続中、そのまま開いておきます。6080はデスクトップ、8765はFoxgloveに使います。すでに同じポートを転送しているSSH接続がある場合は、その接続を使い、二重に開きません。

## 2. 操作用PCのブラウザでデスクトップを開く

```text
http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale
```

VNCパスワードを入力します。これは初回に設定した8文字のパスワードです。SSHのログイン用パスワードとは別です。

MATEのLinuxデスクトップが表示されれば接続できています。表示されない場合は[GUIの接続トラブル](../../gui/README.md)を確認します。

<a id="terminals"></a>

## 3. GUIで端末を準備する

**入力場所：ブラウザ内のMATEデスクトップ。** 端末アイコン、またはAlt+F2 → `mate-terminal` → Enterで開けます。

| 使う作業 | 端末の使い方 |
|---|---|
| `collect.sh` で連続収集する | 1つの端末で起動とキー操作。映像は操作用PCのFoxglove |
| 手動テレオペ・モデル実行 | AでRuntimeを起動し、Bでモード切替・記録・停止 |
| 変換・学習 | Runtime終了後に1つの端末で実行。必要なら別端末でログを見る |

各端末で次を実行します。

```bash
cd /workspace
whoami
pwd
```

`dev` と `/workspace` が表示されることを確認します。新しいGUI端末にはROS環境が読み込まれます。初回のROSビルド前は[環境構築の続き](../setup.ja.md#build)へ戻ります。

## 4. 行いたい作業へ進む

- [作業を収集する](../collection_script.ja.md)
- [テレオペだけ行う](teleop.ja.md)
- [初回のmock練習](mock.ja.md)
- [Datasetを作る](dataset.ja.md)
- [学習・実行する手法を選ぶ](../methods/index.ja.md)

作業を終えるときは、それぞれの手順で記録・Runtime・学習を終了してから接続を閉じます。ブラウザを閉じるだけでは、実行中の処理は終了しません。
