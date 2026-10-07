FROM nvcr.io/nvidia/deepstream:7.0-triton-multiarch

RUN /opt/nvidia/deepstream/deepstream-7.0/user_deepstream_python_apps_install.sh -v 1.1.11

COPY requirements.txt /tmp/retrace-requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/retrace-requirements.txt

WORKDIR /workspace/ReTrace

# Match the Compose service default; keep the NVIDIA base entrypoint.
CMD ["python3", "-u", "/workspace/ReTrace/preview.py", "--cameras", "/workspace/ReTrace/configs/cameras.yaml", "--env-file", "/workspace/ReTrace/.env", "--diagnostics"]
