FROM nvcr.io/nvidia/deepstream:7.0-triton-multiarch

RUN /opt/nvidia/deepstream/deepstream-7.0/user_deepstream_python_apps_install.sh -v 1.1.11

# GStreamer WebRTC uses libnice, DTLS/SRTP and GstWebRTC/GstSdp GI bindings.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-good gstreamer1.0-nice \
    gir1.2-gst-plugins-bad-1.0 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/retrace-requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/retrace-requirements.txt

WORKDIR /workspace/ReTrace

# Match the Compose service default; keep the NVIDIA base entrypoint.
CMD ["python3", "-u", "/workspace/ReTrace/preview.py", "--cameras", "/workspace/ReTrace/configs/cameras.yaml", "--env-file", "/workspace/ReTrace/.env", "--diagnostics"]
