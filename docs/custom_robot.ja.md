# 独自ロボットへ応用する：1関節の通信から実機制御まで

理由や仕組みから学ぶ場合は、[機体の仕様から設計する教材](learning/09-building-another-robot.ja.md)を併読できます。

操作端末は統合コンテナのMATE GUIです。ターミナルで `cd /workspace` を実行し、以降のスクリプトとROSコマンドを直接入力します。初回のDocker準備は[環境構築](setup.ja.md)で行います。

ここまでの手順で、観測、指令、安全確認、記録、学習を一巡しました。独自ロボットを作るときは、この役割分担を利用します。一方、SO-101の関節数、通信方式、校正、モデル入力は実装に組み込まれています。ロボット名だけを書き換えて別の機体を動かせる構成ではありません。

この章では、まず実機に触れない1関節の教材でROSの通信を確認します。そのあと、実機へ移植するときに決める項目と、変更するコードを整理します。[制御の章](control.ja.md)も手元に置いて読みます。

## 1. 実機を使わず、指令と状態の違いを体験する

教材は [one_joint_demo.py](../examples/ros2/one_joint_demo.py) です。USBやモーターへアクセスしません。内部の数値を関節位置として更新し、Topicへ配信します。SO-101 Runtimeとは別のROS Domain 73で動かします。

| 項目 | 教材の動作 | 学ぶこと |
|---|---|---|
| `/learning/joint_command` | `std_msgs/msg/Float64` で目標角度を受信 | 指令を出す側と受ける側を分ける |
| `/learning/joint_states` | `sensor_msgs/msg/JointState` で位置を配信 | 状態には名前、単位、時刻が必要 |
| 目標角度 | −0.5〜0.5 radへ制限 | 外から来る値を無条件で採用しない |
| 更新 | 30 Hzを要求、最大0.2 rad/秒で目標へ近づく | 目標位置と現在位置はすぐには一致しない |
| Watchdog | 指令受信から0.5秒経過したら位置を保持 | 更新停止を検出する |

ここでの「保持」は計算上の位置を変えないことです。実機では、姿勢保持に力が必要な場合と、トルクOFFで落下する場合があります。教材の停止処理を、そのまま実機の停止方式に採用しません。

### 1-1. ターミナルAで教材を起動する

[環境構築](setup.ja.md)が済み、Runtime・校正・学習を終了した状態で、GUI内のターミナルから実行します。

```bash
cd /workspace
ROS_DOMAIN_ID=73 python examples/ros2/one_joint_demo.py
```

`ROS_DOMAIN_ID=73` は、このプロセスだけを練習用の通信グループへ分けます。SO-101の既定Domain42へ教材の指令を混ぜないためです。GUIのPython環境はそのまま使い、ターミナル全体の環境変数は変更しません。

Aには `Simulated joint only` と表示されます。そのまま動かし続けます。

### 1-2. ターミナルBで通信を確認する

GUIのBでも `cd /workspace` を実行します。確認コマンドにも同じDomain73を指定します。

```bash
ROS_DOMAIN_ID=73 ros2 node list
ROS_DOMAIN_ID=73 ros2 topic list -t
```

`/learning_joint_demo` と、教材の2つのTopicを探します。Domain 42のSO-101ノードがここに出ないのは、通信グループを分けたためです。以前ROS CLIを別のDomainで使っていた場合は、CLIのDaemonの状態も確認します。

```bash
ROS_DOMAIN_ID=73 ros2 topic info /learning/joint_states --verbose
ROS_DOMAIN_ID=73 ros2 interface show sensor_msgs/msg/JointState
ROS_DOMAIN_ID=73 ros2 topic echo /learning/joint_states --once --qos-reliability best_effort
```

最初の位置は0 radです。`name` と `position` の同じ添字が対応します。`header.stamp` は状態を配信したROS時刻です。

### 1-3. 指令を送り、途中の状態を読む

Bで次を実行します。

```bash
ROS_DOMAIN_ID=73 ros2 topic pub /learning/joint_command std_msgs/msg/Float64 \
  '{data: 0.2}' --rate 10
```

10 Hzで0.2 radの指令を送り続けます。Aの教材は30 Hzで状態を更新します。通信周期を全部同じにしなくても、役割と更新条件が明確なら動作を構成できます。

