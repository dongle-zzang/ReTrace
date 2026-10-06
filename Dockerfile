FROM nvcr.io/nvidia/deepstream:7.0-triton-multiarch

RUN /opt/nvidia/deepstream/deepstream-7.0/user_deepstream_python_apps_install.sh -v 1.1.11

WORKDIR /workspace/ReTrace
