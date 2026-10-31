# The official Noble ROS base has native amd64 and arm64 images.
ARG ROS_IMAGE=ros:jazzy-ros-base-noble
FROM ${ROS_IMAGE}
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEV_UID=1000
ARG DEV_GID=1000
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    mate-desktop-environment-core mate-terminal mate-control-center mate-system-monitor \
    mate-utils pluma terminator mate-applet-brisk-menu mate-screensaver \
    ubuntu-mate-default-settings ubuntu-mate-themes ubuntu-mate-wallpapers-common \
    fonts-ubuntu dconf-cli xterm dbus-x11 dbus-user-session \
    tigervnc-standalone-server tigervnc-common novnc websockify \
    xauth x11-utils x11-xserver-utils xdotool mesa-utils libgl1-mesa-dri \
    libglx-mesa0 libegl-mesa0 fonts-dejavu-core fonts-noto-cjk \
    git curl ca-certificates openssh-client build-essential cmake \
    python3-pip python3-venv python3-yaml python3-colcon-common-extensions \
    python3-rosdep python3-vcstool iproute2 procps netcat-openbsd \
    ros-jazzy-desktop ros-jazzy-rviz2 ros-jazzy-rqt \
    ros-jazzy-rqt-common-plugins ros-jazzy-rqt-runtime-monitor ros-jazzy-turtlesim \
    ros-jazzy-demo-nodes-cpp ros-jazzy-demo-nodes-py \
    && rm -rf /var/lib/apt/lists/*
# Password utilities are packaged separately from the VNC server on Noble.
RUN apt-get update && apt-get install -y --no-install-recommends tigervnc-tools \
    && rm -rf /var/lib/apt/lists/*
# SO-101 and GUI share one Jazzy installation and one Python environment.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-numpy python3-opencv python3-psutil python3-pytest python3-av \
    ros-jazzy-ros2-control ros-jazzy-ros2-controllers ros-jazzy-realtime-tools ros-jazzy-xacro \
    ros-jazzy-robot-state-publisher ros-jazzy-rosbag2-storage-mcap \
    ros-jazzy-foxglove-bridge ros-jazzy-foxglove-msgs ros-jazzy-compressed-image-transport \
    ros-jazzy-launch-testing-ament-cmake ffmpeg libgl1 libegl1 \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH=/opt/venv/bin:$PATH
ARG INSTALL_ML=true
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
RUN --mount=type=cache,target=/root/.cache/pip pip install pyserial==3.5 setuptools==79.0.1 && \
    if [ "$INSTALL_ML" = true ]; then \
      pip install torch==2.11.0 --index-url "$TORCH_INDEX" && \
      pip install safetensors==0.6.2 mujoco==3.3.7 pyarrow==25.0.0; fi
# Ubuntu base images can already own UID 1000; only replace that image user.
RUN test "$DEV_UID" -gt 0 && test "$DEV_GID" -gt 0 && \
    if getent passwd "$DEV_UID" >/dev/null; then \
      userdel "$(getent passwd "$DEV_UID" | cut -d: -f1)"; fi && \
    if ! getent group "$DEV_GID" >/dev/null; then groupadd --gid "$DEV_GID" dev; fi && \
    useradd --uid "$DEV_UID" --gid "$DEV_GID" --create-home --shell /bin/bash dev && \
    install -d -o dev -g "$DEV_GID" /workspace /home/dev/Desktop \
      /home/dev/.config
COPY gui/scripts/entrypoint.sh /usr/local/bin/physical-ai-entrypoint
COPY scripts/entrypoint.sh /usr/local/bin/so101-entrypoint
COPY gui/scripts/start-desktop.sh /usr/local/bin/physical-ai-start-desktop
COPY gui/scripts/healthcheck.py /usr/local/bin/physical-ai-healthcheck
COPY gui/scripts/verify.py /usr/local/bin/physical-ai-verify
COPY gui/config/ros-shell.bash /etc/physical-ai/ros-shell.bash
COPY gui/config/mate/99-physical-ai.gschema.override /usr/share/glib-2.0/schemas/99-physical-ai.gschema.override
COPY gui/config/mate/physical-ai.layout /usr/share/mate-panel/layouts/physical-ai.layout
COPY gui/config/desktop/ /usr/share/applications/
RUN install -d /etc/so101 && touch /etc/so101/unified && \
    glib-compile-schemas --strict /usr/share/glib-2.0/schemas && \
    chmod 755 /usr/local/bin/physical-ai-* /usr/local/bin/so101-entrypoint && \
    printf '\nsource /etc/physical-ai/ros-shell.bash\n' >> /home/dev/.bashrc && \
    cp /usr/share/applications/physical-ai-*.desktop /home/dev/Desktop/ && \
    chown -R dev:"$DEV_GID" /home/dev && \
    chmod 755 /home/dev/Desktop/*.desktop
ENV SO101_IN_CONTAINER=1 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    XDG_CURRENT_DESKTOP=MATE \
    DESKTOP_SESSION=mate \
    DISPLAY=:1 \
    XAUTHORITY=/tmp/physical-ai-runtime/Xauthority \
    XDG_RUNTIME_DIR=/tmp/physical-ai-runtime \
    QT_X11_NO_MITSHM=1 \
    ROS_DISTRO=jazzy
LABEL io.so101.environment=unified
COPY --chown=${DEV_UID}:${DEV_GID} . /workspace
USER dev
WORKDIR /workspace
RUN /workspace/scripts/build_inside.sh && mkdir -p /workspace/data
ENTRYPOINT ["/usr/local/bin/physical-ai-entrypoint"]
CMD ["/usr/local/bin/physical-ai-start-desktop"]