ターミナルCを新しく開き、`cd /workspace` を実行してから読みます。確認コマンドにもDomain73を指定します。

```bash
ROS_DOMAIN_ID=73 ros2 topic echo /learning/joint_states --once --qos-reliability best_effort
```

位置は最大0.2 rad/秒で目標へ近づくので、約1秒で0.2 radへ到達します。実際の時間はタイマーの実行遅延にも依存します。

Bの指令配信をCtrl+Cで止めます。Aに、最後の受信から約0.5秒後に `Command timeout` が表示されます。動いている途中で止めれば、その時点の位置で更新が止まります。Cで状態を読み、目標指令の配信停止と状態配信の継続が別々であることを確認します。

終了するときは、Bの配信を終了し、Aの教材もCtrl+Cで止めます。

### 1-4. コードを通信の役割に対応づける

| コード | 役割 | 実機へ移す場合の注意 |
|---|---|---|
| `create_publisher` | 状態Topicを作る | 読み取り失敗を古い正常値で隠さない |
| `create_subscription` | 指令を受ける | 名前・長さ・有限値・時刻・モードを検証する |
| `on_command` | 値を検証し、目標と受信時刻を保存 | 実機の指令には生成時刻の確認も必要 |
| `create_timer` / `on_tick` | 定期処理を呼ぶ | 長い通信待ちで他の処理を止めない |
| `time.monotonic()` | 経過時間を測る | PCの時刻調整でWatchdogが逆戻りしない |
| `get_clock().now()` | ROSメッセージの時刻を付ける | 機器の実測時刻と送信時刻を区別する |
| `rclpy.spin` | 受信・タイマーなどの処理を実行 | Callbackの実行を誰が担うか理解する |

この教材のFloat64にはHeaderがなく、指令の生成時刻を検証できません。また、位置は内部で計算した値です。実機用ドライバーでは実測値、通信異常、停止状態を扱う必要があります。教材を短くした箇所と、実機に必須の箇所を区別してください。

## 2. ロボットの仕様を先に書き出す

最初に、1関節ごとの仕様表を作ります。数値や名称が曖昧なまま、制御コードを先に作らないようにします。

| 決めること | 書き出す内容 | なぜ必要か |
|---|---|---|
| 関節 | 名前、個数、回転／直動、親子関係 | URDF、JointState、Controller、学習配列を対応させる |
| 単位と正方向 | 回転はrad、直動はm。正方向を図へ記入 | 符号逆転やdegree/rad混在を防ぐ |
| 原点 | 基準姿勢、エンコーダ原点、再起動時の再現方法 | 同じ数値で同じ姿勢を指定する |
| 状態取得 | 位置、必要なら速度・電流・力。取得周期と精度 | 制御と異常検出に使う |
| 指令方式 | 位置、速度、トルクなど | Controllerと駆動装置の対応を決める |
| 通信 | USB、CAN、Ethernetなど。ID、速度、応答時間 | 正常周期と故障時の待ち時間を設計する |
| 制限 | 位置、速度、加速度、力、温度、電流 | ハードウェアの能力と作業条件を満たす |
| 停止 | 通信断、PC停止、電源断、非常停止時の動作 | ROSから指令できないときにも備える |
| センサー | カメラ、力覚などの名称、位置、時刻 | 観測と行動の時刻を対応づける |

SO-101のprotocol-0通信や校正tick範囲は、このプロジェクトで使うサーボ向けです。別のモーターには、そのメーカーの通信仕様と校正手順を使います。

## 3. 制御をどこで実行するか決める

駆動装置が位置制御を内蔵しているなら、ROS側から位置目標を渡す構成を検討できます。トルク制御を自作するなら、センサー取得、制御演算、通信を必要周期内に完了させる設計が必要です。100 Hzという数値を設定するだけでは周期を保証できません。

本プロジェクトは、C++のros2_controlとPythonのサーボ通信をTopicで接続します。この構成は責務を追いやすい一方、DDS送信、Python実行、USB通信の遅延を測る必要があります。[制御経路と遅延](control.ja.md)を参照してください。

