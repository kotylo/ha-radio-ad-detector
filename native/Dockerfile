# Build a Python-version-independent Lite C runtime for HA OS/Container (musl).
# Run on the target architecture, or with Docker buildx --platform.
FROM alpine:3.23 AS configured
RUN apk add --no-cache build-base cmake ninja git python3 linux-headers curl
WORKDIR /src
RUN curl -fL --retry 3 https://github.com/tensorflow/tensorflow/archive/refs/tags/v2.14.0.tar.gz | tar xz
COPY native/select_ops.py /tmp/select_ops.py
RUN python3 /tmp/select_ops.py
RUN cmake -S /src/tensorflow-2.14.0/tensorflow/lite/c -B /build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_C_FLAGS=-D_LARGEFILE64_SOURCE \
    -DCMAKE_CXX_FLAGS=-D_LARGEFILE64_SOURCE \
    "-DCMAKE_SHARED_LINKER_FLAGS=-static-libstdc++ -static-libgcc" \
    -DTFLITE_ENABLE_XNNPACK=OFF \
    -DTFLITE_ENABLE_RUY=ON \
    -DTFLITE_ENABLE_GPU=OFF \
    -DTFLITE_ENABLE_NNAPI=OFF \
    -DTFLITE_ENABLE_EXTERNAL_DELEGATE=OFF
FROM configured AS build
ARG BUILD_JOBS=2
RUN cmake --build /build --target tensorflowlite_c --parallel ${BUILD_JOBS}
RUN mkdir /out && cp /build/libtensorflowlite_c.so /out/ && strip /out/libtensorflowlite_c.so
COPY native/notices.py /tmp/notices.py
RUN python3 /tmp/notices.py
FROM scratch AS artifact
COPY --from=build /out/ /
