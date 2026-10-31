# 公開用設定とローカル設定

[README](../README.md) ／ [設定一覧](../config/README.md)

このリポジトリーの設定は出発点となる例です。ホスト名・ユーザー名・USB識別名・校正値・タスク・計算環境は、利用する環境に合わせます。

## 初回の設定

ホストのプロジェクトルートで実行します。コンテナ内では同じプロジェクトが `/workspace` です。

```bash
python3 scripts/init_local_config.py
```

既存のローカル設定は上書きしません。次のファイルを作成し、実機や作業に合わせて編集します。

| 公開する標準設定 | 編集するローカル設定 | 設定する内容 |
|---|---|---|
| `config/hardware/robot.yaml` | `config/hardware/robot.local.yaml` | ポート、校正参照、範囲、実機受入 |
| `config/control/teleop.yaml` | `config/control/teleop.local.yaml` | 校正に対応する倍率・Offset |
| `config/cameras/camera*.yaml` | 対応する `camera*.local.yaml` | USBカメラの識別名、撮影条件 |
| `config/collection/record.yaml` | `config/collection/record.local.yaml` | タスク、操作者、本数、時間、Backend |

同じ方法で、任意のYAMLの拡張子の前へ `.local` を付けられます。例えば `policy.real30hz.yaml` の個人設定は `policy.real30hz.local.yaml` です。

ローカルファイルがある場合は、同名の標準設定の**全体を置き換えて**読みます。部分的なマージではありません。通常の起動引数やSystem YAMLは標準ファイル名のままで使えます。相対パスはどちらも同じフォルダーが基準です。ローカル設定は自動更新されないため、標準設定の更新時は必要な差分を取り込みます。

校正の `measure`・`apply` に伴う機器設定の保存先は `robot.local.yaml`・`teleop.local.yaml` です。校正JSONもGit対象外です。公開設定は `hardware_qualified: false` とし、受入確認後にローカル設定でのみ有効にします。例の関節範囲やグリッパー幅は、実測校正の代わりにはなりません。

標準設定のみを検査する場合は `SO101_USE_LOCAL_CONFIG=0` を指定します。この値は起動するPythonプロセスで読みます。実機の起動で指定するとローカルの受入済み設定を読まなくなるため、通常の実機運用では省略します。

## ホスト・GPU・接続先

`.env.example` から作成する `.env` と、`gui/.secrets/` のVNCパスワードはGit対象外です。`./scripts/gui.sh init` がホストのUID・GID・デバイス用グループを設定し、既存の値を保持します。

公開例は `GPU=0`、時刻表示はUTCです。NVIDIA GPUを使う場合は `.env` の `GPU=1`、利用環境に対応する `TORCH_INDEX` を設定し、Policyや学習の計算先も合わせます。CPU専用構成ではRawカメラProfileを使い、必要に応じてCPU用PyTorch配布先を指定します。

ホーム保存用Volumeの既定名は `so101-ros2-vla-home` です。既存環境から移行する場合は、使っているVolume名を `.env` の `SO101_HOME_VOLUME` へ明示します。既存のVolumeを削除する必要はありません。

手順内の `robot-user@robot-host` と `/path/to/so101-ros2-vla` はプレースホルダーです。SSH先のユーザー・ホスト名と、クローンしたホスト上の絶対パスへ置き換えます。`YOUR_*` のUSB識別名も、`ls -l /dev/serial/by-id /dev/v4l/by-id` の結果と実物を照合して置き換えます。

## 公開対象の確認

プロジェクトルートで実行します。Python標準ライブラリーとGitを使い、Git初期化前でも確認できます。

```bash
python3 scripts/audit_public.py
```

`.gitignore` で除外したデータ、重み、ログ、解析結果、仮想環境、認証情報、ローカル設定、ROS生成物を対象から外し、残ったファイルの個人パス・具体的なIP・デバイス識別子・代表的なトークン形式・非公開ファイルへの文書リンクを検査します。既存Gitで追跡済みの除外対象もエラーにします。ファイル内容や秘密値はエラー出力へ表示しません。

Git初期化前の確認用に、公開対象だけを新しいディレクトリーへコピーできます。コピー先は存在しないパスを指定します。

```bash
python3 scripts/audit_public.py --export /tmp/so101-public-review
```

これはローカルの確認用コピーで、GitHubへの公開操作は行いません。Gitを使っている場合は、ステージした差分も確認します。

```bash
git status --short --ignored
git diff --cached --stat
git diff --cached
```

`.gitignore` は追跡済みファイルや過去のコミットを消しません。必要な場合は、ローカルの実ファイルを保持したまま `git rm --cached` で追跡を外します。このスクリプトは現在のファイルと代表的な漏洩パターンの検査であり、完全な秘密検出やGit履歴全体の監査ではありません。

実験画像・データセット・重み・成功率の実測ログはソース公開に含めません。プロジェクト独自のソースのライセンスは [Apache-2.0](../LICENSE) です。[NOTICE](../NOTICE)・[第三者表示](../THIRD_PARTY_NOTICES.md)・[ライセンス本文](../LICENSES/)も公開対象です。Dockerイメージ、重み、データを別途配布する場合の条件と、依存ライセンスの取り出し方は[ライセンスと配布](licenses.ja.md)を参照してください。

## リポジトリを別の場所へ配置する

ホスト上のクローン先は任意です。コンテナ内では `/workspace` へマウントし、通常の操作コマンドはそのまま使います。モデル・記録・追加依存は `data/` へ保存し、隣接するソースフォルダーには依存しません。

既存環境のローカル設定をコピーするときは、`.env` の `COMPOSE_PROJECT_NAME`、`SO101_HOME_VOLUME`、必要なら `SO101_WORKSPACE` も確認してください。`SO101_WORKSPACE` は省略すると現在のプロジェクトを使います。別のホストへvenvやROSビルド成果物をそのまま移すのではなく、Dockerと導入スクリプトでその環境に合わせて再構築します。GUIのホームはNamed volume、USB機器やGPUドライバーはホスト側で管理します。
