FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.type="app" io.hass.version="${BUILD_VERSION}" io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY app ./app
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python3", "-c", "import os, urllib.request; p=os.getenv('LISTEN_PORT','8765'); urllib.request.urlopen('http://127.0.0.1:'+p+'/health', timeout=3)"]
