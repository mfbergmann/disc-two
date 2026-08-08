# Disc Two reads ISOs. It does not rip, so none of the optical-drive machinery
# a ripper needs is here: no dvdbackup, no cdparanoia, no eject.
FROM debian:bookworm-slim AS dvdcss

# Not in Debian main. Built from source so the runtime image carries no extra
# apt repos. Only needed for ISOs taken straight off a CSS-protected disc with
# dd; an ISO from dvdbackup or MakeMKV is already decrypted.
ARG LIBDVDCSS_VERSION=1.4.3
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential ca-certificates wget; \
    wget -qO /tmp/libdvdcss.tar.bz2 \
        "https://download.videolan.org/pub/libdvdcss/${LIBDVDCSS_VERSION}/libdvdcss-${LIBDVDCSS_VERSION}.tar.bz2"; \
    mkdir -p /tmp/src; tar -xjf /tmp/libdvdcss.tar.bz2 -C /tmp/src --strip-components=1; \
    cd /tmp/src; ./configure --prefix=/usr --libdir=/usr/lib/dvdcss; \
    make -j"$(nproc)"; make install DESTDIR=/staging

FROM debian:bookworm-slim

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 \
        lsdvd \
        genisoimage \
        handbrake-cli \
        ffmpeg \
        tesseract-ocr tesseract-ocr-eng \
        libdvdread8 \
        git \
        ca-certificates \
        tzdata \
        curl; \
    rm -rf /var/lib/apt/lists/*

COPY --from=dvdcss /staging/usr/lib/dvdcss/ /usr/lib/dvdcss/
RUN echo /usr/lib/dvdcss > /etc/ld.so.conf.d/dvdcss.conf && ldconfig

COPY disctwo/ /app/disctwo/
COPY web/ /app/web/
RUN printf '#!/bin/sh\nexec python3 -m disctwo.cli "$@"\n' > /usr/local/bin/disc-two \
 && chmod +x /usr/local/bin/disc-two

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    ISO_DIR=/isos \
    STATE_DIR=/config \
    WEB_PORT=8472

WORKDIR /app
EXPOSE 8472
CMD ["python3", "-m", "disctwo.server"]
