# 統合GUIコンテナの環境構築

機器や作業の値は、[公開用設定とローカル設定](public_repository.ja.md)に従って `*.local.yaml` へ保存します。手順中のホスト名・パス・USB識別名は例です。

理由や仕組みから学ぶ場合は、[ホスト・コンテナ・ROSビルドを分ける理由](learning/01-loop-and-environment.ja.md)を併読できます。

[READMEへ戻る](../README.md)

**このページのゴール：** 初回のコンテナ構築、GUI接続、ROSビルドを完了します。すでに構築済みで操作画面へ入りたい場合は[GUI接続](guide/desktop.ja.md)へ進みます。

GUI、ROS 2、SO-101の制御・収集・学習・推論は1つの `so101-dev` コンテナで動きます。起動後は、ブラウザで開いたMATEのターミナルへコマンドを直接入力します。GUIの中から別のSO-101環境へSSHで移る操作はありません。

コンテナがまだ存在しない最初の準備だけはLinuxホストで行います。パスワード・画面・コンテナの詳しい管理方法は[GUI手順書](../gui/README.md)にあります。

## 1. ホスト・イメージ・コンテナの関係

| 名前 | 役割の例 | 操作する例 |
|---|---|---|
| Linuxホスト | Docker、GPUドライバー、物理USB機器を持つLinuxホスト | イメージのビルド、コンテナの起動・停止 |
| Dockerイメージ | Ubuntu・GUI・ROS・ライブラリーをまとめた再作成用のひな形 | `gui.sh build` で作る |
| コンテナ | イメージから起動したLinuxの実行環境 | GUIの端末でROSとSO-101を使う |
| bind mount | ホストのプロジェクトを `/workspace` に共有する仕組み | コード、設定、Raw、モデルを保持する |
| Named volume | Dockerが保持するホームの保存領域 | MATE設定、ホーム内のファイルを保持する |

コンテナを作り直しても、bind mountとホームVolumeの内容は残ります。ただし、開いたままのアプリや未保存の編集は終了します。保存と、記録・Runtime・学習の正常終了を先に行います。

## 2. ホストで初回準備をする

```bash
cd /path/to/so101-ros2-vla
./scripts/setup_host.sh
./scripts/gui.sh init
python3 scripts/init_local_config.py
```

Docker Engine、Compose、Python 3が必要です。GPUを使う場合はホストのNVIDIAドライバーとContainer Toolkitが必要です。`setup_host.sh` は状態を表示する確認用で、自動インストールしません。

`.env.example` は設定例、直下の `.env` はこのPCで使う値です。`init` は既存値を保持し、不足する統合GUIの設定だけを追加します。VNCパスワードも既存値を保持します。機器固有の校正JSONはイメージやGitへ含めません。

| 設定 | 内容・理由 |
|---|---|
| `GPU=1` / `0` | NVIDIA機器の割当を選ぶ。CPU専用PCなら0 |
| `INSTALL_ML=true` | ACT学習・推論用のPyTorchなどを含める |
| `TORCH_INDEX` | 対応するPyTorch配布先。既定はCUDA 12.8向け |
| `DEV_UID` / `DEV_GID` | 共有ファイルを編集するホスト利用者とdevの数値IDを合わせる |
| `SO101_DIALOUT_GID` / `SO101_VIDEO_GID` | ホストのシリアル・カメラのグループ権限をdevへ渡す |
| `ROS_DOMAIN_ID` | ROS通信グループ。既定42 |
| `RMW_FASTRTPS_PUBLICATION_MODE=ASYNCHRONOUS` | DDSの送信待ちが制御処理へ及ぶことを減らす |
| `GUI_HTTP_PORT` / `VNC_GEOMETRY` | ブラウザ接続ポートと仮想画面のサイズ |
| `SO101_CPU_THREADS=2` | NumPy・PyTorchなどの各プロセスのCPU計算スレッドを抑え、制御への負荷を減らす |
| `GUI_SOFTWARE_RENDERING=1` | GPUなしでもGUIを表示する。CUDAの計算先とは別 |

`INSTALL_ML=false` でも校正・実機通信・mock・収集・Dataset変換は可能です。ACT学習・ACT推論・MuJoCoにはtrueが必要です。LeRobotや外部モデルのダウンロード、Tokenは使いません。

## 3. ホストでイメージを作り、起動する

Runtime・校正・学習・テストを終了してから実行します。

```bash
./scripts/gui.sh build
```

