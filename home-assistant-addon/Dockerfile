FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY app ./app
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
