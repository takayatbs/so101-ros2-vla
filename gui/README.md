# GUIコンテナの管理

[プロジェクトのREADME](../README.md) ／ [環境構築](../docs/setup.ja.md) ／ [GUIへの接続](../docs/guide/desktop.ja.md)

MATE・ROS 2 Jazzy・ロボット制御・記録・学習・推論を、1つの `so101-dev` コンテナで使います。ホストはコンテナの管理、GUIの端末はロボット作業に使います。

## 初回準備と起動

Linuxホストのプロジェクトルートで実行します。

```bash
./scripts/setup_host.sh
./scripts/gui.sh init
python3 scripts/init_local_config.py
```

Docker Engine・Compose・Python 3が必要です。NVIDIA GPUを使う場合はホストのドライバーとContainer Toolkitも用意します。`setup_host.sh` は状態を確認し、自動インストールは行いません。

`init` は `.env` とVNCパスワードを作り、既存の値は保持します。`.env` でGPU、UID/GID、USBグループ、ROS Domain、画面サイズ、タイムゾーンを指定します。公開例は `GPU=0` です。GPUを使う場合は `GPU=1` と対応する `TORCH_INDEX` を設定します。機器・作業の値は [ローカル設定](../docs/public_repository.ja.md)へ保存します。

```bash
./scripts/gui.sh build
./scripts/gui.sh up
./scripts/gui.sh ps
```

`healthy` を確認して、[接続手順](../docs/guide/desktop.ja.md)からブラウザでMATEを開きます。起動するのはデスクトップです。ロボットのRuntimeは [テレオペ](../docs/guide/teleop.ja.md)や [モデル実行](../docs/guide/inference.ja.md)の手順で起動します。

GUIは一般ユーザーdevで実行します。6080・8765はホストの127.0.0.1へ公開し、リモートからはSSHで転送します。VNCの5901とX11 TCPをホストへ公開せず、Docker socketやSSH秘密鍵をコンテナへ渡しません。

## GUIの端末とツール

| ツール | 開き方・用途 |
|---|---|
| MATE Terminal | 上部の端末アイコン、またはAlt+F2で `mate-terminal` |
| Terminator | Alt+F2で `terminator`。端末の分割 |
| Caja | `caja /workspace`。共有ファイルを開く |
| Pluma | `pluma /workspace/config/hardware/robot.local.yaml` などで設定を編集 |
| RViz2 / rqt | `ros2 run rviz2 rviz2` / `ros2 run rqt_gui rqt_gui` |
| 機器の診断 | Runtime起動中に `./scripts/status.sh`。読み取り専用 |

GUIの各端末では最初に `cd /workspace` を実行します。端末AをRuntime、Bを確認・モード切り替え用にすると、停止する端末を取り違えにくくなります。

```bash
cd /workspace
whoami
python -c 'import sys; print(sys.executable)'
```

標準ではdev、`/opt/venv/bin/python`です。ROSツールもRuntimeと同じDomainを使います。カメラと診断を同時に見る場合は [Foxglove](../docs/foxglove_monitoring.ja.md)を使います。

## 設定・ファイルの保存先

| 場所 | 内容 |
|---|---|
| `.env` | ホストごとの起動設定 |
| `config/*.local.yaml`（用途別フォルダー内） | 機器・作業ごとの設定 |
| `gui/.secrets/vnc_password` | VNC認証情報。権限600、実行時に読み取り専用で渡す |
| `/workspace` | ホストのプロジェクトを共有。データは `data/`、ROSは `ros2_ws/` |
| `/home/dev` | MATE設定・ホーム内ファイルをNamed volumeへ保存 |

ホームVolumeの既定名は `so101-ros2-vla-home` です。既存Volumeを使う場合は `.env` の `SO101_HOME_VOLUME` へその名前を指定します。通常のコンテナ再作成では、共有ファイルとホームVolumeを保持します。

## パスワード・画面サイズを変更する

ホストで実行します。

```bash
./scripts/gui.sh passwd
```

8文字の印字可能・空白なしASCIIパスワードを2回入力します。不一致や形式エラーなら既存値を保持します。パスワードをURLへ含めません。画面サイズは `.env` の `VNC_GEOMETRY` で指定します。

変更の反映にはコンテナを再作成します。GUIの編集内容を保存し、Runtime・記録・学習を終了してから実行します。

```bash
./scripts/gui.sh down
./scripts/gui.sh up
```

## 動作確認と権限修復

Runtime・学習を終了してから、ホストで実行します。

```bash
./scripts/gui.sh check
```

GUI・ROS通信・待受ポート・Secret権限・描画方式などを確認し、`gui/artifacts/` へ結果を保存します。GUI内からの `check` はコンテナ内部の確認を行います。詳細は [検証手順](../docs/validation.ja.md#gui)にまとめています。

共有フォルダーとホームの保持を確認する場合は `./scripts/gui.sh recreate-check` を使います。これはGUIコンテナを再作成するため、先に作業を保存してください。

生成物がroot所有になり編集できない場合は、処理を終了してからホストで修復します。

```bash
./scripts/gui.sh permissions
```

data・config・ROSの生成物でroot所有のものだけを、ComposeのUID/GIDへ変更します。既存の非rootファイルとファイルモードを保持し、変更前の所有者をGit対象外のartifactsへ保存します。

## GPUとVS Code

GPUを使う場合は `.env` の `GPU=1`、または `./scripts/gui.sh --gpu up` を使います。GUIのソフトウェア描画とCUDA計算の可否は別です。GUI内で `nvidia-smi` と `python -c 'import torch; print(torch.cuda.is_available())'` を確認できます。

VS Codeでは、稼働中の `so101-dev` へ「Attach to Running Container」で接続します。Dev Containerの定義は `.devcontainer/devcontainer.json` がCPU、`.devcontainer/gui-gpu/devcontainer.json` がGPUです。ユーザーはdev、Workspaceは `/workspace` です。

## 終了・再開とトラブル

記録停止 → IDLE → RuntimeのCtrl+C → 学習終了 → GUIの保存後、ホストで `./scripts/gui.sh down` を実行します。再開は `./scripts/gui.sh up` です。ブラウザを閉じるだけではRuntimeは終了しません。

| 症状 | 確認すること |
|---|---|
| ブラウザで接続できない | コンテナのhealthy、SSHトンネル、ローカルポートの競合 |
| VNC認証に失敗 | 専用パスワードか。変更後にコンテナを再作成したか |
| GUI内でDockerがない | コンテナ管理はホスト、ロボットの作業はGUI内で実行 |
| USBアクセスに失敗 | ホストのデバイス権限、グループID、ローカル設定 |
| ROS Packageが見つからない | `./scripts/build.sh` の結果と、新しい端末での環境読込 |
| 実行ロックが使用中 | Runtime・学習・校正・可視化などのプロセスを終了 |
