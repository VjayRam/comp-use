FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

USER root
ENV DEBIAN_FRONTEND=noninteractive

# Pin exact Jammy package versions for reproducible builds.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb=2:21.1.4-2ubuntu1.7~22.04.16 \
    x11vnc=0.9.16-8 \
    novnc=1:1.0.0-5 \
    websockify=0.10.0+dfsg1-2build1 \
    supervisor=4.2.1-1ubuntu1 \
    xfce4 \
    xfce4-terminal \
    dbus-x11 \
    socat \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# The Playwright base image bundles browsers, but not necessarily the Python package.
RUN python -m pip install --no-cache-dir playwright==1.58.0

# Note: Playwright "chrome" and "msedge" channels are not supported on Linux Arm64.
# We keep the sandbox cross-platform by relying on bundled Chromium for all chromium-family
# browsers on Arm64. If you need real Chrome/Edge channels, run the container as linux/amd64.

# Pre-create X11/ICE temp dirs (required when running as non-root).
RUN mkdir -p /tmp/.X11-unix /tmp/.ICE-unix && chmod 1777 /tmp/.X11-unix /tmp/.ICE-unix

USER pwuser

COPY --chown=pwuser:pwuser supervisord.conf /home/pwuser/sandbox/supervisord.conf
COPY --chown=pwuser:pwuser entrypoint.sh /home/pwuser/sandbox/entrypoint.sh
COPY --chown=pwuser:pwuser launch_browser.py /home/pwuser/sandbox/launch_browser.py
COPY --chown=pwuser:pwuser novnc-index.html /home/pwuser/sandbox/novnc-web/index.html

USER root
RUN /bin/bash -lc 'set -euo pipefail; \
  mkdir -p /home/pwuser/sandbox/novnc-web; \
  for p in /usr/share/novnc/*; do ln -sf "$p" "/home/pwuser/sandbox/novnc-web/$(basename "$p")"; done; \
  chmod +x /home/pwuser/sandbox/entrypoint.sh'
USER pwuser

EXPOSE 6080

ENTRYPOINT ["/bin/bash", "/home/pwuser/sandbox/entrypoint.sh"]

