# 2. ROS 2で処理をつなぐ

[教材の入口](../learning_path.ja.md) ／ [前の教材](01-loop-and-environment.ja.md)

関節位置を画面へ表示するたびに、表示ソフトからサーボへアクセスすると、記録や制御とUSBを取り合います。Driverが読み取った値をROSメッセージとして公開すれば、表示・記録・制御は同じ測定結果を受け取れます。処理を分ける単位と、分けた処理を接続する約束がROS 2の基礎です。

## NodeとPackageを分けて考える

ROSと通信する処理単位がNodeです。カメラの画像を取得するNode、手動操作から目標を作るNode、停止条件を確認するNodeなどがあります。実行中のNodeは `ros2 node list` で調べられます。

コードや設定、依存関係をまとめる単位はPackageです。1つのPackageから複数のNodeを起動できます。このリポジトリでは `so101_hardware` がアームとカメラの処理を含み、Launchがカメラごとに別のNodeを起動します。

NodeとOSのプロセスは、必ず1対1になるとは限りません。このRuntimeは主に処理ごとのプロセスを起動していますが、ROSには複数のNodeを同じプロセスへまとめる構成もあります。Package名だけからプロセス数や処理の並列性は判断できません。

## データの型と通信名をそろえる

Topicは繰り返し届くデータを配る通信名です。公開する側をPublisher、受け取る側をSubscriberと呼びます。メッセージの型は、そのデータにどの項目があるかを定めます。

例えば `/hardware/follower/state` はDriverの実測値を配るTopicで、型は `sensor_msgs/msg/JointState` です。メッセージには時刻、関節名、位置などが入ります。`name[0]` と `position[0]` が対応するため、配列の数値だけを読んでも関節は特定できません。

| 調べる対象 | 読み取り用のコマンド | 分かること |
|---|---|---|
| 実行中の処理 | `ros2 node list` | Node名 |
| 通信名と型 | `ros2 topic list -t` | Topicとメッセージの型 |
| 接続している相手 | `ros2 topic info /hardware/follower/state --verbose` | Publisher、Subscriber、QoS |
| 型の定義 | `ros2 interface show sensor_msgs/msg/JointState` | 項目の名前と型 |

これらは起動済みRuntimeと同じROS環境の端末で使います。ROSのコマンドが動くことと、対象Nodeが起動していることも別です。Topicがない場合は、まずRuntimeの起動ログと `node list` を確認します。

## TopicとServiceとROS Actionの選択

画像や関節位置には、継続的に新しい値を配るTopicを使います。モード変更には要求して成否を受け取るServiceを使います。`mode.sh TELEOP` の実行では、要求が届いたことに加えて、応答の `success` がtrueかを確認します。

ROS Actionは目標を要求し、途中経過と終了結果を受け取り、必要ならキャンセルする通信方式です。長い軌道の実行などで使えます。一方、機械学習でいうactionはロボットへ与える指令データの呼び名です。今回の `/policy/action` はTopicであり、ROS Actionのサーバー名ではありません。

この区別は別のロボットのAPIをつなぐときにも必要です。メーカーの軌道APIが「動作完了まで待つ」方式なのか、「目標を更新し続ける」方式なのかによって、ROS側の接続とWatchdogの設計が変わります。

## QoSで何を選んでいるのか

同じTopic名と型でも、送信側と受信側のQoSが合わなければデータは届きません。QoSは通信の信頼性、保持する件数、後から接続した受信者へ過去の値を渡すか、といった扱いを決める設定です。

今回のコードでは位置やRaw画像にセンサー向けのbest effort、指令にdepth 1のreliable、モードや準備完了の通知にtransient localを使います。best effortは欠落を許容する方式、reliableは再送を含めて配送を扱う方式です。transient localは保持している値を後から接続した対応Subscriberにも渡せます。詳しい組合せは[ROS公式のQoS説明](https://github.com/ros2/ros2_documentation/blob/jazzy/source/Concepts/Intermediate/About-Quality-of-Service-Settings.rst)にあります。

動作指令のQueueへ大量の古い目標が残ると、受け取れた順に過去の動作を続けてしまいます。そこでdepthを小さくし、受信時にも生成時刻や期限を検査します。reliableを選んでも、期限内に届くことやモーターの動作完了まで保証されるわけではありません。

H.264映像には、前のフレームを使って復元するフレームが含まれます。この映像TopicはRaw画像とは異なるQoSで配信しています。すべてのTopicへ同じ通信設定を当てず、データが失われたときの影響から選びます。

## DomainとNamespaceが分けるもの

`ROS_DOMAIN_ID` はROSの通信グループを分ける設定です。このプロジェクトの既定値は42で、学習用の1関節教材は例として73を使います。別Domainで起動すると、同じ名前のTopicを使っていても通常は直接つながりません。

Domainは機器のアクセス権を分ける機能ではありません。別Domainの2つのプログラムが同じUSB機器を開こうとすれば、取り合いは残ります。また、認証や安全のための境界としてDomainだけに頼る設計にはしません。

同じ通信グループ内で複数のロボットを区別するには、`/robot_a/joint_states` のようなNamespaceや名前の割当を設計します。今回の実装には `/follower/joint_states` などの絶対名があるため、LaunchへNamespaceを1つ足すだけでは全通信名が置き換わりません。Service、TFのFrame名、記録対象も合わせて見直します。

## CallbackとExecutorの待ち時間

Subscriberにデータが届いたときやタイマーの時間になったとき、登録した関数を実行するのがCallbackです。Callbackを呼び出す実行管理をExecutorと呼びます。`rclpy.spin` は受信・タイマー・Serviceなどの処理を実行し続けます。[ROS公式のExecutor説明](https://github.com/ros2/ros2_documentation/blob/jazzy/source/Concepts/Intermediate/About-Executors.rst)でも役割を確認できます。

1本のスレッドで画像圧縮に長くかかると、その間に同じ実行系で処理する受信やタイマーも待たされます。タイマーに30 Hzを指定しても、前の処理が終わらなければ予定どおりには呼ばれません。今回の画像取得、NVENC圧縮、モデル推論を別スレッドへ分ける設計は、この待ち時間を制御処理から分離するためのものです。

スレッドを増やせば待ちが消えるとは限りません。GPU、CPU、USB、メモリー帯域を共有し、Queueやロックによる待ちも生じます。処理時間に加え、データを取得してから使うまでの経過時間を測ります。

## 接続を調べる順番

画像が表示されないときは、Nodeの存在、Topicと型、Publisherの数、QoS、メッセージの更新時刻を順に確認します。最後の画像が画面に残っているだけなら、新しいデータが届いている証拠にはなりません。

準備完了の通知のように値を保持するTopicを読むときは、次のように対応するQoSを指定します。

```bash
ros2 topic echo /policy/ready --once \
  --qos-reliability reliable --qos-durability transient_local
```

この値はモデルを読み込み初回計算を終えたかの確認です。作業の成功や実機の停止性能の判定は、別の検証になります。

実装の対応は[共通QoSとメッセージ生成](../../ros2_ws/src/so101_core/so101_core/ros_utils.py)、操作の練習は[mockの操作手順](../guide/mock.ja.md)で確認できます。

[次の教材：実機の位置と制御と停止](03-hardware-and-control.ja.md)
