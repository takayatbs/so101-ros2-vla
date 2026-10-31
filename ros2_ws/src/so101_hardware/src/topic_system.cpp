#include <algorithm>
#include <chrono>
#include <cmath>
#include <memory>
#include <string>
#include <vector>
#include "hardware_interface/system_interface.hpp"
#include "hardware_interface/types/hardware_interface_type_values.hpp"
#include "pluginlib/class_list_macros.hpp"
#include "rclcpp/rclcpp.hpp"
#include "realtime_tools/realtime_publisher.hpp"
#include "sensor_msgs/msg/joint_state.hpp"
#include "std_msgs/msg/bool.hpp"
#include "trajectory_msgs/msg/joint_trajectory.hpp"

namespace so101_hardware {
class TopicSystem : public hardware_interface::SystemInterface {
  using Clock = std::chrono::steady_clock;
  std::shared_ptr<rclcpp::Node> node_;
  std::unique_ptr<rclcpp::executors::SingleThreadedExecutor> executor_;
  rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr state_sub_;
  rclcpp::Subscription<std_msgs::msg::Bool>::SharedPtr enable_sub_;
  rclcpp::Subscription<trajectory_msgs::msg::JointTrajectory>::SharedPtr safe_sub_;
  rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr command_pub_;
  std::unique_ptr<realtime_tools::RealtimePublisher<sensor_msgs::msg::JointState>> command_rt_pub_;
  std::vector<double> positions_, commands_;
  std::vector<std::string> names_;
  Clock::time_point state_time_{}, enable_time_{}, safe_time_{};
  Clock::time_point first_state_time_{};
  size_t state_samples_ = 0;
  bool received_ = false, enabled_ = false;
  double timeout_ = 0.25;
  double age(Clock::time_point time) const {return std::chrono::duration<double>(Clock::now()-time).count();}
public:
  hardware_interface::CallbackReturn on_init(const hardware_interface::HardwareInfo & info) override {
    if (SystemInterface::on_init(info) != hardware_interface::CallbackReturn::SUCCESS)
      return hardware_interface::CallbackReturn::ERROR;
    for (const auto & j : info.joints) {
      if (j.command_interfaces.size() != 1 || j.command_interfaces[0].name != "position" ||
          j.state_interfaces.size() != 1 || j.state_interfaces[0].name != "position")
        return hardware_interface::CallbackReturn::ERROR;
      names_.push_back(j.name);
    }
    positions_.assign(names_.size(), 0.0);
    commands_ = positions_;
    if (info.hardware_parameters.count("timeout_s")) timeout_ = std::stod(info.hardware_parameters.at("timeout_s"));
    return hardware_interface::CallbackReturn::SUCCESS;
  }
  std::vector<hardware_interface::StateInterface> export_state_interfaces() override {
    std::vector<hardware_interface::StateInterface> result;
    for (size_t i=0;i<names_.size();++i) result.emplace_back(names_[i], "position", &positions_[i]);
    return result;
  }
  std::vector<hardware_interface::CommandInterface> export_command_interfaces() override {
    std::vector<hardware_interface::CommandInterface> result;
    for (size_t i=0;i<names_.size();++i) result.emplace_back(names_[i], "position", &commands_[i]);
    return result;
  }
  hardware_interface::CallbackReturn on_configure(const rclcpp_lifecycle::State &) override {
    if (executor_ && node_) executor_->remove_node(node_);
    node_ = rclcpp::Node::make_shared("so101_control_hardware");
    executor_ = std::make_unique<rclcpp::executors::SingleThreadedExecutor>();
    executor_->add_node(node_);
    received_ = false;
    state_samples_ = 0;
    state_sub_ = node_->create_subscription<sensor_msgs::msg::JointState>(
      "/hardware/follower/state", rclcpp::SensorDataQoS(), [this](sensor_msgs::msg::JointState::ConstSharedPtr msg) {
        const double header_age = (node_->now()-rclcpp::Time(msg->header.stamp, node_->get_clock()->get_clock_type())).seconds();
        if (header_age < -0.05 || header_age > timeout_) return;
        if (msg->name.size()!=names_.size() || msg->position.size()!=names_.size()) return;
        std::vector<double> next(names_.size());
        for (size_t i=0;i<names_.size();++i) {
          auto found=std::find(msg->name.begin(),msg->name.end(),names_[i]);
          if (found==msg->name.end()) return;
          next[i]=msg->position[std::distance(msg->name.begin(),found)];
          if (!std::isfinite(next[i])) return;
        }
        positions_=next; state_time_=Clock::now();
        if (!received_) first_state_time_=state_time_;
        ++state_samples_;
        received_=true;
      });
    enable_sub_ = node_->create_subscription<std_msgs::msg::Bool>(
      "/hardware/motor_enable", rclcpp::QoS(1).reliable(), [this](std_msgs::msg::Bool::ConstSharedPtr msg) {
        enabled_=msg->data; enable_time_=Clock::now();
      });
    safe_sub_ = node_->create_subscription<trajectory_msgs::msg::JointTrajectory>(
      "/safe_action", rclcpp::QoS(1).reliable(), [this](trajectory_msgs::msg::JointTrajectory::ConstSharedPtr) {safe_time_=Clock::now();});
    command_pub_ = node_->create_publisher<sensor_msgs::msg::JointState>(
      "/hardware/follower/command", rclcpp::QoS(1).reliable());
    // DDS publication can block on reliable readers. Never perform it in the
    // controller's update thread; a busy publisher skips this cycle and the
    // independent driver command watchdog still bounds a stalled transport.
    command_rt_pub_ = std::make_unique<
      realtime_tools::RealtimePublisher<sensor_msgs::msg::JointState>>(command_pub_);
    command_rt_pub_->msg_.name = names_;
    command_rt_pub_->msg_.position.resize(names_.size());
    return hardware_interface::CallbackReturn::SUCCESS;
  }
  hardware_interface::CallbackReturn on_activate(const rclcpp_lifecycle::State &) override {
    const auto start=Clock::now();
    // A single queued packet is insufficient evidence that a fresh stream has
    // started. Keep torque disabled while discovery/imports are still settling.
    auto stream_ready = [this]() {
      return received_ && state_samples_ >= 3 && age(state_time_) <= timeout_ &&
        std::chrono::duration<double>(state_time_-first_state_time_).count() >= 0.05;
    };
    while (!stream_ready() && age(start)<10.0 && rclcpp::ok()) {
      executor_->spin_some();
      rclcpp::sleep_for(std::chrono::milliseconds(10));
    }
    if (!stream_ready()) return hardware_interface::CallbackReturn::ERROR;
    commands_=positions_;
    return hardware_interface::CallbackReturn::SUCCESS;
  }
  hardware_interface::CallbackReturn on_deactivate(const rclcpp_lifecycle::State &) override {
    enabled_=false; commands_=positions_;
    return hardware_interface::CallbackReturn::SUCCESS;
  }
  hardware_interface::return_type read(const rclcpp::Time &, const rclcpp::Duration &) override {
    // Keep one executor/wait set instead of registering the node every 10 ms.
    executor_->spin_some();
    if (!received_ || age(state_time_)>timeout_) return hardware_interface::return_type::ERROR;
    return hardware_interface::return_type::OK;
  }
  hardware_interface::return_type write(const rclcpp::Time &, const rclcpp::Duration &) override {
    if (!enabled_ || age(enable_time_)>timeout_ || age(safe_time_)>timeout_) {
      commands_=positions_;
      return hardware_interface::return_type::OK;
    }
    for (double q:commands_) if (!std::isfinite(q)) return hardware_interface::return_type::ERROR;
    if (command_rt_pub_->trylock()) {
      command_rt_pub_->msg_.header.stamp = node_->now();
      std::copy(commands_.begin(), commands_.end(), command_rt_pub_->msg_.position.begin());
      command_rt_pub_->unlockAndPublish();
    }
    return hardware_interface::return_type::OK;
  }
};
}
PLUGINLIB_EXPORT_CLASS(so101_hardware::TopicSystem, hardware_interface::SystemInterface)