| 構成候補 | 向いている条件 | 検証すること |
|---|---|---|
| ros2_controlのHardware Interface内で直接通信 | 必要な周期で通信できるドライバーを実装できる | read/writeの最大時間、通信待ち上限、制御スレッドへの影響 |
| ROS Topic経由で外部ドライバーへ接続 | 独立プロセスや別言語で機器を扱う | QoS、メッセージ時刻、キュー滞留、プロセス停止 |
| マイコンで制御し、ROSから目標を渡す | PC通信と分離して周期・停止を管理したい | マイコン側Watchdog、目標の有効期限、状態返信 |

機器の制御周期とAIの推論周期を同じにする必要はありません。推論の結果を実行する間も、状態読み取りと停止監視を継続できる構成にします。物理的な非常停止は、ROSの停止Topicとは別に設計します。

## 4. ROSパッケージ、URDF、Controllerを用意する

Nodeは実行中の役割、Packageはコード・依存関係をまとめる単位です。試作が進んだら、ドライバー、モデル、起動設定をPackageへ整理します。

- Python Nodeなら `ament_python`、依存関係には `rclpy` と使うメッセージPackageを記載する。
- C++のHardware Interfaceなら `ament_cmake`、`hardware_interface`、`pluginlib` などを使い、クラスをPluginとして登録する。
- URDFにはLink、Joint、回転軸、寸法、原点、制限、必要な慣性を実機に合わせて記載する。
- ros2_control設定にはJointごとのstate interfaceとcommand interfaceを定義する。
- Controllerの設定に、同じJoint名と対応するinterfaceを記載する。
- Launchでモデル、Controller Manager、Driver、安全処理、必要なセンサーを起動する。

