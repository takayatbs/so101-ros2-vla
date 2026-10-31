# ROS 2で必要な制御と停止の設計

理由や仕組みから学ぶ場合は、[単位と機器の制御から読む教材](learning/03-hardware-and-control.ja.md)を併読できます。

Leaderの肘が0.3 radになったとき、Followerへ0.3 radを1回送るだけでは動作を管理できません。Followerの現在位置、急な目標変化、通信が止まった場合、机や関節の使用範囲を考える必要があります。

この章では現行の制御を、指令の生成、制限、Controller、Driver、サーボ内部の制御に分けて説明します。操作は[テレオペ手順](guide/teleop.ja.md)、実物の確認は[実機受入](hardware_acceptance.ja.md)を参照します。

## 1. 現在位置と目標位置を分ける

現在位置をq、目標位置をq_targetと書くと、追従誤差は `e = q_target - q` です。Stateがq、位置のActionがq_targetです。ActionをStateとして記録すると、まだ到達していない位置を到達済みとして学習してしまいます。

5つの回転関節はradを使います。グリッパーも、このSO-101では駆動関節の角度をradで扱います。独自の直動グリッパーなら幅をmで表す設計もあります。アーム全体で「positionは必ず角度」と決めず、関節ごとに単位を定義します。

## 2. 位置・速度・トルクの制御方式

| 制御方式 | 送る量 | 実装時に必要な確認 |
|---|---|---|
| 位置制御 | 目標の角度・位置 | 内部の追従方式、可動範囲、急な目標変化 |
| 速度制御 | 目標の角速度・速度 | 停止時の0指令、積算位置、速度上限 |
| トルク／力制御 | モーターのトルクや力 | 電流・力換算、摩擦、重力、低遅延の帰還 |

今回のSTS3215は位置制御モードで使い、サーボ内部が目標位置への追従を行います。標準のROS側Forward Position Controllerはposition interfaceへ目標をそのまま渡します。この構成のROS側に、別の位置PIDを新規実装しているわけではありません。

PIDは誤差、誤差の累積、誤差の変化から操作量を計算する方法です。概念式は `u = Kp*e + Ki*積分(e) + Kd*de/dt` です。uが電流なのか速度なのかは制御対象で変わります。内部位置制御のあるサーボへ、根拠なく外側のPIDを重ねると扱いが難しくなるため、まず機器の制御方式と提供インターフェースを確認します。

現在の校正ツールはPID調整を行いません。実機の剛性や振動の確認は、校正値の一致とは別です。

## 3. ros2_controlで分ける3つの処理