ルートのDockerfileで、Ubuntu 24.04、MATE、ROS 2 Jazzy、SO-101、学習用ライブラリーを1つの環境へ導入します。amd64・arm64に対応したNobleのROSイメージとパッケージを使います。ホストのCPUアーキテクチャーに対応する依存パッケージを使用してください。確認手順は[統合環境の検証](validation.ja.md#gui)にあります。

起動します。

```bash
./scripts/gui.sh up
./scripts/gui.sh ps
```

`healthy` を確認します。プロジェクトは `/workspace` へ共有され、GUIのホームはNamed volumeへ保存されます。

<a id="build"></a>

## 4. GUIでROSワークスペースをビルドする

[GUI接続の手順1〜3](guide/desktop.ja.md)でMATEと端末を開き、ここへ戻ります。以降はGUIの端末へ入力します。

```bash
cd /workspace
whoami
python -c 'import sys; print(sys.executable)'
./scripts/build.sh
```

ユーザーはdev、Pythonは `/opt/venv/bin/python` です。GUI内の `build.sh` はDockerを呼ばず、ROSの `colcon build` を行います。C++のコンパイル、Message/Service型の生成、Pythonの起動入口やPackage登録を行うためです。

Dockerイメージにもビルド済みのソースがありますが、実際の `/workspace` はホストの共有ファイルへ置き換わります。その共有Workspaceに成果物を用意します。ビルド後、既存の別端末で必要なら読み直します。

```bash
source /workspace/ros2_ws/install/setup.bash
```

新しい端末では自動で読み込まれます。単に同じ環境を起動する日には、毎回ビルドする必要はありません。

| 保存先 | 中身 |
|---|---|
| `ros2_ws/build` | コンパイルなどの中間生成物 |
| `ros2_ws/install` | ROSから使うPackage・ライブラリー・設定 |
| `ros2_ws/log` | ビルドログ |

## 5. GUI内でテストとGPUを確認する

Runtimeなどを終了して実行します。

```bash
./scripts/test.sh -m 'not ml'
```

`passed` は成功、`deselected` は指定した条件で対象外、`skipped` は実行条件により省略された試験です。件数は試験追加で変わるため、固定件数ではなく失敗内容と対象範囲を確認します。

学習機能を含むイメージでは次も確認します。

```bash
python -c 'import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU")'
```

GPU構成はTrueとGPU名、CPU構成はFalseが想定です。GPU=0でもCUDA向けPyTorchを入れる構成はありますが、計算先に `--device cpu` を指定します。x86_64のCPU専用wheelには `TORCH_INDEX=https://download.pytorch.org/whl/cpu` を選ぶ方法があります。

**初回構築の完了の目安：** GUI端末でdevユーザーとして操作でき、ROSビルドと試験が通った。次は[mockで起動・記録・停止を練習する](guide/mock.ja.md)へ進みます。以下は権限や設定変更が必要な場合の補足です。

## 6. USB・カメラと一般ユーザーの権限

Composeでホストの `/dev` と、USB serial/V4L2用のデバイスアクセスを渡しています。通常ユーザーdevはdialout/videoの数値グループIDを追加されます。デバイスの一覧と `id` を確認し、アクセスできない場合はホストのグループID、udev、実際の権限を調べます。

機器は変わりやすいttyACM番号ではなくby-idで照合します。USBが見えることと、サーボが正しいID・通信速度で応答することは別です。[校正手順](calibration.ja.md#ports)で実物との対応を確認します。GUI全体をrootへ変更して解決しようとしません。

旧生成物のPermission deniedには、Runtimeを終了してホストの `./scripts/gui.sh permissions` を使えます。対象はroot所有のdata/config/build/install/logで、プロジェクト全体を無条件に変更しません。

## 7. 設定変更を反映する

YAMLは対象のRuntimeを再起動します。ROSソースやMessageを変えたらGUIでROSビルドします。`.env` のGPU・Domain・機器グループはコンテナの起動設定なので、ホストで再作成します。

```bash
./scripts/gui.sh up --force-recreate
```

Dockerfileや導入ライブラリーを変えた場合は、その前にホストで `./scripts/gui.sh build` が必要です。再作成はGUIにも影響するため、記録・Runtime・学習の終了とGUIの保存を行います。

ネットワークは専用のDocker bridgeです。GUIの6080とFoxgloveの8765だけをホストの127.0.0.1へ転送し、VNCの5901やROSのUDPポートは外部公開しません。ROS通信は同じDomain・QoS・Topic型を使う必要があります。同じGUI内で、AがRuntime、Bが操作、Cがrqt/RVizなら、同じROS環境で確認できます。詳細は[ROS 2基礎](learning/02-ros-communication.ja.md)と[構成](architecture.ja.md)を参照してください。