Python Publisherの基本は[ROS 2公式教材](https://github.com/ros2/ros2_documentation/blob/jazzy/source/Tutorials/Beginner-Client-Libraries/Writing-A-Simple-Py-Publisher-And-Subscriber.rst)、Hardware Interfaceの分類は[ros2_control公式説明](https://control.ros.org/jazzy/doc/ros2_control/hardware_interface/doc/hardware_interface_types_userdoc.html)で確認できます。新しいWorkspaceを作った場合は、その `install/setup.bash` を読み込んだターミナルで実行します。既存Runtimeへ別WorkspaceのOverlayを混ぜる場合は、同名Packageの競合にも注意します。

SO-101の実装例は `so101_hardware` のC++ Pluginと `so101_description` のURDFです。現在の寸法・慣性・カメラTFは近似です。独自機体の衝突判定や動力学へ使うには、実機モデルを作り直します。

## 5. このプロジェクトで変更が必要な場所

同じ6関節SO-101のUSB割当や制限変更と、別の関節構成への移植は変更量が違います。ここでは後者を想定します。

| 対象 | 現在の役割 | 移植時に見直す内容 |
|---|---|---|
| `config/hardware/robot.yaml` | 6関節、モーターID、校正、単位、制限 | 関節名、数、駆動装置、校正方式、能力 |
| `config/control/teleop.yaml` | Leader→Followerの対応 | 名前・倍率・原点。異構造なら運動学による変換も必要 |
| `config/hardware/controllers.yaml` | 関節Controllerの設定 | Joint名、位置／速度などのinterface、周期 |
| `so101_description` | URDF・TF | 実寸、回転軸、親子関係、座標系 |
| `so101_hardware` | 通信とros2_control接続 | 通信プロトコル、状態取得、書込み、Watchdog |
| `so101_core` | 配列順、校正、制限、同期、モデル仕様 | 6次元前提の検証・Dataset・モデル入出力 |
| `so101_teleop` / `so101_safety` | 操作と安全制御 | 新しい指令方式、状態検証、モード、停止条件 |
| `so101_dataset` | 記録とDataset生成 | Topic、message型、状態とActionの意味・次元 |
| `so101_policy` | モデルとROSの接続 | 入出力の単位、次元、周期、前処理 |
| `so101_bringup` | Launch・構成の組合せ | 新しい機器と必須Nodeの終了監視 |

Packageは `ros2_ws/src/` にあります。固定の関節名や配列長を調べる入口は次の検索です。検索結果にはテストや説明も含まれるため、全部を文字置換しません。

```bash
rg -n 'shoulder_pan|elbow_flex|gripper|JOINT_NAMES' ros2_ws/src config tests
```

改造前に、`JointState.name` と配列の対応を統一します。現在は名前から順序を解決する処理がありますが、モデルは学習時に決めた順序の数値配列を使います。関節の数・順序・単位・原点・画像前処理を変更したら、既存モデルの互換性も確認し直します。

校正やRobot設定のhashは誤った組合せを検出するための情報です。検証を通す目的でhashだけを書き換えると、間違った姿勢を指令する原因になります。新しい構成で収集し、新しいDatasetとモデルを作る工程を計画します。

## 6. 移植は一段ずつ検証する

| 順番 | 行うこと | 次へ進む条件 |
|---|---|---|
| 1 | 通信仕様と停止方式を整理する | 読み取り・書込み・応答なし時の動作を説明できる |
| 2 | mockの状態と指令を接続する | 名前・単位・有限値・時刻・制限が検証できる |
| 3 | 実機の状態だけを読む | 電源、ID、原点、正方向、値の再現性を確認できる |
| 4 | 1関節の小さな指令を試す | 目標と実測の差、停止、最大遅延を確認できる |
| 5 | 全関節を接続し手動操作する | 姿勢差、干渉、全体負荷時の周期を確認できる |
| 6 | 短いEpisodeを作る | State・Action・画像の意味と同期が確認できる |
| 7 | 学習とmock推論を行う | 入出力・正規化・モデルと設定の一致が確認できる |
| 8 | 実機で自律動作を評価する | 停止手段、評価範囲、失敗時の扱いを決めている |

通信試験では、平均時間に加えて最大時間と異常時の待ち時間を記録します。動いているように見えても、稀な数百msの遅延がWatchdogやControllerの期限を超える場合があります。

実機へ指令を出す前に、指令が途切れた場合、状態が途切れた場合、値がNaNの場合、範囲を超えた場合をmockで試します。その後、実際の停止回路と駆動装置の応答を確認します。ROSの単体テストと実機停止試験は、別の証拠として残します。

## 7. 次の学習課題を決める

1関節教材を使って、指令10 Hz、更新30 Hz、Watchdog0.5秒の役割を自分の言葉で説明してみてください。次に、指令を途中で止めたとき、指令の値と状態の値がどう違うかを記録します。

機体の設計ができたら、手順2の仕様表を実際の数値で埋めます。そこから、必要な制御周期と通信上限を決め、どこにWatchdogを置くかを図にします。その図と仕様表が、独自ロボットのDriver、安全処理、Launchを実装するための設計の入口になります。

RTC・Training-time RTC・VLASH・A2C2は小型flowに加え、[モデル選択](methods/models.ja.md)のLeRobotモデルにも接続しています。別の指令方式へ移す場合は、動作列の意味、将来状態の見積もり、許容遅延も見直します。MoveItの経路計画・IK・実機の衝突回避、カメラ内部・外部パラメーターの自動測定は別の実装が必要です。それぞれの入力、単位、期限、停止条件を設計してから接続します。

## 8. 複数ロボットの名前と通信を分ける

同じDomainで2台のRuntimeを起動し、両方が `/follower/joint_states` を公開すると、受信側には同じTopicの複数Publisherとして見えます。どちらのロボットの値かを、Topic名だけで区別できません。独自機体を増やすときは、Namespace（名前の共通の接頭辞）とTopicの割当も設計します。

例えば `robot_a/joint_states` と `robot_b/joint_states` のような名前を選び、制御指令、状態、モード、Service、TFのFrame名、記録先まで対応づけます。先頭 `/` を付けた絶対名はNodeのNamespaceに対して自動で相対化されません。本プロジェクトには絶対Topic名があるため、LaunchにNamespaceを追加するだけで2台対応が完成するとは限りません。

ROSのRemapは通信名の変更です。使う側・出す側を同じ名前へ対応させ、Datasetの記録Topic設定も合わせます。関節名や学習配列の意味を変える操作とは別です。まず1関節教材の通信名で試し、`topic info --verbose` で接続するPublisherとSubscriberを確認します。

Domainを分ければ通信グループを分離できますが、意図した共同動作には別途接続の設計が必要です。共有GPUやUSBを分離する機能でもありません。複数機体で同時実行する場合は、このプロジェクトの共通Computeロックとデバイス所有も見直します。
