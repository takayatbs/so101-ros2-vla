from pathlib import Path
import os
import xacro
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, RegisterEventHandler, EmitEvent
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from so101_core.config import load_system
from so101_core.lerobot_compat import control_profile


def build(context):
    config_path = LaunchConfiguration("config").perform(context)
    backend = LaunchConfiguration("backend").perform(context)
    if backend not in ("mock", "mujoco", "real"):
        raise ValueError("backend must be mock, mujoco or real")
    config = load_system(config_path)
    if backend == "real" and not config["robot"]["hardware_qualified"]:
        raise ValueError("See docs/hardware_acceptance.ja.md and qualify robot.yaml before real runtime")
    description = Path(get_package_share_directory("so101_description"))/"urdf/so101.urdf.xacro"
    urdf = xacro.process_file(str(description), mappings={"robot_config": config["robot"]["_file"]}).toxml()
    controllers = config["controllers"]["_file"]
    command_controller = "forward_position_controller" if control_profile(config["robot"]) == "lerobot" else "joint_trajectory_controller"
    parameters = [{"config": config_path, "backend": backend}]
    leader = Node(package="so101_hardware", executable="so101_leader_driver", parameters=parameters, output="screen")
    follower = Node(package="so101_hardware", executable="so101_follower_driver", parameters=parameters, output="screen")
    safety = Node(package="so101_safety", executable="safety_node", parameters=parameters, output="screen")
    manager = Node(package="controller_manager", executable="ros2_control_node",
                   parameters=[controllers, {"update_rate": int(config["robot"]["update_rate_hz"])}], output="screen")
    nodes = [leader, follower, manager,
             Node(package="robot_state_publisher", executable="robot_state_publisher",
                  parameters=[{"robot_description": urdf}], remappings=[("joint_states", "/follower/joint_states")]),
             Node(package="controller_manager", executable="spawner", arguments=["joint_state_broadcaster",
                  "--controller-manager-timeout", "30", "--controller-ros-args",
                  "--remap /joint_state_broadcaster/joint_states:=/follower/joint_states"], output="screen"),
             Node(package="controller_manager", executable="spawner", arguments=[command_controller,
                  "--controller-manager-timeout", "30"], output="screen"),
             Node(package="so101_teleop", executable="so101_teleop", parameters=parameters, output="screen"),
             safety,
             Node(package="so101_dataset", executable="data_recorder", parameters=parameters, output="screen"),
             Node(package="so101_dataset", executable="episode_manager", parameters=parameters, output="screen"),
             Node(package="so101_policy", executable="policy_node", parameters=[*parameters, {
                  "policy_backend": LaunchConfiguration("policy_backend").perform(context),
                  "model_id": LaunchConfiguration("model_id").perform(context),
                  "action_smoothing": LaunchConfiguration('action_smoothing').perform(context),
                  "correction_checkpoint": LaunchConfiguration('correction_checkpoint').perform(context)}],
                  prefix=LaunchConfiguration('model_python').perform(context), output="screen"),
             Node(package="so101_bringup", executable="system_diagnostics", parameters=parameters)]
    for name in config["camera"]["cameras"]:
        nodes.append(Node(package="so101_hardware", executable="camera_capture", name="camera_"+name,
                          parameters=[*parameters, {"camera": name}], output="screen"))
    if LaunchConfiguration("foxglove").perform(context).lower() == "true":
        nodes.append(Node(package="foxglove_bridge", executable="foxglove_bridge",
                          parameters=[{"address": os.environ.get("SO101_FOXGLOVE_BIND_ADDRESS", "127.0.0.1"),
                                       "port": int(os.environ.get("SO101_FOXGLOVE_PORT", "8765")),
                                       "topic_whitelist": ["/camera/.*/preview/compressed", "/camera/.*/video", "/.*joint_states",
                                                           "/diagnostics", "/tf.*", "/control_mode", "/episode/state",
                                                           "/policy/action", "/safe_action"],
                                       "service_whitelist": ["/set_control_mode", "/start_episode", "/stop_episode"]}]))
    # A critical process exit tears down the runtime, allowing driver finally
    # blocks and its independent command watchdog to stop torque.
    for critical in (follower, manager, safety):
        nodes.append(RegisterEventHandler(OnProcessExit(target_action=critical,
                     on_exit=[EmitEvent(event=Shutdown(reason="Critical runtime process exited"))])))
    return nodes


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("config", default_value="/workspace/config/runtime/system.yaml"),
        DeclareLaunchArgument("backend", default_value="mock"),
        DeclareLaunchArgument("policy_backend", default_value="hold"),
        DeclareLaunchArgument("model_id", default_value="example_act"),
        DeclareLaunchArgument('correction_checkpoint',default_value=''),
        DeclareLaunchArgument('model_python',default_value=''),
        DeclareLaunchArgument('action_smoothing', default_value='none'),
        DeclareLaunchArgument("foxglove", default_value="true"),
        OpaqueFunction(function=build),
    ])
