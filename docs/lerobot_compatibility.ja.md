# LeRobotのSO-101動作に合わせた設定

このワークスペースは、LeRobotのSO-101ドライバーの位置換算、テレオペ対応、サーボ初期設定、指令の扱いを再現します。`config/hardware/robot.yaml` の `control_profile: lerobot` が標準です。校正JSONは機体ごとに測定した値を使います。有効な既存校正の再利用や再測定の判断は[校正手順](calibration.ja.md)に従います。

参照版は、上流LeRobotの **`22bd7a2f489b367d8df42de803b1e8c4ca63a3f9`** に固定しています。動くmainブランチを毎回追うのではなく、この版との対応を検証します。

- [Follower実装](https://github.com/huggingface/lerobot/blob/22bd7a2f489b367d8df42de803b1e8c4ca63a3f9/src/lerobot/robots/so_follower/so_follower.py)
- [Follower設定](https://github.com/huggingface/lerobot/blob/22bd7a2f489b367d8df42de803b1e8c4ca63a3f9/src/lerobot/robots/so_follower/config_so_follower.py)
- [Leader実装](https://github.com/huggingface/lerobot/blob/22bd7a2f489b367d8df42de803b1e8c4ca63a3f9/src/lerobot/teleoperators/so_leader/so_leader.py)
- [位置の正規化・逆変換](https://github.com/huggingface/lerobot/blob/22bd7a2f489b367d8df42de803b1e8c4ca63a3f9/src/lerobot/motors/motors_bus.py)
- [Feetech初期設定](https://github.com/huggingface/lerobot/blob/22bd7a2f489b367d8df42de803b1e8c4ca63a3f9/src/lerobot/motors/feetech/feetech.py)

## 1. 校正値・単位・テレオペ

| 項目 | 再現する動作 |
|---|---|
| モーター | STS3215、ID 1〜6、通信1 Mbps、protocol 0 |
| 校正JSON | `id`、`drive_mode`、`homing_offset`、`range_min`、`range_max` をそのまま使う。現在の両JSONは `drive_mode: 0` |
| 5つの回転関節 | `use_degrees: true`。測定範囲の中点を0°として、1回転4095 tickで換算 |
| ROS側の角度 | rad。LeRobotのdegreeをradへ換算してTopic・独自Datasetへ渡す |
| グリッパー | LeRobotと同じ閉0%・開100%。ROSでは測定した実角度へ換算する |
| テレオペ | 同じ関節名へ対応。回転関節は同じ角度、グリッパーは同じ開度割合 |
| `use_degrees: false` | 回転関節を−100〜100へ正規化する方式も対応。両アームの幅に応じて対応倍率を算出 |
| 指令の整数化 | LeRobotと同じ `int()` によるtick整数化。Goal_Positionは符号絶対値表現のbit 15 |

degree方式の回転関節は、ソフト側で校正min/maxへ指令を切り詰めません。グリッパーは0〜100%、正規化方式の回転関節は−100〜100へ制限します。これは参照実装の変換処理に合わせた動作です。校正範囲内であることは、机・物体との衝突がないことを保証しません。

`calibrate.sh apply` は、JSONを変更せず、グリッパー換算・全関節の表示用範囲・テレオペ倍率をYAMLへ反映します。回転関節の表示用範囲は `±(range_max-range_min)/2 × 2π/4095` です。

## 2. 指令の制限とController

| 項目 | 標準設定・動作 |
|---|---|
| 一律±1.5 radの位置制限 | 使用しない。表示・URDFのmin/maxは校正JSONから算出 |
| 独自の速度・加速度フィルター | 使用しない |
| 独自のjumpフィルター | 使用しない |
| 独自の0.05 radステップ制限 | 使用しない |
| `max_relative_target` | LeRobotと同じ `null`。指定したときだけ、実測現在位置との差を制限 |
| 上記を指定する場合の単位 | 回転関節はdegree、グリッパーは開度%。正規化方式では回転関節も−100〜100の単位 |
| ROS Controller | `forward_position_controller`。位置指令をそのまま渡し、軌道補間を加えない |
| 制御・Teleopの目標周期 | 60 Hz。参照版 `lerobot-teleoperate` の標準FPSに対応 |

`max_relative_target` は正の数を1つ、または全6関節名をキーにした辞書で指定します。例えば `5.0` は回転関節の1回の目標差5°、グリッパーの開度差5ポイントに相当します。速度の単位ではありません。

旧方式は `control_profile: bounded` を明示した場合に利用できます。`limits` の `velocity`、`acceleration` と `config/control/safety.yaml` のjump・軌道設定はこの方式用です。LeRobot方式では指令へ作用しません。URDFの速度値も旧方式・計画用のメタデータであり、LeRobotのサーボ速度設定ではありません。

## 3. サーボ初期設定

| レジスター | Leader | Follower |
|---|---:|---:|
| Return_Delay_Time | 0 | 0 |
| Maximum_Acceleration | 254 | 254 |
| Acceleration | 254 | 254 |
| Phase | bit 4のみ解除、他のbitを保持 | 同左 |
| Operating_Mode | 0（位置制御） | 0（位置制御） |
| P_Coefficient | 変更しない | 16 |
| I_Coefficient | 変更しない | 0 |
| D_Coefficient | 変更しない | 32 |
| gripper Max_Torque_Limit | 変更しない | 500 |
| gripper Protection_Current | 変更しない | 250 |
| gripper Overload_Torque | 変更しない | 25 |

この数値はサーボレジスターの値であり、rad/秒²やアンペアの実数値ではありません。設定値は `config/hardware/robot.yaml` の `servo_settings` にあります。

接続時に型番777と、アーム内のファームウェア版の一致を検査します。位置の同期読み取りは通信失敗に対してLeRobotと同じ追加2回まで再試行します。通信期限は50 msで、参照版SDKのパケット期限の約50 msに対応します。SDK・OSの待ち時間まで一致するという意味ではありません。

### グリッパーの過負荷通知

物を挟むと、グリッパーの実測位置が閉じ指令へ届かない状態になります。参照版LeRobotの同期読み取りはサーボのstatusを上位例外にせず、通信が成立すれば位置データを利用します。ROS版も `control_profile: lerobot` のFollowerでは、初期設定完了後、**グリッパー単独の過負荷bit `0x20`** をWARNとして扱います。有効な実測位置の配信と目標送信を継続し、この通知だけではアーム全体をFAULTにしたり記録を失敗扱いにしたりしません。

サーボ自身の過負荷保護と、上表の最大出力・電流・過負荷設定は有効です。握力が一定になる保証ではなく、サーボ自身の保護で把持力が下がることもあります。保持できたかは実機で確認し、作業の成否を記録します。

過電流・過熱・電圧異常、他関節の過負荷、複数bitを含むアラームは停止対象です。欠落・不正長・重複応答・チェックサム不一致も引き続き検出します。起動時の型番・校正・初期設定の照合、および `bounded` profileは全アラームをエラーとして扱います。

Followerの診断には2秒ごとに `gripper_load_percent`、`gripper_position_rad`、駆動中の `gripper_target_rad`、`gripper_overload_reported`、`servo_status` を出します。`servo_status` は直近の応答と前回診断以降に検出した通知を含むため、短い通知も表示・MCAPに残ります。負荷%はサーボの駆動出力指標で、接触力の実測値ではありません。Leaderは読み取り専用の操作入力なので、Followerの接触力をLeaderへ返す機能はありません。

LeaderはトルクOFFで位置を読み取ります。FollowerのPIDとグリッパー保護はFollowerにだけ設定します。初期設定は目標位置を書かず、トルクONもしません。

## 4. カメラ・記録とROS固有の相違

カメラはRGB、回転なし、接続後の読み捨て1秒、最新フレームの期限0.5秒です。USBパス・解像度・FPS・MJPGは各機器に合わせてローカル設定で指定します。LeRobotのOpenCV設定も解像度・FPS・FOURCCを機器ごとに指定するため、LeRobot全体で固定された解像度があるわけではありません。

制御60 Hzと、カメラ・Datasetの30 Hzは別です。ROSの非同期ノード、DDS、Controller Managerを使うため、LeRobotの単一同期ループと処理タイミングは同一ではありません。設定FPSが同じでも実測FPSを保証しません。

次はROS Runtimeとして保持する機能です。

- 起動後はIDLE、モードを明示してから駆動する。`hardware_qualified` は自動でtrueにしない。
- 状態・指令・Enableの期限、ESTOP、異常停止の保持、有限値検査、機器の排他ロック。
- 駆動開始時に現在位置の目標を先に送り、古いサーボ目標への移動を避ける。
- 校正JSONとサーボが違う場合は停止する。LeRobotの対話画面のように既存JSONをその場でサーボへ書き戻す処理はRuntimeへ追加しない。

これらは追加の位置・速度フィルターではありません。校正と方向の一致だけで、実機の動作確認を完了扱いにはしません。[実機受入](hardware_acceptance.ja.md)で確認します。

独自Datasetの保存形式は引き続きプロジェクトの形式です。LeRobotのDatasetやCheckpointをそのまま読めるようにする変更ではありません。設定hashが変わった場合、既存モデルの受入を再確認します。

## 5. 検証方法

`tests/fixtures/lerobot_so101_reference.json` は、固定したLeRobotソースの `_normalize` と `_unnormalize` を実行して生成した期待値です。両アーム、degree・正規化の両方式、範囲外指令、端点と半tickの中点を含みます。SDKや実機を使わずに同じ出力になることを検査します。

加えて、役割別のサーボ初期設定、読み取り再試行、任意の相対目標制限、元JSONの不変性、ROSのForward Controllerからハードウェア指令まで値が変わらず届くことをテストします。

Probeは読み取り専用です。`inside_configured_limits` は保存した校正範囲との位置比較、`position_limits_enforced_in_software` は関節ごとの位置切り詰めの有無です。LeRobot方式のdegree関節はfalse、グリッパーはtrueです。端点の比較では単位換算の丸めを吸収する1e-9 radの許容差を使います。
