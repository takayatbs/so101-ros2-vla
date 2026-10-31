# ライセンスと配布

確認日：2026-10-31。[README](../README.md) ／ [第三者表示と一次資料](../THIRD_PARTY_NOTICES.md)

## このリポジトリのライセンス

既存のROSパッケージの宣言に合わせ、プロジェクト独自のコード・文書・設定を **Apache-2.0** とし、ルートへ [LICENSE](../LICENSE) と [NOTICE](../NOTICE) を配置しています。第三者部分にはその配布元の条件が適用されます。

LeRobotとの互換処理・モデルのflow/denoisingアダプターには、参照元、著作権表示、プロジェクトで変更した点を記載しています。[LeRobotのLICENSE全文](../LICENSES/LeRobot-LICENSE.txt)は、MIT等の第三者表示も含めて保持しています。FTServoはプロトコルの参考資料として表示し、[MIT本文](../LICENSES/FTServo-LICENSE.txt)を保存しています。FTServo SDKそのものは同梱していません。

ソースをGitHubへ公開するときは、`LICENSE`・`NOTICE`・`THIRD_PARTY_NOTICES.md`・`LICENSES/` と各ファイルの著作権表示を残してください。ROSパッケージ単体で配布する場合にも表示が含まれるよう、各パッケージのインストール対象へライセンス文書を加えています。

<a id="model-weights"></a>

## モデル重みは別の条件

次の表は `policy_catalog.py` にある既定の取得元について、確認時点のモデルカードとLICENSEを照合した結果です。モデル重みはソース配布に含みません。コードがApache-2.0でも、重みやその派生物の条件が同じになるとは限りません。