Controller Managerは、状態をreadし、Controllerを更新し、指令をwriteする処理を管理します。起動時は1件だけの状態で準備完了とせず、新しいHeaderを持つ状態が複数回・50 ms以上にわたって届くことを確認してからHardwareをactivateします。待機中は動作指令を有効にせず、動作中の受信タイムアウトは維持します。Hardware Interfaceは機器固有の入出力、Controllerは公開された標準インターフェースを使う計算を担当します。[ros2_control公式のHardware Interface説明](https://control.ros.org/jazzy/doc/ros2_control/hardware_interface/doc/hardware_interface_types_userdoc.html)に、state/command interfaceの定義があります。

| 処理 | 今回の担当 | 入出力 |
|---|---|---|
| read | C++ TopicSystem | Driverの実測位置をState interfaceへ |
| update | Forward Position Controller | 目標を補間せずposition commandへ渡す |
| write | TopicSystemとPython Driver | position commandをサーボへ送る |

`joint_state_broadcaster` はStateをTopicへ公開します。モーターを駆動するControllerではありません。標準の `forward_position_controller` は目標をそのまま渡します。旧bounded方式の `joint_trajectory_controller` は位置目標を扱います。軌道補間と機器の物理追従は異なる処理です。[Controller公式説明](https://control.ros.org/jazzy/doc/ros2_controllers/joint_trajectory_controller/doc/userdoc.html)を参照できます。

## 4. Teleopの関節対応

Teleopは `q_follower_target = scale * q_leader + offset` で対応する関節の目標を作ります。scaleは倍率、offsetは基準位置の差です。Leaderの名前にはleader_を付け、Followerの名前へ対応させます。

身体の5関節は現在scale=1、offset=0です。グリッパーは2台の校正開閉幅が異なるため、applyで倍率を計算します。取り付け方向が違う独自ロボットでは、符号やゼロの定義まで確認します。数字を合わせるだけで、実物の姿勢が合うとは限りません。

関節名・配列長・重複・NaN/Infを検査します。LeRobot方式では回転関節の目標を追加の位置範囲へ切り詰めません。Leaderのデータが古ければ、新しいTeleop指令を公開しません。

## 5. 指令Sourceを1つに選ぶ

| Mode | 指令Source | 動作 |
|---|---|---|
| IDLE | なし | 待機。今回の初期実装はトルクOFF |
| TELEOP | `/teleop/action` | 人の操作を使う |
| AUTONOMOUS | `/policy/action` | Policyの予測を使う |
| MOVEIT | `/moveit/action` | 外部の単一点指令を使う。Planner本体は未実装 |
| ESTOP | なし | 緊急停止を保持 |
| FAULT | なし | 異常停止を保持 |

TELEOPからAUTONOMOUSへ直接切り替えず、IDLEを経由します。切替時に過去のSourceを消し、古い目標を次のモードへ持ち越さないためです。停止保持中は通常のモード要求を拒否します。

起動時はIDLEです。Policyは新しい観測で初回計算を行い、指令へ送らずに破棄して `/policy/ready` をtrueにします。準備前のAUTONOMOUS要求はSafetyが拒否します。初回のGPU計算が重くても、動作中のWatchdog期限を広げず準備できます。Runtimeを起動したことと、実機を動かすことは別です。`hardware_qualified` は実機Runtime起動前の確認済み記録であり、モード選択の代わりではありません。

## 6. 指令を制限する理由と計算

標準の `control_profile: lerobot` は、LeRobotのSO-101と同じ目標をサーボへ渡します。独自の位置・速度・加速度・jumpフィルターは適用しません。`max_relative_target` は標準nullで、指定した場合だけ実測位置と目標の差をLeRobot単位で制限します。

旧 `control_profile: bounded` はSafetyFilterで位置・速度・加速度・制動距離を制限し、範囲外の開始姿勢を拒否します。この方式は明示した場合だけ有効です。LeRobot方式では有限な開始姿勢を受け入れます。

校正由来の表示範囲、サーボの保護設定、ROS固有のWatchdogとの違いは[LeRobot互換設定](lerobot_compatibility.ja.md)を参照してください。追加フィルターを使用しないことと、実機の速度や衝突を保証することは別です。

## 7. 複数の周期を同じものとして扱わない

| 処理 | real30hz Profileの設定目標 | 役割 |
|---|---|---|
| Driver状態更新 | 60 Hz | 実測状態を得て、送信条件を検査 |
| Controller Manager | 60 Hz | read/update/write |
| Teleop | 60 Hz | 最新Leaderから目標を公開 |
| Safety | 60 Hz | 選択・検査・制限・Enable更新 |
| Rawカメラ | 各30 FPS | 画像取得の要求 |
| Dataset | 30 Hz | 同期後の学習用時刻列 |
| Policyの消費タイマー | 30 Hz | 観測と指令更新のスケジュール目標 |

60 Hzは1周期約16.7 ms、30 Hzは約33.3 msです。60 Hzの制御に30 HzのPolicyを接続すると、ControllerはPolicyの新しい目標がない間も、現在の目標を扱います。Policyの呼び出し頻度はChunk Schedulerやworkerの時間にも左右され、タイマー設定と同じとは限りません。

カメラの要求30 FPSは実測30 FPSの保証ではありません。Datasetの30 Hzも、元の画像を物理的に30回取得した証明ではありません。[収集と同期](guide/teleop.ja.md)で別々に確認します。

## 8. Header、期限、Watchdog

Headerが古い指令を受け取ると、現在の姿勢に対して昔の目標を適用することになります。受信の経過時間だけでなく、メッセージのHeaderも検査します。最近届いたが中身は古い、という状態を区別するためです。

既定の主な値は、command/state timeout各0.25秒、最大Header age 0.25秒、制御gap 0.1秒、旧bounded方式の単一点軌道の長さ0.02秒（LeRobot方式では使用しない）です。値は最新の `config/control/safety.yaml` と `config/hardware/robot.yaml` で確認します。

Watchdogは、決めた時間内に更新が来ない場合に停止する監視です。SafetyだけでなくHardware PluginとFollower Driverにもあります。どれかが停止しても別の監視が古い指令の継続を防ぐためです。

受信のタイムアウトを延ばすと、一見FAULTが減っても、通信停止を検出する時間が長くなります。遅延の原因を調べずに期限だけ広げないようにします。

## 9. 軌道の終了時刻が過去になるエラー

この節は旧bounded ProfileのJoint Trajectory Controllerで扱う軌道の説明です。標準のLeRobot ProfileはForward Position Controllerへ位置配列を渡すため、この軌道補間は使いません。

bounded方式の単一点指令は、Headerの時刻から約20 ms先をtime_from_startで指定します。Controllerが処理するまでに長く待つと、終了予定時刻がすでに過去になります。

`trajectory ... ends in the past` とoverrunが並ぶ場合は、サーボの校正だけでなく送信待ち・CPU負荷・時間設定を確認します。以前は制御writeのDDS待ちが約333 msになった記録があります。RealtimePublisherと非同期DDS送信へ変更しましたが、すべての環境で遅延がなくなる保証ではありません。

この理由は、`trajectory_duration_s` を増やすだけでは分からないままです。read/update/writeの時間、Topic受信周期、Safety診断を分けて測ります。

## 10. 通常停止、Software E-stop、物理停止

| 操作 | 今回の働き | 確認事項 |
|---|---|---|
| IDLEへ変更 | 通常の指令を止め、Enableを解除 | トルクOFFで自重落下しない支持 |
| Software E-stop | ESTOPを保持し、指令再開を拒否 | Runtime・通信が動く条件での停止 |
| FAULT | 古い状態・指令、周期異常などで停止保持 | 理由を診断で確認 |
| 物理電源停止 | PC/ROS/USBと独立して給電を止める | 回路と現場操作の確認 |
| Reset | 原因解消後にIDLEへ戻す | 新しいFollower状態と停止解除通知が必要 |

ROSの `/physical_estop` は補助通知です。通知だけで電源を切る回路になるわけではありません。通信断のTorque OFF指令は届かない可能性があり、実物の停止手段を別に確認します。

独自ロボットでトルクOFFが危険な場合、今回の停止方式をそのまま移しません。ブレーキ、姿勢保持、制御された減速、電源遮断の役割を機構と制御方式に合わせて決めます。

## 11. 制御を変更したときの確認順

最初に計算の単体試験で名前・単位・有限値・範囲・モード遷移を確認します。次にmockでStateとAction、停止保持、復帰先を確認します。その後、読み取りだけの実機通信、校正、位置制限、低い指令からの動作へ進みます。

制御周期を変える場合はrobot、controllers、teleop、safetyの設定をそろえ、再起動後に実測します。学習・推論周期や同期条件も影響を受ける場合はDatasetとモデルの対応を見直します。

自分で説明できる目安は、Leaderの位置、Safety出力、applied_action、実測Follower位置の4つが異なる理由を説明できることです。読み取り先は[構成図](architecture.ja.md)にあります。