| 選択値 / 取得元 | 確認した条件 | 利用・再配布時の扱い |
|---|---|---|
| `flow`、プロジェクト独自のACT | 外部の初期重みなし | 独自コードはApache-2.0。自分で収集したデータと生成した重みの公開条件は別途決めます。 |
| LeRobot `act` / `diffusion` | 方策自体は新規学習。既定ではtorchvisionのResNet事前学習重みを取得 | 画像バックボーンの重みとその学習データの条件も確認します。[torchvisionの重みに関する説明](https://docs.pytorch.org/vision/stable/models.html)を参照。`pretrained_backbone_weights: null` で取得しない構成も可能です。 |
| `smolvla` / [lerobot/smolvla_base](https://huggingface.co/lerobot/smolvla_base/tree/5e8d12a6e2975b0e5e5fce7c8caf47c371d257b6) | モデルカードはApache-2.0 | 利用・fine-tuning・再配布時も元の表示と変更の記録を保持します。基盤の[SmolVLM2-500M](https://huggingface.co/HuggingFaceTB/SmolVLM2-500M-Video-Instruct/tree/7b375e1b73b11138ff12fe22c8f2822d8fe03467)もApache-2.0表記です。 |
| `pi0` / [lerobot/pi0_base](https://huggingface.co/lerobot/pi0_base/tree/6e245399ad403ccf02b8112b73cfdc2633068f38) | `gemma`、PaliGemmaベース | [Gemma規約](https://ai.google.dev/gemma/terms)と[禁止用途ポリシー](https://ai.google.dev/gemma/prohibited_use_policy)が適用されます。Apacheのみで配布し直さず、規約の写し、指定NOTICE、変更表示、利用制限の引継ぎなどを確認します。 |
| `pi05` / [lerobot/pi05_base](https://huggingface.co/lerobot/pi05_base/tree/22c30e5b34f9e7ab738b1b34fc2fc5ecfe2d28f0) | `gemma`、PaliGemmaベース | π₀と同様にGemma規約を確認します。 |
| `pi0_fast` / [lerobot/pi0fast-base](https://huggingface.co/lerobot/pi0fast-base/tree/308eb844dabdf46cd9dcd5138c1988b7a234f1d9) | `gemma`、PaliGemmaベース | π₀と同様。追加取得するトークナイザー等にも配布元の条件が適用されます。 |
| `groot` / [nvidia/GR00T-N1.7-3B](https://huggingface.co/nvidia/GR00T-N1.7-3B/tree/2fc962b973bccdd5d8ce4f67cc63b264d6886495) | **同梱LICENSE §3.3に非商用の制限** | モデルカード本文の「商用・非商用で利用可能」という説明と不一致です。商用利用可とは扱わず、該当版の[LICENSE](https://huggingface.co/nvidia/GR00T-N1.7-3B/blob/2fc962b973bccdd5d8ce4f67cc63b264d6886495/LICENSE)と提供元への確認を優先します。全文の[確認用コピー](../LICENSES/GR00T-N1.7-model-LICENSE.txt)を保存しています。派生重みにも制限と表示の引継ぎ条件があります。 |
| `xvla` / [lerobot/xvla-base](https://huggingface.co/lerobot/xvla-base/tree/cdb7964e4fe842935d671bfab5a5ebe00a96648c) | モデルカードはApache-2.0 | 元の表示・ライセンス・変更記録を保持し、追加のVLMや画像モデルを取得する場合はその条件も確認します。 |

表のcommitはライセンス確認時点の記録です。実行時のモデルダウンロードをその版へ固定する変更はしていません。取得した版のモデルカードとLICENSEを保存し、`--pretrained` を変えた場合はその取得元を再確認してください。ROS側の `model.json` や重みのhashは実験の追跡情報であり、配布許可の証明ではありません。

学習済み重みを公開する場合は、ベースモデルのID・実際のrevision・ライセンスとNOTICE・fine-tuningした旨・学習データの利用条件をモデルカードに記載します。`data/` がGit除外であることは、重みやデータを別の場所で公開する許可にはなりません。

<a id="image-distribution"></a>

## Dockerイメージや環境を配布する場合

Dockerfileを公開することと、依存パッケージを含む構築済みイメージを配布することでは、配布物が異なります。このリポジトリには依存パッケージのバイナリーを同梱していませんが、構築したイメージにはUbuntu・ROS・GUI・Pythonとその推移的依存が入ります。

とくにTigerVNC、MATEの一部、現在のUbuntu版FFmpeg等にはGPLが含まれます。noVNCにはMPL、websockifyやGUIライブラリーにはLGPL等も含まれます。FFmpegを一律にLGPLと記載せず、実際の `ffmpeg -L` とパッケージのcopyrightを確認します。[FFmpeg公式の説明](https://ffmpeg.org/legal.html)でもビルドによる条件の違いが説明されています。CUDAやドライバー関連のコンポーネントには[NVIDIAの別規約](https://docs.nvidia.com/cuda/eula/index.html)が適用される場合があります。

配布するイメージそのものの中で、次を実行すると実際の環境の表示を取り出せます。Python標準ライブラリーと `dpkg-query` を使い、パッケージの追加・更新や学習・実機操作は行いません。

```bash
cd /workspace
python3 scripts/export_licenses.py --output data/licenses/release-v1
```

現在のPythonと、存在する場合は `data/envs/policies/bin/python` の両方を調べます。調べる環境を明示する場合は、次のように繰り返して指定します。出力先は既存の内容を保護するため、新しいフォルダーを指定します。

```bash
python3 scripts/export_licenses.py --output data/licenses/release-v2 \
  --python /opt/venv/bin/python \
  --python /workspace/data/envs/policies/bin/python
```

出力には次を含みます。

- プロジェクトのLICENSE・NOTICE・第三者表示・保存したライセンス本文。
- Pythonの各パッケージ名・バージョン・ライセンスメタデータと、配布物に付いているLICENSE・NOTICE・COPYING・EULA等。依存パッケージ本体はimportしません。
- インストール済みDebian/Ubuntuパッケージのバージョン、対応するソースパッケージ名とバージョン、copyright文書、共通ライセンス本文。
- 本文が付いていない配布パッケージについて、公式の同じ版から補ったライセンス文書。[取得元とSHA-256の記録](../LICENSES/runtime-manifest.json)に一致する版だけに適用し、版が変わった場合には流用しません。
- ライセンス本文が見つからないパッケージの `needs_review` 表示。識別子だけで必要な表示が揃ったとは判定しません。

この出力だけでイメージの配布条件を満たすわけではありません。GPL/LGPL/MPL等の条件に従い、該当バージョンの対応ソース、ローカルな変更、必要なビルド情報や再リンクに必要な材料を揃えてください。`apt-get source ソースパッケージ名=ソースバージョン` は取得の起点になりますが、利用した配布元でその版が保存されていることも確認します。上流ホームページへのリンクやライセンス一覧だけでは対応ソースの提供を代替できません。

今回調べた環境では、[`cuda-toolkit==12.8.1` のメタパッケージ](https://pypi.org/project/cuda-toolkit/12.8.1/)に単独のライセンスファイルもライセンス識別子もありませんでした。これを推測でApacheやBSDに分類せず、`needs_review` として残しています。実際のCUDAライブラリーの配布物にあるEULA等とは分けて、イメージを配布するときに提供元へ確認する対象です。

配布対象ごとに、出力の不足表示、ベンダー同梱ライブラリー、モデル重みの条件を確認し、必要な本文と対応ソースを配布物へ添付します。`data/licenses/` はローカル出力としてGit除外です。第三者の本文や著作権表示を、個人設定の一般化のために削除・置換しないでください。
